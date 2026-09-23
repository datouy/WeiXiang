"""端到端验证：密钥 → 解密 → 会话列表 → 群消息抽取。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxinsight import config as C  # noqa: E402
from wxinsight.wechat import extract_keys, locate, schema  # noqa: E402

ACCOUNT = Path(r"D:\chat\wx\xwechat_files\wxid_m50yoq16e8ju22_1d86")


def main() -> int:
    keys = extract_keys.load_keys()
    print(f"已加载 {len(keys)} 个库密钥")

    wx = schema.WxV4(ACCOUNT, keys, log=lambda m: print("  " + m))

    print("\n=== 1. 会话列表（解密 session.db）===")
    sessions = wx.sessions()
    print(f"会话数: {len(sessions)}")
    for s in sessions[:12]:
        print(f"  {s['username'][:40]:42s}  {str(s['summary'])[:24]}  {s.get('last_sender_display_name','')}")

    print("\n=== 2. 联系人（解密 contact.db）===")
    contacts = wx.contacts()
    print(f"联系人数: {len(contacts)}")
    rooms = wx.chatrooms()
    print(f"群聊数: {len(rooms)}")

    # 找 JLog 备用群
    print("\n=== 3. 找目标群 ===")
    target = None
    for s in sessions:
        if "chatroom" in s["username"] and ("56639685459" in s["username"] or "JLog" in str(s["summary"])):
            target = s
            break
    if target is None:
        # 兜底：列所有群
        gs = [s for s in sessions if s["username"].endswith("@chatroom")]
        print(f"  未精确命中，共 {len(gs)} 个群，取前 5：")
        for s in gs[:5]:
            print(f"    {s['username']}  summary={str(s['summary'])[:30]}")
        return 0

    user = target["username"]
    print(f"目标群: {user}")
    print(f"  summary: {target['summary']}")

    print("\n=== 4. 抽取群消息（解密 message_0.db，可能较慢）===")
    n = 0
    samples = []
    for m in wx.iter_messages(user, dbs=None):
        n += 1
        if n <= 8:
            samples.append(m)
    print(f"群消息总数: {n:,}")
    for m in samples:
        print(f"  [{m.dt:%m-%d %H:%M}] {m.sender[:18]:20s} [{m.kind}] {m.text[:50]}")

    wx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
