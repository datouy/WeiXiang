"""微信 4.1+ 密钥提取（Windows）：Config.Cipher 对象 blob XOR 解码。

原理（对齐 wcdb-key-tool 的 Windows 只读扫描）：
  1. 在 Weixin.exe 可读内存里搜 `com.Tencent.WCDB.Config.Cipher` 字符串
  2. 找引用它的「指针+长度」对 `<Q:addr><Q:len>`
  3. 该对往前 0x10 字节是 node 结构：node[0x28] = Config 对象指针
  4. Config 对象 +0x88 处是 blob 描述符：+0x8 = data_ptr，+0x10 = data_len
  5. blob 用 38 字节机器码掩码做循环 XOR 解码
  6. 解码结果里是 SQL 字面量 `x'<64hex key><32hex salt>'`
  7. key 是**裸 AES-256 密钥**，用页面 HMAC 校验（mac_key = PBKDF2(key, salt^0x3a, 2)）

返回 {salt_hex: key_hex}，并写入 data/all_keys.json。
"""

from __future__ import annotations

import json
import re
import struct
import time
from pathlib import Path
from typing import Callable

from .. import config as C
from . import cipher, locate, winmem
from .winmem import MEM_COMMIT, PAGE_GUARD, PAGE_NOACCESS

NAME = b"com.Tencent.WCDB.Config.Cipher"
XOR_MASK = bytes.fromhex("d2c7442458020000004889442450488b450048844c2448488944254048584c24")
LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
MAX_USER = 0x0000800000000000
BLOB_MAX = 1024


def _xor(data: bytes, mask: bytes) -> bytes:
    return bytes(c ^ mask[i % len(mask)] for i, c in enumerate(data))


def _readable_chunks(handle):
    CH = 32 * 1024 * 1024
    for r in winmem.enum_regions(handle):
        if r.state != MEM_COMMIT or (r.protect & (PAGE_GUARD | PAGE_NOACCESS)) or not (r.protect & 0xEE):
            continue
        off = 0
        while off < r.size:
            n = min(CH, r.size - off)
            d = winmem.read_memory(handle, r.base + off, n)
            if d:
                yield r.base + off, d
            off += max(1, n - 0x80)


def extract_keys(
    account_dir: Path,
    log: Callable[[str], None] = print,
    pid: int | None = None,
) -> dict[str, str]:
    """返回 {salt_hex: key_hex}，只含该账号下的库。"""
    # 目标库
    targets = []
    acc = None
    for a in locate.scan_accounts():
        if a.data_dir == Path(account_dir):
            acc = a
            break
    if acc is None:
        accs = locate.scan_accounts()
        if accs:
            acc = accs[0]
    if acc is None:
        raise RuntimeError("未找到微信账号数据目录")

    for p in acc.message_dbs() + [
        acc.db_path("db_storage", "contact", "contact.db"),
        acc.db_path("db_storage", "session", "session.db"),
    ]:
        if p.name.endswith(("-shm", "-wal")) or not p.exists():
            continue
        try:
            p1 = cipher.read_page1(p)
        except Exception:
            continue
        targets.append((p, p1[:16], p1))

    if pid is None:
        procs = winmem.find_processes(C.WECHAT_V4_PROCESS)
        if not procs:
            raise RuntimeError("微信未运行")
        procs.sort(key=lambda p: winmem.working_set_size(p["pid"]), reverse=True)
        pid = procs[0]["pid"]

    handle = winmem.open_process(pid)
    try:
        chunks = list(_readable_chunks(handle))
        str_addr = None
        for base, d in chunks:
            j = d.find(NAME)
            if j >= 0:
                str_addr = base + j
                break
        if str_addr is None:
            raise RuntimeError("内存里找不到 Config.Cipher 字符串，请确认微信已登录")

        pair = struct.pack("<Q", str_addr) + struct.pack("<Q", len(NAME))
        keymap: dict[str, str] = {}
        seen_blob = set()
        for base, d in chunks:
            pos = d.find(pair)
            while pos >= 0:
                node = winmem.read_memory(handle, base + pos - 0x10, 0x50)
                if node and len(node) >= 0x40 \
                        and struct.unpack("<Q", node[0x10:0x18])[0] == str_addr \
                        and struct.unpack("<Q", node[0x18:0x20])[0] == len(NAME):
                    cptr = struct.unpack("<Q", node[0x28:0x30])[0]
                    if 0x10000 <= cptr < MAX_USER:
                        obj = winmem.read_memory(handle, cptr + 0x88, 0x28)
                        if obj and len(obj) >= 0x18:
                            dptr = struct.unpack("<Q", obj[0x8:0x10])[0]
                            dlen = struct.unpack("<Q", obj[0x10:0x18])[0]
                            if 0 < dlen <= BLOB_MAX and 0x10000 <= dptr < MAX_USER and (dptr, dlen) not in seen_blob:
                                seen_blob.add((dptr, dlen))
                                blob = winmem.read_memory(handle, dptr, int(dlen))
                                if blob and len(blob) == dlen:
                                    dec = _xor(blob, XOR_MASK)
                                    for m in LITERAL_RE.finditer(dec):
                                        run = m.group(1).decode().lower()
                                        starts = [0]
                                        if len(run) > 96:
                                            starts.extend(range(0, len(run) - 63, 32))
                                            starts.append(len(run) - 64)
                                        for st in dict.fromkeys(starts):
                                            if st < 0 or st + 96 > len(run):
                                                continue
                                            kh = run[st:st + 64]
                                            sh = run[st + 64:st + 96]
                                            try:
                                                kb = bytes.fromhex(kh)
                                            except ValueError:
                                                continue
                                            if not (len(kb) == 32 and len(set(kb)) >= 15):
                                                continue
                                            for p, salt, p1 in targets:
                                                if salt.hex() == sh and cipher.validate_raw_key(p1, kb, salt):
                                                    keymap[sh] = kh
                pos = d.find(pair, pos + 1)
        return keymap
    finally:
        winmem.close_handle(handle)


def load_keys(path: Path | None = None) -> dict[str, str]:
    p = path or (C.DATA_DIR / "all_keys.json")
    if not p.exists():
        return {}
    d = json.loads(p.read_text(encoding="utf-8"))
    return d.get("keys", {}) or {}


def save_keys(keymap: dict[str, str], path: Path | None = None) -> None:
    p = path or (C.DATA_DIR / "all_keys.json")
    p.write_text(
        json.dumps({"mode": "raw", "keys": keymap, "found_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def ensure_keys(
    account_dir: Path,
    log: Callable[[str], None] = print,
    pid: int | None = None,
    refresh: bool = False,
) -> dict[str, str]:
    """取密钥（优先缓存）。返回 {salt_hex: key_hex}。"""
    cached = {} if refresh else load_keys()
    # 校验缓存是否覆盖该账号所有库
    need = []
    for a in locate.scan_accounts():
        if a.data_dir != Path(account_dir):
            continue
        for p in a.message_dbs() + [
            a.db_path("db_storage", "contact", "contact.db"),
            a.db_path("db_storage", "session", "session.db"),
        ]:
            if p.name.endswith(("-shm", "-wal")) or not p.exists():
                continue
            try:
                salt = cipher.salt_of(p).hex()
            except Exception:
                continue
            if salt not in cached:
                need.append(p)
    if not need:
        return cached
    log(f"缓存缺 {len(need)} 个库的密钥，从内存提取…")
    fresh = extract_keys(account_dir, log=log, pid=pid)
    cached.update(fresh)
    save_keys(cached)
    return cached
