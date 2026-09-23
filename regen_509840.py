"""一次性脚本：用最新代码重新生成 509840 群像谱，并就地校验 polish 是否生效。
仅离线依赖本机微信本地库；跑完即可删除。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from wxinsight import config as C  # noqa: E402
from wxinsight.wechat import extract_keys, locate, schema  # noqa: E402
from wxinsight.analysis import analyze, render  # noqa: E402


def pick_account():
    accs = [a for a in locate.scan_accounts() if a.version == 4]
    if not accs:
        return None
    keys = extract_keys.load_keys()
    cipher = __import__("wxinsight.wechat.cipher", fromlist=["cipher"])
    for a in accs:
        for p in a.message_dbs():
            try:
                salt = cipher.salt_of(p).hex()
            except Exception:
                continue
            if salt in keys:
                return a
    return accs[0]


def slug(name: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]", "_", name).strip()
    return s[:60] or "report"


def main():
    # 用法：python regen_509840.py [群名子串]，默认 509840
    want = sys.argv[1] if len(sys.argv) > 1 else "509840"

    acc = pick_account()
    if acc is None:
        print("ERR: 未找到微信账号数据目录（微信是否登录？）")
        return
    keys = extract_keys.ensure_keys(acc.data_dir, log=print)
    if not keys:
        print("ERR: 未获取到数据库密钥，请确认微信已登录")
        return
    wx = schema.WxV4(acc.data_dir, keys, log=print)

    # 找匹配 want 的会话
    target = None
    for s in wx.sessions():
        u = s["username"]
        label = wx.display_name(u, wx.contacts())
        if want in (label or "") or want in u:
            target = (u, label)
            break
    if target is None:
        print(f"ERR: 没找到名字含「{want}」的会话")
        return
    talker, label = target
    print(f"命中会话: {talker}  ({label})")

    rep = analyze.analyze(wx, talker, log=lambda m: print("  ", m))
    fname = slug(label) + "_群像谱.html"
    out = render.write_report(rep, C.OUTPUT_DIR / fname)
    print("写出:", out)

    # -------- 校验 polish --------
    print("\n=== 校验：特征词是否含纯数字 / 性格标签（前 8 人）===")
    any_digit = False
    any_praise = False
    for name, p in list((rep.get("detail") or {}).items())[:8]:
        kws = p.get("keywords") or []
        traits = [t.get("tag", "") for t in (p.get("traits") or [])]
        st = p.get("style_raw") or {}
        dig = [w for w in kws if w.isdigit()]
        if dig:
            any_digit = True
            print(f"  [数字特征词] {name}: {dig}  <- 仍含纯数字")
        if any("捧场" in t for t in traits):
            any_praise = True
            print(f"  [仍带 爱捧场] {name}: traits={traits}  neg={st.get('_neg_rate')} pos={st.get('_pos_rate')}")
        print(f"  - {name}: traits={traits} | neg={st.get('_neg_rate')} pos={st.get('_pos_rate')}")
    print()
    if not any_digit:
        print("  ✓ 特征词已无纯数字")
    if not any_praise:
        print("  ✓ 无人被误标 爱捧场（又骂又捧的改判为 互损型）")

    # 话题段样例
    topics = rep.get("topics") or []
    print(f"\n=== 话题数: {len(topics)} ===")
    for t in topics[:5]:
        print(f"  - {t.get('words')} | {t.get('heat')} | {t.get('n')} 条 / {t.get('speakers')} 人 / {t.get('pct')}%")

    conf = rep.get("conflict") or {}
    print(f"\n=== 冲突：neg_rate={conf.get('neg_rate')}% targeted_rate={conf.get('targeted_rate')}% lines={len(conf.get('lines') or [])} ===")


if __name__ == "__main__":
    main()
