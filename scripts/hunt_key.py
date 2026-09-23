"""密钥搜索：双模式校验 + 锚点周边海量试探。

两种密钥形态都测：
  raw  模式 —— 候选【就是】AES-256 裸密钥（只 2 轮 PBKDF2，几乎免费）→ 可以海量试探
  pass 模式 —— 候选是 passphrase，需 PBKDF2-SHA512 256000 轮派生（贵，限量）

先在大量锚点周边（±4KB）以步长 2 收集 32 字节窗口，全部跑 raw 模式；
raw 全灭再对候选做抽样 pass 模式。命中即写 data/key_found.json 并退出。
"""

from __future__ import annotations

import argparse
import json
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
LOGIN_MB = 300
WIN = 4096           # 锚点周边窗口
STEP = 2
MAX_CANDS = 900_000  # raw 模式下的候选上限

ANCHOR_NEEDLES = [b"cipher_", b"Config.Cipher", b"kdf_iter", b"PRAGMA cipher_salt"]


def entropy_ok(k: bytes) -> bool:
    if k.count(0) > 6 or len(set(k)) < 16:
        return False
    return True


class Targets:
    def __init__(self) -> None:
        items = []
        for acc in locate.scan_accounts():
            if acc.version != 4:
                continue
            prio = []
            for name in ("message_0.db", "message_1.db", "contact.db", "session.db"):
                for p in acc.message_dbs() + [
                    acc.db_path(*C.V4_CONTACT_DB.parts),
                    acc.db_path(*C.V4_SESSION_DB.parts),
                ]:
                    if p.name == name and p.exists():
                        prio.append(p)
            rest = [p for p in acc.message_dbs() if p.name not in {x.name for x in prio}]
            for p in prio + rest:
                if p.name.endswith(("-shm", "-wal")):
                    continue
                try:
                    p1 = cipher.read_page1(p)
                except Exception:
                    continue
                items.append((f"{acc.name}/{p.name}", p, p1[:16], p1))
        self.items = items
        self.n = len(items)

    # -- 校验 --------------------------------------------------------------
    def match_raw(self, key: bytes):
        for i, (name, p, salt, p1) in enumerate(self.items):
            try:
                if cipher.validate_raw_key(p1, key, salt):
                    return i, salt, "raw"
            except Exception:
                pass
        return None

    def match_pass(self, key: bytes, only: int | None = None):
        idxs = range(self.n) if only is None else [only]
        for i in idxs:
            name, p, salt, p1 = self.items[i]
            try:
                if cipher.validate_key(p1, key, salt):
                    return i, salt, "pass"
            except Exception:
                pass
        return None


def read_regions(handle):
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=2400)
    ap.add_argument("--rounds", type=int, default=40)
    args = ap.parse_args()

    tg = Targets()
    print(f"[{time.strftime('%H:%M:%S')}] 目标库 {tg.n} 个", flush=True)
    for name, p, salt, _ in tg.items[:6]:
        print(f"    {name} salt={salt.hex()}")

    t_end = time.time() + args.seconds
    for rnd in range(1, args.rounds + 1):
        if time.time() > t_end:
            break
        procs = winmem.find_processes(C.WECHAT_V4_PROCESS)
        if not procs:
            print(f"[{time.strftime('%H:%M:%S')}] #{rnd} 微信未运行，等 5s", flush=True)
            time.sleep(5)
            continue
        procs.sort(key=lambda p: winmem.working_set_size(p["pid"]), reverse=True)
        pid = procs[0]["pid"]
        mb = winmem.working_set_size(pid) // 1048576
        if mb < LOGIN_MB:
            print(f"[{time.strftime('%H:%M:%S')}] #{rnd} 主进程 {mb}MB（未登录），等 5s", flush=True)
            time.sleep(5)
            continue

        t0 = time.time()
        h = winmem.open_process(pid)
        try:
            regions = read_regions(h)
            print(f"[{time.strftime('%H:%M:%S')}] #{rnd} pid={pid} {mb}MB / "
                  f"{sum(len(d) for _,d in regions)//1048576}MB 可读", flush=True)

            # 收集锚点
            anchors: list[int] = []
            for base, d in regions:
                for salt in {t[2] for t in tg.items}:
                    i = -1
                    while True:
                        i = d.find(salt, i + 1)
                        if i < 0:
                            break
                        anchors.append(base + i)
                for nd in ANCHOR_NEEDLES:
                    i = -1
                    while True:
                        i = d.find(nd, i + 1)
                        if i < 0:
                            break
                        anchors.append(base + i)
            anchors = list(dict.fromkeys(anchors))[:3000]
            print(f"    锚点 {len(anchors)}", flush=True)

            cands: set[bytes] = set()
            for a in anchors:
                buf = winmem.read_memory(h, max(0x10000, a - WIN // 2), WIN)
                if not buf:
                    continue
                for i in range(0, len(buf) - 32, STEP):
                    k = buf[i : i + 32]
                    if entropy_ok(k):
                        cands.add(k)
                if len(cands) > MAX_CANDS:
                    print(f"    候选达到上限 {MAX_CANDS}，停止收集", flush=True)
                    break
            cl = list(cands)
            print(f"    候选 {len(cl)} · 收集 {time.time()-t0:.0f}s", flush=True)

            # ---- 第一轮：raw 模式（免费） ----
            hit = None
            t1 = time.time()
            with ThreadPoolExecutor(max_workers=16) as ex:
                for key, r in zip(cl, ex.map(tg.match_raw, cl)):
                    if r:
                        hit = (key, r)
                        break
            print(f"    raw 模式校验完成 {time.time()-t1:.0f}s" +
                  (f" → 🎯 命中 {hit[1][2]} {tg.items[hit[1][0]][0]}" if hit else " → 未命中"),
                  flush=True)

            # ---- 第二轮：pass 模式（贵，抽样 + 轮转库） ----
            if not hit:
                db_idx = (rnd - 1) % tg.n
                sample = cl[:6000]
                print(f"    pass 模式：{len(sample)} 候选 × {tg.items[db_idx][0]}", flush=True)
                t2 = time.time()
                with ThreadPoolExecutor(max_workers=16) as ex:
                    for key, r in zip(sample, ex.map(lambda k: tg.match_pass(k, db_idx), sample)):
                        if r:
                            hit = (key, r)
                            break
                print(f"    pass 模式完成 {time.time()-t2:.0f}s" +
                      (" → 🎯 命中" if hit else " → 未命中"), flush=True)

            if hit:
                key, (idx, salt, mode) = hit
                FOUND_FILE.write_text(
                    json.dumps({"key": key.hex(), "mode": mode,
                                "matched_db": tg.items[idx][0], "salt": salt.hex(),
                                "found_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                               ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"\n🎯🎯🎯 密钥到手（{mode} 模式）: {key.hex()}")
                print(f"    命中 {tg.items[idx][0]} → 已写入 {FOUND_FILE}")
                return 0
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] 异常 {type(e).__name__}: {e}", flush=True)
        finally:
            winmem.close_handle(h)
        time.sleep(1)

    print("结束，未命中")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
