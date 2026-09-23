"""重火力密钥搜索：等微信登录完成后，在密钥可能存在的结构周边穷举 32 字节候选，多线程 PBKDF2 校验。

与 watch_key.py 互补：
  watch_key.py  只做廉价字面量扫描（快，抓登录瞬间）
  本脚本        做区域性穷举（慢，但覆盖"密钥以裸 32 字节存在"的情况）
"""

from __future__ import annotations

import json
import struct
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
FOUND_FILE = C.DATA_DIR / "key_found.json"
LOGIN_MB = 400          # 主进程内存超过这个值 = 已登录
MAX_CANDS = 26000       # 单轮候选上限
STEP = 4                # 候选窗口步长


def entropy_ok(k: bytes) -> bool:
    if k.count(0) > 6:
        return False
    if len(set(k)) < 16:
        return False
    return True


class Targets:
    def __init__(self) -> None:
        self.items = []
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

    def match(self, key: bytes):
        for i, (name, p, salt, p1) in enumerate(self.items):
            try:
                if cipher.validate_key(p1, key, salt):
                    return i, salt
            except Exception:
                pass
        return None


def read_all(handle):
    out = []
    for r in winmem.enum_regions(handle):
        if r.state != MEM_COMMIT or (r.protect & (PAGE_GUARD | PAGE_NOACCESS)) or not (r.protect & READABLE):
            continue
        off = 0
        while off < r.size:
            n = min(CHUNK, r.size - off)
            d = winmem.read_memory(handle, r.base + off, n)
            if d:
                out.append((r.base + off, d))
            off += max(1, n - 8)
    return out


def main(rounds: int = 6) -> int:
    tg = Targets()
    print(f"[{time.strftime('%H:%M:%S')}] 目标库 {len(tg.items)} 个")
    salts = [t[2] for t in tg.items]

    for rnd in range(1, rounds + 1):
        procs = winmem.find_processes(C.WECHAT_V4_PROCESS)
        if not procs:
            print("微信未运行，等 5s")
            time.sleep(5)
            continue
        procs.sort(key=lambda p: winmem.working_set_size(p["pid"]), reverse=True)
        pid = procs[0]["pid"]
        mb = winmem.working_set_size(pid) // 1048576
        if mb < LOGIN_MB:
            print(f"[{time.strftime('%H:%M:%S')}] #{rnd} 主进程仅 {mb}MB，尚未登录，等 5s")
            time.sleep(5)
            continue

        print(f"[{time.strftime('%H:%M:%S')}] #{rnd} 主进程 {mb}MB → 开始区域穷举")
        h = winmem.open_process(pid)
        try:
            regions = read_all(h)
            total = sum(len(d) for _, d in regions)
            print(f"    读取 {total//1048576}MB / {len(regions)} 块")

            anchors: list[int] = []
            for base, d in regions:
                for salt in salts:
                    i = -1
                    while True:
                        i = d.find(salt, i + 1)
                        if i < 0:
                            break
                        anchors.append(base + i)
                if len(anchors) > 4000:
                    break
            print(f"    salt 锚点 {len(anchors)}")

            cands: set[bytes] = set()
            for a in anchors:
                buf = winmem.read_memory(h, max(0x10000, a - 1024), 2048)
                if not buf:
                    continue
                for i in range(0, len(buf) - 32, STEP):
                    k = buf[i : i + 32]
                    if entropy_ok(k):
                        cands.add(k)
            print(f"    锚点周边候选 {len(cands)}")

            # Config.Cipher 注册表对象
            cfg = 0x7FFE10956AB8 if any(True for _ in ()) else None
            try:
                hits = []
                needle = None
                for base, d in regions:
                    if b"com.Tencent.WCDB.Config.Cipher" in d:
                        j = d.find(b"com.Tencent.WCDB.Config.Cipher")
                        needle = struct.pack("<Q", base + j)
                        break
                if needle:
                    for base, d in regions:
                        i = -1
                        while len(hits) < 120:
                            i = d.find(needle, i + 1)
                            if i < 0:
                                break
                            hits.append((base + i))
                    for e in hits:
                        buf = winmem.read_memory(h, e, 128)
                        if not buf:
                            continue
                        for slot in range(16, 120, 8):
                            if slot + 8 > len(buf):
                                break
                            p = struct.unpack("<Q", buf[slot : slot + 8])[0]
                            if not (0x10000 < p < 0x7FFFFFFFFFFF):
                                continue
                            obj = winmem.read_memory(h, p, 512)
                            if not obj:
                                continue
                            for i in range(0, len(obj) - 32, STEP):
                                k = obj[i : i + 32]
                                if entropy_ok(k):
                                    cands.add(k)
                    print(f"    + Config.Cipher 对象候选后共 {len(cands)}")
            except Exception as ex:
                print(f"    Config.Cipher 分支异常 {ex}")

            cl = list(cands)[:MAX_CANDS]
            print(f"    候选 {len(cands)} → 校验 {len(cl)}（16 线程）")
            t0 = time.time()
            hit = None
            with ThreadPoolExecutor(max_workers=16) as ex:
                for n, (key, m) in enumerate(zip(cl, ex.map(tg.match, cl)), 1):
                    if m:
                        hit = (key, m)
                        break
                    if n % 2000 == 0:
                        print(f"      {n}/{len(cl)} · {time.time()-t0:.0f}s")
            if hit:
                key, (idx, salt) = hit
                FOUND_FILE.write_text(
                    json.dumps({"key": key.hex(), "matched_db": tg.items[idx][0],
                                "salt": salt.hex(),
                                "found_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                               ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"\n🎯🎯🎯 拿到密钥！{key.hex()}  命中 {tg.items[idx][0]}")
                return 0
            print(f"[{time.strftime('%H:%M:%S')}] #{rnd} 本轮无命中（{time.time()-t0:.0f}s）")
        finally:
            winmem.close_handle(h)
        time.sleep(3)

    print("穷举结束，未命中")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
