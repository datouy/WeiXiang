"""Windows 进程内存读取（纯 ctypes，无第三方依赖）。

用于从正在运行的 Weixin.exe 里抓取数据库密钥。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
from dataclasses import dataclass
from typing import Iterator

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_READWRITE = 0x04
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01

MAX_PATH = 260


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    """x64 布局：sizeof == 48。"""

    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("_pad1", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("_pad2", wt.DWORD),
    ]


assert ctypes.sizeof(MEMORY_BASIC_INFORMATION) == 48, ctypes.sizeof(MEMORY_BASIC_INFORMATION)

kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.VirtualQueryEx.restype = ctypes.c_size_t
kernel32.VirtualQueryEx.argtypes = [
    wt.HANDLE,
    ctypes.c_void_p,
    ctypes.POINTER(MEMORY_BASIC_INFORMATION),
    ctypes.c_size_t,
]
kernel32.ReadProcessMemory.restype = wt.BOOL
kernel32.ReadProcessMemory.argtypes = [
    wt.HANDLE,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]


@dataclass
class Region:
    base: int
    size: int
    state: int
    protect: int
    type: int

    @property
    def readable(self) -> bool:
        return (
            self.state == MEM_COMMIT
            and self.type == MEM_PRIVATE
            and bool(self.protect & PAGE_READWRITE)
            and not (self.protect & PAGE_GUARD)
        )


def open_process(pid: int):
    h = kernel32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        raise OSError(
            ctypes.get_last_error(),
            f"OpenProcess({pid}) 失败（可能需要管理员权限，或进程已退出）",
        )
    return h


def close_handle(h) -> None:
    try:
        kernel32.CloseHandle(h)
    except Exception:
        pass


def enum_regions(handle, min_addr: int = 0x10000, max_addr: int = 0x7FFFFFFFFFFF) -> Iterator[Region]:
    mbi = MEMORY_BASIC_INFORMATION()
    addr = min_addr
    while addr < max_addr:
        ret = kernel32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not ret:
            break
        size = int(mbi.RegionSize)
        if size <= 0:
            break
        yield Region(
            base=int(mbi.BaseAddress or 0),
            size=size,
            state=int(mbi.State),
            protect=int(mbi.Protect),
            type=int(mbi.Type),
        )
        nxt = int(mbi.BaseAddress or 0) + size
        if nxt <= addr:
            break
        addr = nxt


def read_memory(handle, addr: int, size: int) -> bytes | None:
    buf = ctypes.create_string_buffer(size)
    read = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(
        handle, ctypes.c_void_p(addr), buf, ctypes.c_size_t(size), ctypes.byref(read)
    )
    if not ok:
        return None
    return buf.raw[: read.value]


def iter_readable_memory(handle, min_region: int = 1024 * 1024, chunk: int = 16 * 1024 * 1024):
    """产出 (base_addr, bytes)，只包含可读私有可写且足够大的区段。"""
    for reg in enum_regions(handle):
        if reg.size < min_region or not reg.readable:
            continue
        off = 0
        while off < reg.size:
            n = min(chunk, reg.size - off)
            data = read_memory(handle, reg.base + off, n)
            if data:
                yield reg.base + off, data
            off += n


# --------------------------------------------------------------------------
# 进程枚举
# --------------------------------------------------------------------------
psapi = ctypes.WinDLL("psapi", use_last_error=True)
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE,
    wt.DWORD,
    wt.LPWSTR,
    ctypes.POINTER(wt.DWORD),
]


def list_processes() -> list[dict]:
    """返回 [{pid, name, exe}]。"""
    count = 1024
    while True:
        arr = (wt.DWORD * count)()
        needed = wt.DWORD(0)
        if not psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(needed)):
            raise OSError(ctypes.get_last_error(), "EnumProcesses 失败")
        n = needed.value // ctypes.sizeof(wt.DWORD)
        if n < count:
            break
        count *= 2

    out = []
    for i in range(n):
        pid = int(arr[i])
        if pid == 0:
            continue
        h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(MAX_PATH * 2)
            size = wt.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                exe = buf.value
                out.append({"pid": pid, "exe": exe, "name": os.path.basename(exe)})
        finally:
            close_handle(h)
    return out


def find_processes(name: str) -> list[dict]:
    name = name.lower()
    return [p for p in list_processes() if p["name"].lower() == name]


def working_set_size(pid: int) -> int:
    """粗略取进程工作集大小（用于挑主进程）。"""
    h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return 0
    try:
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wt.DWORD),
                ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        c = PROCESS_MEMORY_COUNTERS()
        c.cb = ctypes.sizeof(c)
        if psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return int(c.WorkingSetSize)
    finally:
        close_handle(h)
    return 0


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False
