"""把分析结果渲染成「群像谱」HTML 报告（复用 jlog-network.html 版式）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "web" / "templates" / "jlog_template.html"

DATA_START = "/* ==================== 数据 ==================== */"
DATA_END = "/* ==================== 渲染:主轴 / 指令 / 名册 / 改名 / 时间线 ==================== */"


def _build_detail(report: dict) -> dict:
    """把 personas 转成 jlog 的 DETAIL 结构 {name: {role, desc, facts, quote}}。"""
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
        if p.get("partner"):
            facts.append(["最紧密", f"与「{p['partner']}」互回 {p.get('mutual', 0)} 次"])
        if p.get("kinds"):
            dom = sorted(p["kinds"].items(), key=lambda x: -x[1])[:3]
            facts.append(["内容类型", "、".join(f"{kind_label.get(k, k)}×{v}" for k, v in dom)])
        if p.get("reply_in"):
            facts.append(["被引用", f"{p['reply_in']} 次"])
        detail[name] = {
            "role": p.get("role_label") or p.get("role") or "成员",
            "desc": p.get("desc") or "",
            "facts": facts,
            "quote": (p.get("quote") or p.get("sample") or "").strip(),
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

    lede = (
        f'<p class="lede">{window}、<b>{total:,} 条</b>记录、{n_speaker} 位发言者。'
        f'下图的连线全部来自<b>引用回复</b>与<b>@提及</b>的机械统计，不是印象。'
        f'点节点看画像，拖动可重排。</p>'
    )
    html = re.sub(r'<p class="lede">.*?</p>', lede, html, count=1, flags=re.S)

    # footer
    html = re.sub(r'<footer>.*?</footer>',
                  f'<footer>数据源：本机微信 4.x 本地库 · {talker}<br>由 wxinsight 生成 · {window}</footer>',
                  html, count=1, flags=re.S)

    # 详情占位符「先点这四个」→ 动态 Top4
    top4 = " · ".join(f"<b>{n.get('n','')}</b>" for n in nodes[:4])
    html = re.sub(r'想快速看懂的话,先点这四个:</b><br>.*?</p>',
                  f'想快速看懂的话,先点这四个:</b><br>{top4}</p>', html, count=1, flags=re.S)

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


def write_report(report: dict, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_html(report), encoding="utf-8")
    return out_path
