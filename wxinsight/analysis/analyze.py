"""分析编排：把微信消息聚合成「群像谱」报告数据。

纯统计 + 规则，不调用大模型。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Callable

from . import text as T
from .model import Rec
from .persona import build_personas
from .relations import Network, compute_roles
from .. import config as C


def _name2id_map(wx) -> dict[int, str]:
    """Name2Id: rowid -> user_name，跨所有 message 库合并。"""
    m: dict[int, str] = {}
    for dbp in wx.message_db_paths():
        try:
            out = wx.db(dbp.relative_to(wx.account_dir))
        except Exception:
            continue
        import sqlite3
        con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
        try:
            for rowid, uname in con.execute("SELECT rowid, user_name FROM Name2Id"):
                m[rowid] = uname
        except sqlite3.Error:
            pass
        con.close()
    return m


def _self_wxid(account_dir, n2id: dict[int, str]) -> str:
    """账号自身 wxid：Name2Id 里 is_session=1 且是账号目录名前缀的那个。"""
    for rowid, uname in n2id.items():
        if uname.startswith("wxid_") and account_dir.name.startswith(uname):
            return uname
    # 兜底：账号目录名去掉末尾 _xxxx
    base = account_dir.name
    m = re.match(r"^(wxid_[0-9a-zA-Z]+)_[0-9a-f]{4,}$", base)
    if m:
        return m.group(1)
    return base


def analyze(
    wx,
    talker: str,
    log: Callable[[str], None] = print,
    since: int | None = None,
    until: int | None = None,
    max_msgs: int = 0,
) -> dict:
    is_room = talker.endswith("@chatroom")

    log("加载 Name2Id / 联系人 / 群成员…")
    n2id = _name2id_map(wx)
    self_wxid = _self_wxid(wx.account_dir, n2id)
    contacts = wx.contacts()
    members = wx.room_member_names(talker) if is_room else {}

    def resolve(raw: str) -> str:
        if raw in members and members[raw]:
            return members[raw]
        c = contacts.get(raw)
        if c:
            return c.get("remark") or c.get("nick_name") or c.get("alias") or raw
        return raw

    self_name = resolve(self_wxid)

    log(f"抽取消息 {talker}…")
    recs: list[Rec] = []
    n = 0
    for msg in wx.iter_messages(talker, dbs=None, since=since, until=until):
        # 用 Name2Id 拿到真实发送者 wxid（比 content 前缀更可靠）
        sender_raw = msg.sender
        sender = resolve(sender_raw)
        # 自己：real_sender_id 映射到 self_wxid，或消息无前缀
        is_self = (msg.sender_wxid == self_wxid) or sender_raw == self_wxid
        rec = Rec(
            ts=msg.ts, seq=msg.seq, sender=sender, kind=msg.kind, text=msg.text,
            local_type=msg.local_type, mentions=tuple(msg.mentions),
            quote_name=msg.quote_name, quote_text=msg.quote_text, is_self=is_self,
        )
        recs.append(rec)
        n += 1
        if max_msgs and n >= max_msgs:
            break

    log(f"共 {n:,} 条，开始分析…")
    speakers: Counter = Counter(r.sender for r in recs if r.sender)
    total = len(recs)

    # 全局词频
    global_df: Counter = Counter()
    docs = []
    for r in recs:
        if r.kind in ("text", "quote"):
            toks = T.tokenize(r.text)
            docs.append(toks)
            for t in set(toks):
                global_df[t] += 1

    # 关系网
    net = Network(self_name=self_name)
    spk_set = set(speakers)
    for r in recs:
        net.feed(r, spk_set)

    # 回复/提及入度
    reply_in: Counter = Counter()
    reply_out: Counter = Counter()
    for (a, b), c in net.reply_dir.items():
        reply_out[a] += c
        reply_in[b] += c
    mention_in: Counter = Counter()
    mention_out: Counter = Counter()
    for (a, b), c in net.mention_dir.items():
        mention_out[a] += c
        mention_in[b] += c

    roles = compute_roles(speakers, reply_in, reply_out, mention_in, mention_out, self_name)

    personas, ranked = build_personas(
        recs, speakers, net, roles, self_name, global_df, total,
        max_people=40,
    )

    # 边
    edges = net.edges(min_weight=3)

    # 主轴（宿敌 Top 4）
    axes = _build_axes(edges, personas, net)

    # 指令矩阵
    cmds = _build_cmds(recs, speakers)

    # 改名映射
    ren = _build_ren(recs, speakers, members, contacts)

    # 时间线
    timeline = _build_timeline(recs, speakers, net)

    # 名册
    roster = []
    for name in ranked[:40]:
        p = personas.get(name, {})
        real = _real_name(name, members, contacts)
        roster.append([name, p.get("count", 0), real, p.get("role_label", "")])

    # 网络节点
    nodes = []
    for name in ranked[:40]:
        p = personas.get(name, {})
        nodes.append({
            "id": _sid(name),
            "n": name,
            "real": _real_name(name, members, contacts),
            "wx": _wx_of(name, members, contacts),
            "v": p.get("count", 0),
            "r": roles.get(name, "member"),
        })

    window = ""
    if recs:
        lo = min(r.ts for r in recs)
        hi = max(r.ts for r in recs)
        window = f"{datetime.fromtimestamp(lo):%m-%d} → {datetime.fromtimestamp(hi):%m-%d}"

    meta = {
        "talker": talker,
        "is_room": is_room,
        "name": _chat_name(wx, talker),
        "self": self_name,
        "window": window,
        "total_msgs": total,
        "speakers": len(speakers),
        "member_count": len(members) or 0,
    }

    return {
        "meta": meta,
        "nodes": nodes,
        "edges": edges,
        "detail": {k: v for k, v in personas.items()},
        "axes": axes,
        "cmds": cmds,
        "roster": roster,
        "ren": ren,
        "timeline": timeline,
    }


def _sid(name: str) -> str:
    import hashlib
    return hashlib.md5(name.encode("utf-8")).hexdigest()[:6]


def _real_name(name: str, members: dict, contacts: dict) -> str:
    for uname, disp in members.items():
        if disp == name and contacts.get(uname):
            c = contacts[uname]
            if c.get("remark") or c.get("nick_name"):
                return c.get("remark") or c.get("nick_name")
    return ""


def _wx_of(name: str, members: dict, contacts: dict) -> str:
    for uname, disp in members.items():
        if disp == name:
            return uname
    for uname, c in contacts.items():
        if (c.get("nick_name") or c.get("remark") or c.get("alias")) == name:
            return uname
    return ""


def _chat_name(wx, talker: str) -> str:
    return wx.display_name(talker, wx.contacts())


def _build_axes(edges, personas, net) -> list[dict]:
    axes = []
    for e in edges:
        if e["t"] != "foe":
            continue
        if len(axes) >= 4:
            break
        a, b = e["a"], e["b"]
        # 找到这对之间最有代表性的一句（被引用最多的）
        q = ""
        texts = net.pair_text.get(tuple(sorted((a, b))), [])
        if texts:
            q = max(texts, key=len)[:80]
        axes.append({
            "who": a,
            "vs": b,
            "tag": _tag_for_pair(a, b),
            "n": f"{e['mutual']} 次互回",
            "why": f"互回 {e['mutual']} 次、引用 {e['reply']} 次、@ {e['mention']} 次。",
            "q": q,
        })
    return axes


def _tag_for_pair(a, b):
    return f"{a} vs {b}"


def _build_cmds(recs, speakers) -> list[dict]:
    cmd_users: Counter = Counter()
    cmd_count: Counter = Counter()
    for r in recs:
        if r.kind != "text":
            continue
        for m in T.CMD_RE.finditer(r.text):
            name = m.group(1)
            if len(name) >= 2:
                cmd_count[name] += 1
                cmd_users[name] += 1
    out = []
    for name, n in cmd_count.most_common(12):
        out.append({
            "sl": f"/{name}",
            "n": f"{n} 次",
            "who": "",
            "note": "",
        })
    return out


def _build_ren(recs, speakers, members, contacts) -> list[list]:
    """改名映射：引用回复里出现的「旧备注名」≠ 当前名。"""
    pairs = []
    for r in recs:
        if r.quote_name and r.quote_text and r.quote_name != r.sender:
            # 若 quote_name 是某个 wxid 的映射，说明有改名
            if r.quote_name not in speakers:
                pairs.append((r.quote_name, r.sender))
    seen = set()
    out = []
    for old, new in pairs:
        if (old, new) in seen:
            continue
        seen.add((old, new))
        out.append([old, new, ""])
        if len(out) >= 12:
            break
    return out


def _build_timeline(recs, speakers, net) -> list[list]:
    by_day: dict[str, list[Rec]] = defaultdict(list)
    for r in recs:
        by_day[r.day].append(r)
    out = []
    for day in sorted(by_day):
        rs = by_day[day]
        c = Counter(r.sender for r in rs)
        top = c.most_common(2)
        top_s = "、".join(n for n, _ in top)
        n = len(rs)
        # 当日金句：被引用最多的一句
        q = ""
        texts = [r.text for r in rs if r.kind == "text" and 6 <= len(r.text) <= 120]
        if texts:
            q = max(texts, key=len)[:80]
        html = f"{n:,} 条 · 主力 {top_s}" + (f" · 「{q}」" if q else "")
        out.append([day, html, False])
    return out[-40:]
