"""阶段一探针：定位账号 → 抓密钥 → 解密 contact.db → 打印表结构。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxinsight import config as C  # noqa: E402
from wxinsight.wechat import decrypt, keygrab, locate  # noqa: E402

import sqlite3  # noqa: E402


def main() -> int:
    print("=" * 68)
    print("1. 扫描微信账号数据目录")
    print("=" * 68)
    accounts = locate.scan_accounts()
    if not accounts:
        print("❌ 没找到任何微信数据目录")
        return 1
    for a in accounts:
        sizes = ", ".join(f"{p.name}={p.stat().st_size/1048576:.0f}MB" for p in a.files[:4])
        print(f"  - {a.name}  v{a.version}  {a.data_dir}")
        print(f"      消息库: {sizes or '(无)'}")

    acc = locate.pick_account(accounts)
    print(f"\n选中账号: {acc.name}\n")
    probe = acc.probe_db()
    if probe is None:
        print("❌ 该账号没有消息库")
        return 1

    print("=" * 68)
    print("2. 从 Weixin.exe 内存抓取密钥")
    print("=" * 68)
    t0 = time.time()
    key = keygrab.get_key(acc.data_dir, probe)
    print(f"耗时 {time.time()-t0:.1f}s，密钥 = {key}\n")

    print("=" * 68)
    print("3. 解密 contact.db 并检视表结构")
    print("=" * 68)
    contact_src = acc.db_path(*C.V4_CONTACT_DB.parts)
    if not contact_src.exists():
        print(f"❌ 找不到 {contact_src}")
        return 1
    out = decrypt.ensure_decrypted(acc.data_dir, contact_src, key)

    con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    tables = [
        r[0]
        for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    ]
    print(f"表数量: {len(tables)}")
    print("表名:", ", ".join(tables[:60]))
    for t in ("contact", "chat_room"):
        if t not in tables:
            continue
        print(f"\n--- {t} schema ---")
        print(con.execute(f"SELECT sql FROM sqlite_master WHERE name='{t}'").fetchone()[0])
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"行数: {n}")
        for row in con.execute(f"SELECT * FROM {t} LIMIT 3"):
            print("  ", str(row)[:200])

    # session 库
    sess_src = acc.db_path(*C.V4_SESSION_DB.parts)
    if sess_src.exists():
        print("\n" + "=" * 68)
        print("4. session.db 会话表")
        print("=" * 68)
        sout = decrypt.ensure_decrypted(acc.data_dir, sess_src, key)
        con2 = sqlite3.connect(f"file:{sout}?mode=ro", uri=True)
        t2 = [r[0] for r in con2.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        print("表:", ", ".join(t2))
        if "SessionTable" in t2:
            print(con2.execute("SELECT sql FROM sqlite_master WHERE name='SessionTable'").fetchone()[0])
            n = con2.execute("SELECT COUNT(*) FROM SessionTable").fetchone()[0]
            print(f"会话数: {n}")
            for row in con2.execute(
                "SELECT username, summary, last_timestamp FROM SessionTable "
                "ORDER BY sort_timestamp DESC LIMIT 10"
            ):
                print("  ", row)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
