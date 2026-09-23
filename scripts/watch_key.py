"""密钥守望者：持续扫描 Weixin.exe 内存，抓住微信重登瞬间派生的数据库密钥。

针对微信 4.1+（密钥不再常驻内存，只在登录/开库瞬间出现）设计。
多策略并行，命中即写盘退出。

用法：
    python -u scripts/watch_key.py [--seconds 900]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxinsight import config as C  # noqa: E402
from wxinsight.wechat import cipher, locate, winmem  # noqa: E402
from wxinsight.wechat.winmem import MEM_COMMIT, PAGE_GUARD, PAGE_NOACCESS  # noqa: E402

READABLE = 0xEE
CHUNK = 32 * 1024 * 1024
HEX64 = re.compile(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
FOUND_FILE = C.DATA_DIR / "key_found.json"


class Targets:
    """待解密的库集合。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, Path, bytes, bytes]] = []
        for acc in locate.scan_accounts():
            if acc.version != 4:
                continue
            cands = list(acc.message_dbs())
            for rel in (C.V4_CONTACT_DB, C.V4_SESSION_DB):
                p = acc.db_path(*rel.parts)
                if p.exists():
                    cands.append(p)
            for p in cands:
                if p.name.endswith(("-shm", "-wal")):
                    continue
                try:
                    p1 = cipher.read_page1(p)
                except Exception:
                    continue
                self.items.append((f"{acc.name}/{p.name}", p, p1[:16], p1))
        self.salts = {}
        for i, (name, p, salt, p1) in enumerate(self.items):
            self.salts.setdefault(salt, []).append(i)

    def match(self, key: bytes) -> tuple[int, bytes] | None:
        for salt, idxs in self.salts.items():
            i = idxs[0]
            p1 = self.items[i][3]
            try:
                if cipher.validate_key(p1, key, salt):
                    return i, salt
            except Exception:
                pass
        return None


def iter_readable(handle):
    for r in winmem.enum_regions(handle):
        if r.state != MEM_COMMIT or (r.protect & (PAGE_GUARD | PAGE_NOACCESS)) or not (r.protect & READABLE):
            continue
        off = 0
        while off < r.size:
            n = min(CHUNK, r.size - off)
            d = winmem.read_memory(handle, r.base + off, n)
            if d:
                yield r.base + off, d
            off += max(1, n - 8)


def snapshot_candidates(handle, targets: Targets, deep: bool) -> list[bytes]:
    """一轮内存扫描，收集候选密钥字节串。"""
    cands: set[bytes] = set()
    salt_hexes = {s.hex().encode() for s in targets.salts}
    salt_hex_upper = {h.upper() for h in salt_hexes}
    all_hex = salt_hexes | salt_hex_upper

    for base, d in iter_readable(handle):
        # A. x''<hex>'''   （SQL 字面量形式）
        i = -1
        while True:
            i = d.find(b"x''", i + 1)
            if i < 0:
                break
            j = i + 3
            k = j
            while k < len(d) and d[k] in b"0123456789abcdefABCDEF":
                k += 1
            ln = k - j
            if ln in (64, 96) and d[k : k + 3] == b"'''":
                try:
                    cands.add(bytes.fromhex(d[j : j + 64].decode()))
                except Exception:
                    pass

        # B. x'<hex>'  （PRAGMA key 形式）
        i = -1
        while True:
            i = d.find(b"x'", i + 1)
            if i < 0:
                break
            j = i + 2
            k = j
            while k < len(d) and d[k] in b"0123456789abcdefABCDEF":
                k += 1
            ln = k - j
            if ln in (64, 128) and d[k : k + 1] == b"'":
                try:
                    cands.add(bytes.fromhex(d[j : j + 64].decode()))
                except Exception:
                    pass

        # C. salt 的 hex 形态附近取 64 位 hex
        for sh in all_hex:
            i = -1
            while True:
                i = d.find(sh, i + 1)
                if i < 0:
                    break
                seg = d[max(0, i - 96) : i + 96]
                for m in re.finditer(rb"[0-9a-fA-F]{64}", seg):
                    try:
                        cands.add(bytes.fromhex(m.group().decode()))
                    except Exception:
                        pass
                if not deep:
                    break

        # D. 全量 64 位 hex 串（较慢，抽帧执行）
        if deep:
            for m in HEX64.finditer(d):
                try:
                    cands.add(bytes.fromhex(m.group().decode()))
                except Exception:
                    pass

    return list(cands)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=900)
    args = ap.parse_args()

    targets = Targets()
    print(f"[{time.strftime('%H:%M:%S')}] 待解密库 {len(targets.items)} 个 / salt {len(targets.salts)} 种")
    for name, p, salt, _ in targets.items[:8]:
        print(f"    {name}  salt={salt.hex()}")

    t_end = time.time() + args.seconds
    rnd = 0
    seen_pids: set[int] = set()

    while time.time() < t_end:
        rnd += 1
        deep = (rnd % 4 == 0)
        procs = winmem.find_processes(C.WECHAT_V4_PROCESS)
        if not procs:
            print(f"[{time.strftime('%H:%M:%S')}] 微信未运行，等待中…")
            time.sleep(3)
            continue
        procs.sort(key=lambda p: winmem.working_set_size(p["pid"]), reverse=True)
        pid = procs[0]["pid"]
        if pid not in seen_pids:
            seen_pids.add(pid)
            print(f"[{time.strftime('%H:%M:%S')}] 检测到 Weixin.exe #{pid}（内存 "
                  f"{winmem.working_set_size(pid)//1048576}MB）")

        try:
            handle = winmem.open_process(pid)
        except OSError as e:
            print(f"[{time.strftime('%H:%M:%S')}] 打开进程失败: {e}")
            time.sleep(3)
            continue

        try:
            t0 = time.time()
            cands = snapshot_candidates(handle, targets, deep)
            n_c = len(cands)
            hit = None
            if cands:
                with ThreadPoolExecutor(max_workers=16) as ex:
                    for key, m in zip(cands, ex.map(targets.match, cands)):
                        if m:
                            hit = (key, m)
                            break
            dt = time.time() - t0
            flag = "🔎" if deep else "  "
            print(f"[{time.strftime('%H:%M:%S')}]{flag} #{rnd} pid={pid} 候选={n_c} 用时={dt:.1f}s"
                  + ("  ⚡命中" if hit else ""))
            if hit:
                key, (idx, salt) = hit
                name = targets.items[idx][0]
                FOUND_FILE.write_text(
                    json.dumps(
                        {"key": key.hex(), "matched_db": name, "salt": salt.hex(),
                         "found_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                        ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"\n🎯🎯🎯 拿到密钥！ {key.hex()}")
                print(f"      命中库: {name}   已写入 {FOUND_FILE}")
                return 0
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] 扫描异常: {type(e).__name__}: {e}")
        finally:
            winmem.close_handle(handle)

        time.sleep(0.4)

    print(f"[{time.strftime('%H:%M:%S')}] 超时，未捕获密钥")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
