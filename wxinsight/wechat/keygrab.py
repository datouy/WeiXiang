"""从运行中的 Weixin.exe 抓取 SQLCipher 密钥。

算法（对齐 chatlog v0.0.31 key/windows/v4.go）：
  1. OpenProcess(PROCESS_VM_READ|PROCESS_QUERY_INFORMATION)
  2. 遍历私有可写、>=1MB 的内存区段
  3. 在区段内反向搜索 24 字节特征串 KEY_PATTERN
  4. 命中处往前读 8 字节作为指针；指针指向的 32 字节即候选密钥
  5. 用 message_0.db 首页做 HMAC-SHA512 校验，命中即真密钥

性能：内存里通常有 ~5k 个候选，每个候选要做一次 PBKDF2(256000)。
故先做零成本的"垃圾过滤"，再用多进程并行校验，命中即取消。
实测本机 ~5600 候选 / 12 进程 ≈ 60~90 秒（结果会缓存，只需跑一次）。
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path
from typing import Callable, Iterable

from .. import config as C
from . import cipher, winmem

_LOG = print

# ---------------------------------------------------------------------------
# 并行校验的工作进程
# ---------------------------------------------------------------------------
_W_PAGE1: bytes | None = None
_W_SALT: bytes | None = None


def _pool_init(page1: bytes, salt: bytes) -> None:
    global _W_PAGE1, _W_SALT
    _W_PAGE1 = page1
    _W_SALT = salt


def _pool_check(batch: list[bytes]) -> bytes | None:
    """在一批候选里找命中的密钥。"""
    assert _W_PAGE1 is not None and _W_SALT is not None
    for k in batch:
        try:
            if cipher.validate_key(_W_PAGE1, k, _W_SALT):
                return k
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# 候选提取
# ---------------------------------------------------------------------------
def _looks_like_key(b: bytes) -> bool:
    """零成本垃圾过滤：真密钥是 32 字节高熵随机值。"""
    if len(b) != C.KEY_SIZE:
        return False
    if b == b"\x00" * C.KEY_SIZE:
        return False
    if b[0:8] == b[8:16] == b[16:24] == b[24:32]:  # 重复块
        return False
    printable = sum(1 for x in b if 0x20 <= x <= 0x7E)
    if printable >= 30:  # 几乎全是可见字符 → 是字符串不是密钥
        return False
    return True


def collect_candidates(handle, log: Callable[[str], None] = print) -> list[bytes]:
    """返回去重后的候选密钥列表。"""
    pattern = C.KEY_PATTERN
    addrs: list[int] = []
    seen_addr: set[int] = set()
    scanned = 0
    t0 = time.time()

    for base, data in winmem.iter_readable_memory(handle):
        scanned += len(data)
        idx = len(data)
        while True:
            idx = data.rfind(pattern, 0, idx)
            if idx < 8:
                break
            a = int.from_bytes(data[idx - 8 : idx], "little")
            if C.PTR_MIN < a < C.SCAN_ADDR_MAX and a not in seen_addr:
                seen_addr.add(a)
                addrs.append(a)
            idx -= 1

    log(f"  扫描 {scanned/1048576:.0f}MB，命中候选指针 {len(addrs)} 个（{time.time()-t0:.1f}s）")

    keys: list[bytes] = []
    for a in addrs:
        d = winmem.read_memory(handle, a, C.KEY_SIZE)
        if d and _looks_like_key(d):
            keys.append(d)

    uniq = list(dict.fromkeys(keys))
    log(f"  过滤后可校验候选 {len(uniq)} 个")
    return uniq


def _parallel_validate(
    candidates: list[bytes],
    page1: bytes,
    salt: bytes,
    log: Callable[[str], None] = print,
    workers: int | None = None,
    batch: int = 16,
) -> bytes | None:
    if not candidates:
        return None
    workers = workers or max(2, min((os.cpu_count() or 4), 16))
    batches = [candidates[i : i + batch] for i in range(0, len(candidates), batch)]
    log(f"  并行校验：{len(batches)} 批 × {batch} / {workers} 进程")

    t0 = time.time()
    done = 0
    with ProcessPoolExecutor(
        max_workers=workers, initializer=_pool_init, initargs=(page1, salt)
    ) as ex:
        it = iter(batches)
        pending = set()
        for _ in range(workers * 2):
            try:
                pending.add(ex.submit(_pool_check, next(it)))
            except StopIteration:
                break
        while pending:
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in finished:
                done += 1
                try:
                    hit = fut.result()
                except Exception:
                    hit = None
                if hit is not None:
                    for p in pending:
                        p.cancel()
                    log(f"  ✅ 命中（校验 {done}/{len(batches)} 批，{time.time()-t0:.1f}s）")
                    return hit
                if done % 20 == 0:
                    log(f"    进度 {done}/{len(batches)} 批 · {time.time()-t0:.1f}s")
                try:
                    pending.add(ex.submit(_pool_check, next(it)))
                except StopIteration:
                    pass
    return None


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def extract_key(
    probe_db: Path,
    pid: int | None = None,
    log: Callable[[str], None] = print,
) -> tuple[str, int]:
    page1 = cipher.read_page1(probe_db)
    salt = page1[: C.SALT_SIZE]

    if pid is None:
        procs = winmem.find_processes(C.WECHAT_V4_PROCESS)
        if not procs:
            raise RuntimeError(f"没有找到运行中的 {C.WECHAT_V4_PROCESS}，请先登录微信")
        procs.sort(key=lambda p: winmem.working_set_size(p["pid"]), reverse=True)
    else:
        procs = [{"pid": pid, "name": C.WECHAT_V4_PROCESS, "exe": ""}]

    log("目标进程: " + ", ".join(f"{p['name']}#{p['pid']}" for p in procs))
    if not winmem.is_admin():
        log("提示: 当前非管理员权限，若读取失败请以管理员身份重试")

    for proc in procs:
        pid_ = proc["pid"]
        try:
            handle = winmem.open_process(pid_)
        except OSError as e:
            log(f"  跳过 #{pid_}: {e}")
            continue
        try:
            log(f"  正在分析 #{pid_} …")
            cands = collect_candidates(handle, log=log)
            hit = _parallel_validate(cands, page1, salt, log=log)
            if hit is not None:
                return hit.hex(), pid_
            log(f"  #{pid_} 未命中")
        finally:
            winmem.close_handle(handle)

    raise RuntimeError("所有 Weixin.exe 进程都未找到有效密钥；请确认微信已登录并保持运行")


# --------------------------------------------------------------------------
# 密钥缓存
# --------------------------------------------------------------------------
def _load_cache() -> dict:
    if C.KEY_CACHE.exists():
        try:
            return json.loads(C.KEY_CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(d: dict) -> None:
    C.KEY_CACHE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def get_key(
    account_dir: Path,
    probe_db: Path,
    pid: int | None = None,
    log: Callable[[str], None] = print,
    use_cache: bool = True,
) -> str:
    """取密钥，带磁盘缓存。缓存键 = 账号目录名 + 首页 salt。"""
    try:
        salt_hex = cipher.salt_of(probe_db).hex()
    except Exception:
        salt_hex = ""

    cache_key = f"{account_dir.name}:{salt_hex}"
    cache = _load_cache()
    if use_cache and cache_key in cache:
        key = cache[cache_key]
        if len(key) == C.KEY_SIZE * 2:
            log(f"使用缓存密钥 {key[:16]}…")
            return key
        cache.pop(cache_key, None)

    key, pid_used = extract_key(probe_db, pid=pid, log=log)
    cache[cache_key] = key
    _save_cache(cache)
    log(f"密钥已缓存: {key[:16]}…")
    return key


def clear_cache() -> None:
    if C.KEY_CACHE.exists():
        C.KEY_CACHE.unlink()
