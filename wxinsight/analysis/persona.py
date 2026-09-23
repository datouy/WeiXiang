"""人物画像：纯模板 + 规则拼装，不调用大模型。"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from . import text as T
from .model import Rec
from .relations import Network

CITIES = [
    "北京", "上海", "广州", "深圳", "杭州", "南京", "苏州", "成都", "重庆", "武汉",
    "西安", "长沙", "郑州", "天津", "青岛", "厦门", "福州", "合肥", "济南", "沈阳",
    "哈尔滨", "昆明", "南宁", "桂林", "贵阳", "南昌", "太原", "石家庄", "兰州",
    "新疆", "西藏", "内蒙古", "海南", "东莞", "佛山", "珠海", "中山", "番禺", "西丽",
    "临桂", "阳朔", "柳州", "珠海", "香港", "台湾", "澳门",
]

JOBS = [
    "程序员", "后端", "前端", "全栈", "运维", "测试", "算法", "架构师", "工程师",
    "产品经理", "运营", "设计", "ui", "UI", "销售", "采购", "老师", "教师", "医生",
    "护士", "律师", "会计", "财务", "人事", "公务员", "警察", "司机", "厨师",
    "老板", "创业者", "个体户", "开店", "厂长", "外贸", "客服", "主播", "up主",
    "学生", "研究生", "博士", "硕士", "本科", "大专", "国企", "央企", "大厂",
    "银行", "券商", "基金", "保险", "房产中介", "中介", "厂里", "工地", "外卖",
    "骑手", "快递", "网约车", "自媒体", "写代码", "搞技术", "做技术", "打工",
]

WEALTH = ["房", "房子", "首付", "房贷", "车", "车主", "提车", "奔驰", "宝马", "奥迪",
          "特斯拉", "理想", "问界", "工资", "月薪", "年薪", "存款", "基金", "股票",
          "炒股", "币", "资产", "n套房", "几套房"]

HEALTH = ["减肥", "瘦了", "胖了", "健身", "跑步", "体检", "失眠", "医院", "吃药",
          "打针", "手术", "颈椎", "腰椎", "脂肪肝", "血压", "尿酸"]

PATTERNS = [
    (re.compile(r"我(?:是|在|做|搞|干)([^，。！？\n]{2,24})"), "自述"),
    (re.compile(r"我(?:们)?(?:公司|单位|厂|店)([^，。！？\n]{0,20})"), "单位"),
    (re.compile(r"我(?:有|买了|开着|开)([^，。！？\n]{1,20})"), "持有"),
]


def _hit_in(text: str, words: list[str]) -> list[str]:
    return [w for w in words if w in text]


def build_personas(
    recs: list[Rec],
    speakers: Counter,
    network: Network,
    roles: dict[str, str],
    self_name: str,
    global_df: Counter,
    total_docs: int,
    owner: str = "",
    max_people: int = 40,
) -> tuple[dict[str, dict], list[str]]:
    """返回 (画像字典, 发言量排序的名单)。"""
    by_person: dict[str, list[Rec]] = defaultdict(list)
    for r in recs:
        if r.sender:
            by_person[r.sender].append(r)

    reply_in: Counter = Counter()
    reply_out: Counter = Counter()
    for (a, b), n in network.reply_dir.items():
        reply_in[b] += n
        reply_out[a] += n
    mention_in: Counter = Counter()
    for (a, b), n in network.mention_dir.items():
        mention_in[b] += n

    ranked = [s for s, _ in speakers.most_common()]
    total = sum(speakers.values()) or 1

    # 被引用最多的原话 → 谁说的
    quote_owner: dict[str, tuple[str, int]] = {}
    msg_index: dict[str, str] = {}
    for s, rs in by_person.items():
        for r in rs:
            key = r.text.strip()[:120]
            if len(key) >= 6:
                msg_index.setdefault(key, s)
    for qtext, n in network.quote_text_counter.most_common(400):
        owner_s = msg_index.get(qtext)
        if owner_s and (owner_s not in quote_owner or quote_owner[owner_s][1] < n):
            quote_owner[owner_s] = (qtext, n)

    personas: dict[str, dict] = {}
    for s in ranked[:max_people]:
        rs = by_person[s]
        n = len(rs)
        hour_c = Counter(r.hh for r in rs)
        tops = hour_c.most_common(3)
        kind_c = Counter(r.kind for r in rs)
        toks = [T.tokenize(r.text) for r in rs if r.kind in ("text", "quote")]
        words = T.distinctive_words(toks, global_df, total_docs, top=10)
        phrases = T.top_phrases([r.text for r in rs], min_count=max(2, n // 400), top=6)

        # 关系摘要
        best_partner = ""
        best_mutual = 0
        for (a, b), cnt in network.reply_dir.items():
            if s in (a, b):
                other = b if a == s else a
                mut = min(network.reply_dir.get((s, other), 0), network.reply_dir.get((other, s), 0))
                if mut > best_mutual:
                    best_mutual, best_partner = mut, other

        # 身份线索
        hints = _identity_hints(rs)

        role = roles.get(s, "member")
        if owner and s == owner and role != "admin":
            role = "admin"

        desc = _describe(
            s, n, total, tops, best_partner, best_mutual, words, phrases, role,
            self_name, reply_in[s], reply_out[s], mention_in[s], kind_c,
        )
        quote_msg, quote_n = quote_owner.get(s, ("", 0))
        personas[s] = {
            "name": s,
            "count": n,
            "share": round(n / total * 100, 1),
            "role": role,
            "role_label": C_ROLE.get(role, role),
            "hours": [f"{h:02d}:00" for h, _ in tops],
            "kinds": dict(kind_c.most_common(6)),
            "keywords": words[:8],
            "phrases": [p for p, _ in phrases],
            "reply_in": reply_in[s],
            "reply_out": reply_out[s],
            "mention_in": mention_in[s],
            "partner": best_partner,
            "mutual": best_mutual,
            "hints": hints,
            "quote": quote_msg,
            "quote_n": quote_n,
            "desc": desc,
            "sample": _best_sample(rs, words, network),
        }
    return personas, ranked


C_ROLE = {
    "hub": "引力中心",
    "key": "关键人物",
    "admin": "群主",
    "meme": "梗的载体",
    "self": "你自己",
    "member": "成员",
}


def _identity_hints(rs: list[Rec]) -> list[tuple[str, str]]:
    """从原话里抽身份线索：[类别, 证据句]。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    text_recs = [r for r in rs if r.kind == "text" and 4 <= len(r.text) <= 200]

    def add(cat: str, sent: str) -> None:
        key = sent[:24]
        if key in seen or len(out) >= 6:
            return
        seen.add(key)
        out.append((cat, sent))

    # 1) 自述句式
    for r in text_recs:
        for pat, cat in PATTERNS:
            m = pat.search(r.text)
            if m and len(m.group(1).strip()) >= 2:
                add(cat, r.text.strip()[:90])
                break

    # 2) 词典命中，取命中密度最高的一批
    scored: list[tuple[float, str, str]] = []
    for r in text_recs:
        t = r.text
        for cat, vocab in (
            ("城市", CITIES),
            ("职业", JOBS),
            ("资产", WEALTH),
            ("健康", HEALTH),
        ):
            hits = _hit_in(t, vocab)
            if hits:
                scored.append((len(hits) / max(8, len(t)) * 100, cat, t.strip()[:90]))
    scored.sort(reverse=True)
    used_cat: Counter = Counter()
    for _, cat, sent in scored:
        if used_cat[cat] >= 2:
            continue
        used_cat[cat] += 1
        add(cat, sent)
    return out


def _best_sample(rs: list[Rec], words: list[str], network: Network) -> str:
    """挑一句最能代表此人风格的原话：含特征词 + 长度适中。"""
    best = ("", 0.0)
    wset = set(words[:6])
    for r in rs:
        if r.kind != "text":
            continue
        t = r.text.strip()
        if not (6 <= len(t) <= 90):
            continue
        score = sum(1 for w in wset if w in t) * 2.0
        score += min(len(t), 60) / 60.0
        score -= t.count("http") * 3
        if score > best[1]:
            best = (t, score)
    return best[0]


def _describe(
    name, n, total, tops, partner, mutual, words, phrases, role,
    self_name, reply_in, reply_out, mention_in, kind_c,
) -> str:
    parts: list[str] = []
    share = n / total * 100
    parts.append(f"共 {n:,} 条，占全群 {share:.1f}%")
    if tops:
        parts.append("活跃时段 " + "、".join(f"{h:02d}:00" for h, _ in tops))
    if partner and mutual:
        parts.append(f"与「{partner}」互回最紧（{mutual} 次）")
    if reply_in:
        parts.append(f"被人引用 {reply_in} 次")
    if reply_out:
        parts.append(f"引用他人 {reply_out} 次")
    if mention_in:
        parts.append(f"被 @ {mention_in} 次")
    dom = [(k, v) for k, v in kind_c.most_common(3) if k not in ("text",)]
    if dom and dom[0][1] > n * 0.25:
        labels = {"image": "图片", "sticker": "表情", "voice": "语音", "video": "视频",
                  "link": "链接", "file": "文件", "quote": "引用回复", "system": "系统"}
        parts.append("内容以" + "、".join(labels.get(k, k) for k, _ in dom) + "为主")
    if phrases:
        parts.append("高频用语：" + "、".join(p for p, _ in phrases[:4]))
    tail = {
        "hub": "—— 全群互动的重心。",
        "key": "—— 群里绕不开的活跃角色。",
        "admin": "—— 群主。",
        "self": "—— 这是你自己。",
        "meme": "—— 更多是被别人提起。",
        "member": "",
    }.get(role, "")
    return "；".join(parts) + "。" + tail
