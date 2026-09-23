"""把分析结果渲染成「群像谱」HTML 报告（复用 jlog-network.html 版式）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .analyze import MAX_NODES

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "web" / "templates" / "jlog_template.html"

DATA_START = "/* ==================== 数据 ==================== */"
DATA_END = "/* ==================== 渲染:主轴 / 指令 / 名册 / 改名 / 时间线 ==================== */"


def _sid(name: str) -> str:
    """与 analyze._sid 一致：显示名 → 节点 id（模板 JS 的 DETAIL/EDGES 都按 id 查）。"""
    import hashlib
    return hashlib.md5(name.encode("utf-8")).hexdigest()[:6]


def _build_detail(report: dict) -> dict:
    """把 personas 转成 jlog 的 DETAIL 结构 {node_id: {role, desc, facts, quote}}。

    key 必须是节点 id 而不是显示名 —— 模板 JS 用 showDetail(id) 查这个表。
    """
    kind_label = {
        "image": "图片", "sticker": "表情", "voice": "语音", "video": "视频",
        "link": "链接", "file": "文件", "quote": "引用", "system": "系统",
    }
    detail = {}
    for name, p in (report.get("detail") or {}).items():
        facts = []
        if p.get("hints"):
            facts.append(["身份线索", "；".join(f"{k}：{v}" for k, v in p["hints"][:3])])
        if p.get("hours"):
            facts.append(["活跃时段", "、".join(p["hours"][:4])])
        if p.get("keywords"):
            facts.append(["高频词", "、".join(p["keywords"][:6])])
        if p.get("traits"):
            facts.append(["性格", "、".join(t["tag"] for t in p["traits"][:5])])
        if p.get("style"):
            facts.append(["聊天格式", "；".join(p["style"][:4])])
        if p.get("partner"):
            facts.append(["最紧密", f"与「{p['partner']}」互回 {p.get('mutual', 0)} 次"])
        if p.get("kinds"):
            dom = sorted(p["kinds"].items(), key=lambda x: -x[1])[:3]
            facts.append(["内容类型", "、".join(f"{kind_label.get(k, k)}×{v}" for k, v in dom)])
        if p.get("reply_in"):
            facts.append(["被引用", f"{p['reply_in']} 次"])
        detail[_sid(name)] = {
            "role": p.get("role_label") or p.get("role") or "成员",
            "desc": p.get("desc") or "",
            "facts": facts,
            "quote": (p.get("quote") or p.get("sample") or "").strip(),
            "links": p.get("links") or [],
        }
    return detail


def render_html(report: dict) -> str:
    tpl = TEMPLATE_PATH.read_text(encoding="utf-8")
    head, rest = tpl.split(DATA_START, 1)
    _mid, tail = rest.split(DATA_END, 1)

    nodes = report.get("nodes", [])
    edges = report.get("edges", [])
    detail = _build_detail(report)
    axes = report.get("axes", [])
    cmds = report.get("cmds", [])
    roster = report.get("roster", [])
    ren = report.get("ren", [])
    tl = report.get("timeline", [])

    def js(name, obj):
        return f"const {name} = {json.dumps(obj, ensure_ascii=False)};"

    data_block = "\n".join([
        js("NODES", nodes),
        js("EDGES", edges),
        js("DETAIL", detail),
        js("AXES", axes),
        js("CMDS", cmds),
        js("ROSTER", roster),
        js("REN", ren),
        js("TL", tl),
    ])

    html = head + data_block + "\n\n" + tail

    # ---- 头部动态化 ----
    meta = report.get("meta", {})
    name = meta.get("name") or meta.get("talker") or ""
    talker = meta.get("talker") or ""
    n_member = meta.get("member_count", 0)
    total = meta.get("total_msgs", 0)
    n_speaker = meta.get("speakers", 0)

    html = re.sub(r'<div class="kicker">.*?</div>',
                  f'<div class="kicker">{name} · {talker} · {n_member} 人</div>', html, count=1, flags=re.S)
    html = re.sub(r'<h1>.*?<em>群像谱</em></h1>',
                  f'<h1>{name} <em>群像谱</em></h1>', html, count=1, flags=re.S)

    top_volume = max((n.get("v", 0) for n in nodes), default=0)
    tight = max((e.get("mutual", 0) for e in edges), default=0)
    window = meta.get("window", "")

    stats_html = (
        '<div class="stats">'
        f'<div class="stat hi"><b>{total:,}</b><span>解析出的消息条数</span></div>'
        f'<div class="stat"><b>{n_speaker}</b><span>发过言的账号</span></div>'
        f'<div class="stat"><b>{top_volume:,}</b><span>单人最高发言</span></div>'
        f'<div class="stat"><b>{tight}</b><span>最紧密一对的互回次数</span></div>'
        f'<div class="stat"><b>{window.split(" → ")[0] if window else "—"}</b><span>窗口开始日</span></div>'
        '</div>'
    )
    html = re.sub(r'<div class="stats">.*?</div>\s*</header>', stats_html + '</header>', html, count=1, flags=re.S)

    prof = report.get("profile") or {}
    trait_names = "、".join(t["tag"] for t in (prof.get("traits") or [])[:3])
    trait_txt = f'按数据看，这个会话的特征是 <b>{trait_names}</b>。' if trait_names else ""
    rate_txt = (f'{prof.get("reply_rate")}% 的消息带 @或引用' if prof else "")
    topic_txt = "、".join((prof.get("topics") or [])[:6])
    topic_txt = f'高频议题：<b>{topic_txt}</b>。' if topic_txt else ""
    lede = (
        f'<p class="lede">{window}、<b>{total:,} 条</b>记录、{n_speaker} 位发言者。{trait_txt}'
        f'{("其中 " + rate_txt + "。") if rate_txt else ""}'
        f'{topic_txt}'
        f'下图的连线全部来自<b>引用回复</b>与<b>@提及</b>的机械统计，不是印象。'
        f'点节点看画像，拖动可重排。</p>'
    )
    html = re.sub(r'<p class="lede">.*?</p>', lede, html, count=1, flags=re.S)

    # footer
    html = re.sub(r'<footer>.*?</footer>',
                  f'<footer>数据源：本机微信 4.x 本地库 · {talker}<br>由 wxinsight 生成 · {window}</footer>',
                  html, count=1, flags=re.S)

    # ---- 群档案：插入一个只属于这个会话的 section ----
    html = _inject_profile(html, report.get("profile") or {}, meta, nodes, edges)
    html = _inject_topics_conflict(html, report)

    graph_note = _graph_note(meta, nodes)
    html = re.sub(
        r'(<h2>关系网</h2>\s*)<p class="sub">.*?</p>',
        lambda m: f'{m.group(1)}<p class="sub">{graph_note}</p>',
        html, count=1, flags=re.S,
    )

    # 详情占位符「先点这四个」→ 动态 Top4（HTML 与 JS 模板串里各一处）
    top4 = " · ".join(f"<b>{n.get('n','')}</b>" for n in nodes[:4])
    html = re.sub(r'想快速看懂的话,先点这四个:</b><br>.*?</p>',
                  f'想快速看懂的话,先点这四个:</b><br>{top4}</p>', html, flags=re.S)

    # ---- 主轴：标题与导语按实际数据生成，不再预设「争吵」叙事 ----
    if axes:
        top_pair = f"{axes[0]['who']} ↔ {axes[0]['vs']}"
        axes_sub = (
            f'互动最多的一对是 <b>{top_pair}</b>（{axes[0]["n"]}）。'
            '标签按双方聊天的用词自动归类（同盟 / 话题 / 交易 / 现实关系 / 对抗），'
            '正常讨论为主的群不会出现「对抗」标签 —— 标签不是评判。'
            '<b>点上方关系图中的任意节点，这里会切换成以他为中心的主线。</b>'
        )
        axes_title = f"互动主线（前 {len(axes)} 条）"
    else:
        axes_sub = '这个会话里没有统计到足够的引用回复 / @ 互动（单对互动 ≥ 3 次才成线），因此没有可展示的主线 —— 多见于聊天频率低或以单向通知为主的会话。'
        axes_title = "互动主线"
    html = re.sub(r'<h2>四条主轴</h2>\s*<p class="sub">.*?</p>',
                  f'<h2 id="axesTitle">{axes_title}</h2><p class="sub">{axes_sub}</p>', html, count=1, flags=re.S)

    # ---- 指令矩阵 ----
    if cmds:
        cmds_sub = ('聊天记录里出现的 <code>/名字</code> 指令频次统计，附带谁在点名 —— '
                    '只做计数，不判断被点名的是不是机器人。')
    else:
        cmds_sub = '这个会话里没有统计到 <code>/指令</code> 用法。'
    html = re.sub(r'<h2>/指令 矩阵.*?</h2>\s*<p class="sub">.*?</p>',
                  f'<h2>/指令 矩阵</h2><p class="sub">{cmds_sub}</p>', html, count=1, flags=re.S)

    # ---- 发言量名册 ----
    roster_sub = (
        f'按消息条数排序（统计窗口 {window}）。「已知身份 / 定位」来自群昵称、备注与聊天原话的交叉匹配，'
        '空缺表示记录里没有可靠线索，不做猜测。'
    )
    html = re.sub(r'(<h2>发言量名册</h2>\s*)<p class="sub">.*?</p>',
                  rf'\1<p class="sub">{roster_sub}</p>', html, count=1, flags=re.S)

    # ---- 改名映射 ----
    if ren:
        ren_sub = ('引用回复里保留的是引用者通讯录里的旧备注名 —— 所以同一个 wxid 可能显示成两个名字，'
                   '下表是记录里能对上的改名证据。')
    else:
        ren_sub = '本会话记录里没有发现「旧备注名 ≠ 当前昵称」的改名证据。'
    html = re.sub(r'(<h2>改名映射</h2>\s*)<p class="sub">.*?</p>',
                  rf'\1<p class="sub">{ren_sub}</p>', html, count=1, flags=re.S)

    # ---- 时间线标题 ----
    tl_title = f'时间线 · {window}' if window else '时间线'
    html = re.sub(r'<h2>一个月时间线</h2>', f'<h2>{tl_title}</h2>', html, count=1)

    # ---- 常发言 · 谁在和谁说话（替代原「/指令 矩阵」区块）----
    flow = report.get("flow") or []
    if flow:
        flow_cards = "".join(
            f'<div class="cmd"><code class="sl">{_esc(f["who"])}</code>'
            f'<div class="arrow">最常找：' + "、".join(
                f'{_esc(t["to"])} <em>{t["n"]} 次</em>' for t in f["targets"][:3]
            ) + '</div>'
            f'<div class="note">他主动发起互动共 {_esc(f["total"])} 次（引用回复 + @ 的机械统计）</div></div>'
            for f in flow[:10]
        )
        flow_html = f'<div class="cmds">{flow_cards}</div>'
    else:
        flow_html = ('<div class="panel" style="padding:18px 19px"><p class="ph" style="margin:0">'
                     '这个会话里没有统计到足够的「对着人说话」行为（引用回复 / @），无法归纳常发言关系。</p></div>')

    cmds_block = ""
    if cmds:
        cmds_block = ('<div style="margin-top:22px"><h3 style="font-size:15px;margin:0 0 10px">'
                      '/指令 用法（该群特有的玩法，附带保留）</h3>'
                      '<div class="cmds" id="cmds"></div></div>')

    flow_section = (
        '<section><h2>常发言 · 谁在和谁说话</h2>'
        '<p class="sub">发言量靠前的人，各自最常互动的对象。次数 = 引用回复 + @，'
        '全部来自记录的机械统计 —— 换一个会话就是另一批名字。</p>'
        + flow_html + cmds_block + '</section>'
    )
    html = re.sub(r'<section>\s*<h2>/指令 矩阵.*?</section>', flow_section, html, count=1, flags=re.S)

    # ---- 互动主线：由 JS 在点选节点时切换为「以他为中心」----

    # 方法与边界 → 通用
    html = re.sub(
        r'<h3>这份图能信到什么程度</h3>.*?</ul>',
        '<h3>这份图能信到什么程度</h3><ul>'
        '<li><b>连线是量化数据，不是印象。</b>每条边的权重来自引用回复与 @ 的计数，方向双向。</li>'
        '<li><b>画像由规则拼装，不是模型生成。</b>身份线索、口头禅、金句均来自原话的统计抽取，请以原话为准。</li>'
        '<li><b>图片与语音无法读取。</b>微信图片链接带鉴权，记录里的实际内容不可见。</li>'
        '<li><b>本页只含你自己账号的本地记录。</b>未对外发布。</li>'
        '</ul>',
        html, count=1, flags=re.S)

    return html


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _graph_shape(meta: dict, nodes: list, edges: list, density=None) -> str:
    """一行说清「图里是谁」：入图人数、没被画出来的人去哪了。"""
    n_spk = meta.get("speakers", len(nodes))
    shown = len(nodes)
    member = meta.get("member_count", 0) or 0
    cover = meta.get("graph_msg_cover", 0)

    bits = [f"{n_spk} 人发言 → {shown} 人入图（覆盖 {cover}% 消息）"]
    if n_spk > shown:
        bits.append(f"{n_spk - shown} 人低频未入图")
    bits.append(f"{len(edges)} 条边")
    if density is not None:
        bits.append(f"密度 {density}%")
    if meta.get("silent_members"):
        bits.append(f"在群但从未发言 {meta['silent_members']} 人")
    if meta.get("left_speakers"):
        bits.append(f"已退群但留档 {meta['left_speakers']} 人")
    if member:
        bits.append(f"当前群成员 {member} 人")
    return " · ".join(bits)


def _graph_note(meta: dict, nodes: list) -> str:
    """关系网区块导语：如实说明图里画了谁、没画谁，以及为什么。"""
    n_spk = meta.get("speakers", len(nodes))
    shown = len(nodes)
    cover = meta.get("graph_msg_cover", 0)
    thr = meta.get("min_node_msgs", 2)

    head = ("点节点看画像、右下互动主线会跟着切换，拖动可重排。"
            "节点大小 = 发言量；连线颜色 = 关系类型。")

    if not nodes:
        return head + "这个会话没有可入图的发言者。"

    parts = []
    if n_spk > shown:
        # 分清楚没被画出来是「话说得太少」还是「名额不够」，别笼统带过
        low = meta.get("low_freq", 0)
        reason = [f'<b>{low}</b> 人发言少于 {thr} 条']
        if shown >= MAX_NODES:
            capped = n_spk - shown - low
            if capped > 0:
                reason.append(f'另有 <b>{capped}</b> 人超过 {MAX_NODES} 个节点的画图名额')
        parts.append(
            f'本群共 <b>{n_spk}</b> 人发过言，图中画出 <b>{shown}</b> 人，'
            f'他们占了全部消息的 <b>{cover}%</b>。未入图的原因：{"；".join(reason)} —— '
            f'他们<b>一条不落地列在下方「发言量名册」里</b>，消息数与互动关系照常统计，只是没画成节点。'
        )
    else:
        parts.append(
            f'本群全部 <b>{n_spk}</b> 位发言者<b>一个不落都在图上</b>，不存在抽样或截断。'
        )

    extra = []
    member = meta.get("member_count", 0) or 0
    if meta.get("silent_members"):
        extra.append(f'另有 {meta["silent_members"]} 位在群成员<b>从未发言</b>')
    elif member:
        extra.append(f'群里现有 <b>{member}</b> 位成员<b>全部都有发言记录</b>，没有漏掉的人')
    if meta.get("left_speakers"):
        extra.append(f'另有 {meta["left_speakers"]} 位发言者<b>已不在当前成员列表</b>（退群 / 被移出），他们的聊天记录仍在，所以照样有节点')
    if extra:
        parts.append("；".join(extra) + "。")

    return head + "".join(parts)


def _inject_profile(html: str, p: dict, meta: dict, nodes: list, edges: list) -> str:
    """在「关系网」前插入群档案。内容全部来自 profile 的实时统计。"""
    if not p:
        return html

    traits = p.get("traits") or []
    if traits:
        trait_html = "".join(
            f'<div class="stat"><b style="font-size:15px;font-family:var(--sans)">'
            f'{_esc(t["tag"])}</b><span>{_esc(t["why"])}</span></div>'
            for t in traits[:6]
        )
    else:
        trait_html = ('<div class="stat"><b style="font-size:15px">样本太薄</b>'
                      '<span>这个会话的数据不足以归纳出稳定的性质标签。</span></div>')

    def chips(items, sep=" · "):
        out = []
        for it in items:
            if isinstance(it, (list, tuple)):
                out.append(f"{_esc(it[0])} <span style=\"color:var(--muted)\">{_esc(it[1])}</span>")
            else:
                out.append(_esc(it))
        return sep.join(out)

    topics = p.get("topics") or []
    slang = p.get("slang") or []
    hours = p.get("hours") or []
    wdays = p.get("wdays") or []
    bursts = p.get("bursts") or []
    core = p.get("core") or []
    chasers = p.get("chasers") or []
    magnets = p.get("magnets") or []
    kind_mix = p.get("kind_mix") or []

    def rows(pairs):
        if not pairs:
            return ""
        return "".join(
            f'<div class="stat"><b style="font-size:15px;font-family:var(--sans)">'
            f'{_esc(a)}</b><span>{_esc(b)}</span></div>' for a, b in pairs
        )

    structure = rows([
        ("发言集中度", f'基尼 {p.get("gini")} · 第一人占 {p.get("top1_share")}% · 前五占 {p.get("top5_share")}%'),
        ("互动密度", f'{p.get("reply_rate")}% 的消息带 @或引用 · 引用 {p.get("quoted", 0):,} 次 · @ {p.get("mentioned", 0):,} 次'),
        ("网络形态", _graph_shape(meta, nodes, edges, p.get("density"))),
        ("节奏", f'跨度 {p.get("days")} 天 · 日均 {p.get("avg_day")} 条 · 人均 {p.get("msgs_per_speaker")} 条'),
    ])

    rhythm_bits = []
    if hours:
        rhythm_bits.append("活跃时段 " + chips([(h[0], f"{h[1]:,} 条") for h in hours[:3]]))
    if wdays:
        rhythm_bits.append("活跃星期 " + chips([(w[0], f"{w[1]:,} 条") for w in wdays]))
    if bursts:
        rhythm_bits.append("爆发日 " + chips([(b["day"], f'{b["n"]:,} 条（日均 {b["x"]} 倍）') for b in bursts[:3]]))
    rhythm = "　|　".join(rhythm_bits) if rhythm_bits else "样本不足，无法归纳节律"

    people_bits = []
    if core:
        people_bits.append("连接最多 " + chips([(c["n"], f'{c["deg"]} 次') for c in core[:4]]))
    if chasers:
        people_bits.append("主动找人 " + chips([(c["n"], f'出 {c["out"]} / 入 {c["in"]}') for c in chasers[:3]]))
    if magnets:
        people_bits.append("被人找 " + chips([(m["n"], f'入 {m["in"]} / 出 {m["out"]}') for m in magnets[:3]]))
    if p.get("lurkers"):
        people_bits.append(f'{p.get("lurkers")} 位几乎不发言')
    people = "　|　".join(people_bits) if people_bits else "互动数据太少"

    block = f"""<section>
  <h2>这个群是什么样的</h2>
  <p class="sub">下面每一项都从本会话的聊天记录里现算，换一个群就是另一套结论 —— 不是模板文案。</p>
  <div class="stats">{trait_html}</div>
  <div class="stats" style="margin-top:12px">{structure}</div>
  <div class="panel" style="margin-top:12px;padding:16px 19px;display:flex;flex-direction:column;gap:9px">
    <div><b>内容构成</b>：{chips([(k["label"], f'{k["pct"]}%') for k in kind_mix]) or "—"}</div>
    <div><b>高频议题</b>：{chips(topics) or "—"}</div>
    <div><b>本群用语</b>：{chips(slang) or "没有明显高于噪声的惯用语"}</div>
    <div><b>活跃节律</b>：{rhythm}</div>
    <div><b>人的位置</b>：{people}</div>
  </div>
</section>

"""

    return re.sub(r'<section>\s*<h2>关系网</h2>', block + r'<section><h2>关系网</h2>', html, count=1, flags=re.S)


def _escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inject_topics_conflict(html: str, report: dict) -> str:
    """在「这个群是什么样的」之后、关系网之前，插入两段真正「读得出结论」的内容：

      · 大家在聊什么 —— 话题归纳（不是词频列表）
      · 谁在针对谁   —— 攻击 / 对立线路（只统计有明確对象的负面消息）
    """
    topics = report.get("topics") or []
    conflict = report.get("conflict") or {}

    # ---------- 话题 ----------
    if topics:
        items = []
        for t in topics:
            sample = _escape(t.get("sample", ""))
            items.append(
                '<div class="topic">'
                f'<div class="t-head"><b>{_escape(t["words"])}</b>'
                f'<span class="t-heat">{_escape(t["heat"])}</span>'
                f'<span class="t-meta">{t["n"]:,} 条 · {t["speakers"]} 人 · 占 {t["pct"]}%</span></div>'
                f'<div class="t-why">{_escape(t["why"])}</div>'
                + (f'<div class="t-q">「{sample}」</div>' if sample else "")
                + '</div>'
            )
        topic_section = (
            '<section><h2>大家在聊什么</h2>'
            '<p class="sub">下面不是关键词列表 —— 是把共现的词聚成「议题」，再算规模与参与人。'
            '换一个群，这一页必然不一样。</p>'
            '<div class="topics">' + "".join(items) + '</div></section>'
        )
    else:
        topic_section = (
            '<section><h2>大家在聊什么</h2>'
            '<p class="sub">这个会话的文本量不足以归纳出稳定的议题（多半是图片 / 表情 / 通知型群）。</p></section>'
        )

    # ---------- 攻击 / 对立 ----------
    lines = conflict.get("lines") or []
    if lines:
        rows = []
        for l in lines[:6]:
            back = f'，对方回击 {l["back"]} 次' if l["back"] else ''
            sample = _escape(l.get("sample", ""))
            rows.append(
                '<div class="ax-line">'
                f'<span class="ax-p"><b>{_escape(l["who"])}</b> → <b>{_escape(l["target"])}</b></span>'
                f'<span class="ax-n">{l["n"]} 次{back} · 两人总体互回 {l["mutual"]} 次</span>'
                + (f'<div class="t-q">「{sample}」</div>' if sample else "")
                + '</div>'
            )
        atk = conflict.get("attackers") or []
        vic = conflict.get("victims") or []
        summary = (
            f'抽样文本里有 <b>{conflict.get("neg_rate", 0)}%</b> 的消息带攻击性用词，'
            f'其中 <b>{conflict.get("targeted_rate", 0)}%</b> 是直接冲着某个人去的（@或引用＋脏话）。'
        )
        if atk:
            summary += '最爱开火：' + "、".join(f'{a["n"]}（{a["cnt"]}）' for a in atk[:4]) + '。'
        if vic:
            summary += '最常被怼：' + "、".join(f'{v["n"]}（{v["cnt"]}）' for v in vic[:4]) + '。'
        conflict_section = (
            '<section><h2>谁在针对谁</h2>'
            '<p class="sub">只统计「带负面词 ＋ 明确指向某人」的消息；没有目标的口头禅不算攻击。'
            '「总体互回」高说明是互损而不是单方面霸凌。</p>'
            f'<div class="panel" style="margin-bottom:12px">{summary}</div>'
            '<div class="ax-list">' + "".join(rows) + '</div></section>'
        )
    else:
        conflict_section = (
            '<section><h2>谁在针对谁</h2>'
            '<p class="sub">这个会话里没有统计到「带负面词且指向具体人」的发言 —— 要么是和谐群，'
            '要么攻击性内容藏在图片 / 语音里读不到。</p></section>'
        )

    return re.sub(r'<section>\s*<h2>关系网</h2>',
                  topic_section + conflict_section + r'<section><h2>关系网</h2>',
                  html, count=1, flags=re.S)


def write_report(report: dict, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(report), encoding="utf-8")
    return out_path
