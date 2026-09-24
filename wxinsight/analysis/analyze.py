"""分析编排：把微信消息聚合成「群像谱」报告数据。

纯统计 + 规则，不调用大模型。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Callable

from . import text as T
from .insight import (topic_clusters, describe_topics, conflict_map, person_style,
                      style_lines, person_traits, person_desc)
from .model import Rec
from .persona import build_personas
from .profile import group_profile
from .relations import Network, compute_roles
from .. import config as C

# 关系网入图规则：
# MIN_NODE_MSGS —— 至少发过这么多条才算「有效发言者」进关系图。
#   实测：只发过 1 条的人占全部发言者的 10~20%（多为一串 wxid_），
#   但合计只占消息量的不到 1%，留着既占位又榨不到关系。
# MAX_NODES —— 力导向图的可读上限。超出的发言者仍进名册，不丢数据。
MIN_NODE_MSGS = 2
MAX_NODES = 120


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
    return_recs: bool = False,
) -> dict:
    is_room = talker.endswith("@chatroom")

    log("加载 Name2Id / 联系人 / 群成员…")
    n2id = _name2id_map(wx)
    self_wxid = _self_wxid(wx.account_dir, n2id)
    contacts = wx.contacts()
    members = wx.room_member_names(talker) if is_room else {}
    rooms = wx.chatrooms() if is_room else {}
    owner_wxid = (rooms.get(talker) or {}).get("owner") or ""

    def resolve(raw: str) -> str:
        if not raw:
            return ""
        if raw in members and members[raw]:
            return members[raw]
        c = contacts.get(raw)
        if c:
            return c.get("remark") or c.get("nick_name") or c.get("alias") or raw
        return raw

    self_name = resolve(self_wxid)
    owner_name = resolve(owner_wxid) if owner_wxid else ""

    log(f"抽取消息 {talker}…")
    recs: list[Rec] = []
    seen_wx: set[str] = set()
    n = 0
    n_skipped = 0
    for msg in wx.iter_messages(talker, dbs=None, since=since, until=until):
        # 系统消息（进退群/拍一拍等）没有真实发言者，不参与画像与关系统计
        if msg.kind == "system":
            n_skipped += 1
            continue
        # 群聊里既无前缀又无 real_sender_id 的消息无法归因，跳过
        if is_room and not msg.sender and not msg.sender_wxid:
            n_skipped += 1
            continue
        # 优先用 real_sender_id → Name2Id 得到的 wxid 归因（比 content 前缀更可靠）
        sender = resolve(msg.sender_wxid) if msg.sender_wxid else resolve(msg.sender)
        # 记下每条消息的原始 wxid —— 后面「谁还在群里」必须按 wxid 比对，
        # 不能按昵称：有些成员在群里没设群昵称，其显示名本身就是 wxid 串，
        # 按名字比对会把「没改昵称的人」误判成退群/潜水。
        if msg.sender_wxid:
            seen_wx.add(msg.sender_wxid)
        is_self = bool(msg.sender_wxid) and msg.sender_wxid == self_wxid
        rec = Rec(
            ts=msg.ts, seq=msg.seq, sender=sender, kind=msg.kind, text=msg.text,
            local_type=msg.local_type, mentions=tuple(msg.mentions),
            quote_name=msg.quote_name, quote_text=msg.quote_text,
            quote_wxid=getattr(msg, "quote_wxid", ""), is_self=is_self,
        )
        recs.append(rec)
        n += 1
        if max_msgs and n >= max_msgs:
            break

    if n_skipped:
        log(f"跳过 {n_skipped} 条系统/无法归因消息")
    log(f"共 {n:,} 条，开始分析…")

    # 从引用回复里收割 wxid → 显示名 映射（群昵称缺失/陌生人场景的兜底）
    alias: dict[str, str] = {}
    for r in recs:
        wxid, disp = r.quote_wxid, (r.quote_name or "").strip()
        if wxid and disp and not disp.startswith("wxid_"):
            alias.setdefault(wxid, disp)
    if alias:
        for r in recs:
            if r.sender.startswith("wxid_") and r.sender in alias:
                r.sender = alias[r.sender]
        if self_wxid in alias and self_name == self_wxid:
            self_name = alias[self_wxid]
    # 自己实在没有可用的名字时，用固定标签（节点会用朱砂色标出）
    if self_name == self_wxid:
        self_name = "你自己"

    speakers: Counter = Counter(r.sender for r in recs if r.sender)
    total = len(recs)

    # 全局词频（顺带保留每条的分词结果，语义层复用，避免二次分词）
    global_df: Counter = Counter()
    docs = []
    rec_docs: list[tuple[Rec, list[str]]] = []
    for r in recs:
        if r.kind in ("text", "quote"):
            toks = T.tokenize(r.text)
            docs.append(toks)
            rec_docs.append((r, toks))
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

    roles = compute_roles(speakers, reply_in, reply_out, mention_in, mention_out, self_name, owner_name)

    # 入图名单：发言 >= MIN_NODE_MSGS 的人，发言量降序取前 MAX_NODES。
    # 名册用完整 ranked，不被这两个数截断 —— 关系图画得下多少人，和人有没有被统计到是两回事。
    node_ranked = [s for s, c in speakers.most_common()
                   if c >= MIN_NODE_MSGS or s == self_name][:MAX_NODES]
    n_graph = len(node_ranked)

    personas, ranked = build_personas(
        recs, speakers, net, roles, self_name, global_df, total,
        owner=owner_name,
        max_people=n_graph,
    )

    # 边
    edges = net.edges(min_weight=3)
    # 每人的 Top 互动关系（点节点联动用）
    attach_links(personas, net)

    # 主轴（宿敌 Top 4）
    axes = _build_axes(edges, personas, net)

    # 指令矩阵
    cmds = _build_cmds(recs, speakers)

    # 互动流向：谁常找谁（常发言区块）+ 每人的关系明细（点节点联动）
    flow = _build_flow(net, speakers)

    # 每人的聊天格式 + 性格（只有入图的人算，省时间）
    span_days = 0
    if recs:
        lo_t = min(r.ts for r in recs)
        hi_t = max(r.ts for r in recs)
        span_days = max(1, int((hi_t - lo_t) / 86400) + 1)
    act: dict[str, set[str]] = defaultdict(set)
    for r in recs:
        if r.sender:
            act[r.sender].add(r.day)
    for name in node_ranked:
        p = personas.get(name)
        if not p:
            continue
        rs = [r for r in recs if r.sender == name]
        st = person_style(rs)
        tr = person_traits(rs, st, p, len(act.get(name, ())), span_days,
                          speakers=set(speakers))
        p["style"] = style_lines(st)
        p["traits"] = tr
        p["style_raw"] = st
        rank = node_ranked.index(name) + 1 if name in node_ranked else 0
        p["desc"] = person_desc(name, p, st, tr, rank=rank, n_speakers=len(speakers))

    # 语义层：这个群在聊什么 / 谁在针对谁
    quote_rank = getattr(net, "quote_text_counter", Counter()) or Counter()
    clusters = topic_clusters(rec_docs, len(docs), quote_rank)
    topics_desc = describe_topics(clusters, len(docs))
    conflict = conflict_map(recs, net, set(speakers), quote_rank)

    # 群级档案：这个会话自己的性质 / 话题 / 节律 / 互动结构
    profile = group_profile(recs, speakers, net, edges, global_df, len(docs))

    # 改名映射
    ren = _build_ren(recs, speakers, members, contacts)

    # 时间线
    timeline = _build_timeline(recs, speakers, net)

    # 名册：全部发言者（不截断），超过入图名额的标注「未入图」
    roster = []
    for i, name in enumerate(ranked):
        p = personas.get(name, {})
        real = _real_name(name, members, contacts)
        roster.append([
            name,
            speakers.get(name, p.get("count", 0)),
            real,
            p.get("role_label", ""),
            0 if i < n_graph else 1,   # 1 = 有 profile 但因名额未入图
        ])

    # 网络节点：只有入图名单里的
    nodes = []
    for name in node_ranked:
        p = personas.get(name, {})
        nodes.append({
            "id": _sid(name),
            "n": name,
            "real": _real_name(name, members, contacts),
            "wx": _wx_of(name, members, contacts),
            "v": speakers.get(name, 0),
            "r": roles.get(name, "member"),
        })

    window = ""
    if recs:
        lo = min(r.ts for r in recs)
        hi = max(r.ts for r in recs)
        window = f"{datetime.fromtimestamp(lo):%m-%d} → {datetime.fromtimestamp(hi):%m-%d}"

    # 成员覆盖口径：一律按 wxid 比对（昵称会变，wxid 不会）。
    # left_speakers = 留过言但现在不在成员表里的人（退群 / 被踢）；
    # silent_members = 还在群里但一条消息都没有的人 —— 没有行为，所以图上不会有他。
    member_wx = set(members.keys())
    silent_members = 0
    left_speakers = 0
    if member_wx:
        silent_members = len(member_wx - seen_wx)
        left_speakers = len(seen_wx - member_wx)

    meta = {
        "talker": talker,
        "is_room": is_room,
        "name": _chat_name(wx, talker),
        "self": self_name,
        "window": window,
        "total_msgs": total,
        "speakers": len(speakers),
        "low_freq": sum(1 for c in speakers.values() if c < MIN_NODE_MSGS),
        "member_count": len(members) or 0,
        "shown_nodes": len(nodes),
        "graph_msg_cover": round(
            sum(speakers.get(n["n"], 0) for n in nodes) / (total or 1) * 100),
        "min_node_msgs": MIN_NODE_MSGS,
        "silent_members": silent_members,
        "left_speakers": left_speakers,
    }

    # 边的端点必须用节点 id（模板 JS 按 id 匹配边与节点），显示名只保留在 l/画像里；
    # 端点不在入图节点中的边直接丢弃
    id_set = {n["id"] for n in nodes}
    edges_out = [
        dict(e, a=_sid(e["a"]), b=_sid(e["b"]))
        for e in edges
        if _sid(e["a"]) in id_set and _sid(e["b"]) in id_set
    ]

    return {
        "meta": meta,
        "profile": profile,
        "flow": flow,
        "topics": topics_desc,
        "conflict": conflict,
        "nodes": nodes,
        "edges": edges_out,
        "detail": {k: v for k, v in personas.items()},
        "axes": axes,
        "cmds": cmds,
        "roster": roster,
        "ren": ren,
        "timeline": timeline,
        "_recs": recs if return_recs else None,
    }


def quick_profile(
    wx,
    talker: str,
    max_msgs: int = 20000,
    sample_texts: int = 1500,
) -> dict:
    """轻量档案：只算「这个会话是什么样的」，不做人物画像与渲染。

    用于批量扫描全部会话（746 个也能跑），统计口径与完整报告一致；
    为控制耗时，只回看最近 max_msgs 条，并只对采样文本做分词/短语提取。
    """
    is_room = talker.endswith("@chatroom")
    n2id = _name2id_map(wx)
    self_wxid = _self_wxid(wx.account_dir, n2id)
    contacts = wx.contacts()
    members = wx.room_member_names(talker) if is_room else {}

    def resolve(raw: str) -> str:
        if not raw:
            return ""
        if raw in members and members[raw]:
            return members[raw]
        c = contacts.get(raw)
        if c:
            return c.get("remark") or c.get("nick_name") or c.get("alias") or raw
        return raw

    recs: list[Rec] = []
    seen_total = 0
    # iter_messages 是时间正序，这里只保留最后 max_msgs 条（最近的）
    for msg in wx.iter_messages(talker, dbs=None):
        seen_total += 1
        if msg.kind == "system":
            continue
        if is_room and not msg.sender and not msg.sender_wxid:
            continue
        sender = resolve(msg.sender_wxid) if msg.sender_wxid else resolve(msg.sender)
        recs.append(Rec(
            ts=msg.ts, seq=msg.seq, sender=sender, kind=msg.kind, text=msg.text,
            local_type=msg.local_type, mentions=tuple(msg.mentions),
            quote_name=msg.quote_name, quote_text=msg.quote_text,
            quote_wxid=getattr(msg, "quote_wxid", ""),
            is_self=bool(msg.sender_wxid) and msg.sender_wxid == self_wxid,
        ))
        if max_msgs and len(recs) > max_msgs:
            recs.pop(0)

    if not recs:
        return {"meta": {"talker": talker, "name": _chat_name(wx, talker), "total_msgs": 0}}

    speakers: Counter = Counter(r.sender for r in recs if r.sender)
    net = Network(self_name=resolve(self_wxid))
    spk_set = set(speakers)
    for r in recs:
        net.feed(r, spk_set)

    edges = net.edges(min_weight=3)

    # 只对采样文本分词，控制批量耗时
    sampled = [r for r in recs if r.kind in ("text", "quote")][-sample_texts:]
    global_df: Counter = Counter()
    docs = []
    for r in sampled:
        toks = T.tokenize(r.text)
        docs.append(toks)
        for t in set(toks):
            global_df[t] += 1

    prof = group_profile(recs, speakers, net, edges, global_df, len(docs))
    lo, hi = min(r.ts for r in recs), max(r.ts for r in recs)
    meta = {
        "talker": talker,
        "is_room": is_room,
        "name": _chat_name(wx, talker),
        "self": resolve(self_wxid),
        "window": f"{datetime.fromtimestamp(lo):%m-%d} → {datetime.fromtimestamp(hi):%m-%d}",
        "total_msgs": len(recs),
        "seen_total": seen_total,
        "truncated": bool(max_msgs and seen_total > max_msgs),
        "speakers": len(speakers),
        "member_count": len(members) or 0,
        "last_ts": hi,
    }
    return {"meta": meta, "profile": prof, "top": [s for s, _ in speakers.most_common(8)]}


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


AXIS_TAG = {
    "foe": "对抗线",
    "ally": "同盟线",
    "real": "现实关系线",
    "trade": "交易线",
    "meme": "话题线",
}


def _build_flow(net, speakers, top_n: int = 10) -> list[dict]:
    """常发言分析：发言量前 N 的人，各自最常互动的对象是谁。

    次数 = 引用回复 + @，全部机械统计；每人的 Top 关系由
    attach_links 写进 personas['links']，供报告点节点联动展示。
    """
    out_map: dict[str, Counter] = defaultdict(Counter)
    how: dict[tuple[str, str], list[int]] = {}
    for (a, b), n in net.reply_dir.items():
        out_map[a][b] += n
        how.setdefault((a, b), [0, 0])[0] += n
    for (a, b), n in net.mention_dir.items():
        out_map[a][b] += n
        how.setdefault((a, b), [0, 0])[1] += n

    flow = []
    for s, _cnt in speakers.most_common(top_n):
        tgts = out_map.get(s)
        if not tgts:
            continue
        targets = []
        for t, n in tgts.most_common(4):
            q, m = how.get((s, t), [0, 0])
            targets.append({"to": t, "n": n, "quote": q, "mention": m})
        flow.append({"who": s, "total": sum(tgts.values()), "targets": targets})
    return flow


def attach_links(personas: dict, net) -> None:
    """把每人的 Top 互动关系写进 personas[name]['links']，带方向与代表性原话。"""
    out_map: dict[str, Counter] = defaultdict(Counter)
    in_map: dict[str, Counter] = defaultdict(Counter)
    for (a, b), n in net.reply_dir.items():
        out_map[a][b] += n
        in_map[b][a] += n
    for (a, b), n in net.mention_dir.items():
        out_map[a][b] += n
        in_map[b][a] += n
    for name, p in personas.items():
        total: Counter = Counter()
        for t, n in out_map.get(name, {}).items():
            total[t] += n
        for t, n in in_map.get(name, {}).items():
            total[t] += n
        links = []
        for t, n in total.most_common(6):
            texts = net.pair_text.get((name, t) if name <= t else (t, name), [])
            q = max(texts, key=len)[:80] if texts else ""
            links.append({
                "who": t, "n": n,
                "out": out_map.get(name, {}).get(t, 0),
                "inn": in_map.get(name, {}).get(t, 0),
                "q": q,
            })
        p["links"] = links


def _build_axes(edges, personas, net) -> list[dict]:
    """互动主线：按互动权重取前几对，不预设「争吵」叙事。

    只有词典命中为负面主导（foe）的才标「对抗线」，正常讨论为主的群
    出现的是同盟 / 话题 / 现实关系等中性标签。
    """
    axes = []
    for e in edges:
        if len(axes) >= 4:
            break
        a, b = e["a"], e["b"]
        # 找到这对之间最有代表性的一句（最长的一句原话）
        q = ""
        texts = net.pair_text.get(tuple(sorted((a, b))), [])
        if texts:
            q = max(texts, key=len)[:80]
        rel = e.get("t", "meme")
        if rel == "foe":
            why = f"互回 {e['mutual']} 次、引用 {e['reply']} 次、@ {e['mention']} 次，用词负面密度高于正面。"
        else:
            parts = []
            if e["mutual"]:
                parts.append(f"互回 {e['mutual']} 次")
            if e["reply"]:
                parts.append(f"引用 {e['reply']} 次")
            if e["mention"]:
                parts.append(f"@ {e['mention']} 次")
            why = "、".join(parts) + "。全部来自引用回复与 @ 的机械统计。"
        axes.append({
            "who": a,
            "vs": b,
            "tag": AXIS_TAG.get(rel, "互动线"),
            "n": f"{e['w']} 次互动",
            "why": why,
            "q": q,
        })
    return axes


def _build_cmds(recs, speakers) -> list[dict]:
    """统计聊天里出现的 /指令：频次 + 谁点的名（不做任何机器人判断）。"""
    cmd_count: Counter = Counter()
    cmd_users: dict[str, set[str]] = {}
    for r in recs:
        if r.kind != "text" or not r.sender:
            continue
        for m in T.CMD_RE.finditer(r.text):
            name = m.group(1)
            if len(name) >= 2:
                cmd_count[name] += 1
                cmd_users.setdefault(name, set()).add(r.sender)
    out = []
    for name, n in cmd_count.most_common(12):
        users = list(cmd_users.get(name, ()))
        out.append({
            "sl": f"/{name}",
            "n": f"{n} 次",
            "who": "、".join(users[:3]) + (" 等" if len(users) > 3 else ""),
            "note": f"{len(users)} 人使用",
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
    """按天切片：主力是谁 + 当天最有代表性的一句（优先被引用最多的）。"""
    by_day: dict[str, list[Rec]] = defaultdict(list)
    for r in recs:
        by_day[r.day].append(r)

    # 被引用次数（原话 → 被引用几次），用于挑"当天金句"
    quote_rank = getattr(net, "quote_text_counter", Counter()) or Counter()

    out = []
    for day in sorted(by_day):
        rs = by_day[day]
        c = Counter(r.sender for r in rs)
        top = c.most_common(2)
        top_s = "、".join(n for n, _ in top)
        n = len(rs)
        texts = [r.text for r in rs if r.kind == "text" and 6 <= len(r.text) <= 120]
        q = ""
        if texts:
            # 优先：当天被引用最多的那句；没人引用时退回最有信息量的一句
            q = max(texts, key=lambda t: (quote_rank.get(t.strip()[:120], 0), min(len(t), 80)))[:80]
        html = f"{n:,} 条 · 主力 {top_s}" + (f" · 「{q}」" if q else "")
        out.append([day, html, False])
    return out[-40:]
