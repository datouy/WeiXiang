"""群级档案：从某个会话自己的聊天记录里算出「这个群是什么样的」。

这里不复用任何固定文案，所有标签都附带算出它的证据数值，
因此不同群拿到的档案必然不同（话题、节律、互动结构、性质都是该群独有的）。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime

from . import text as T

# 通用口语/网络惯用语 —— 用于从高频短语里剔除，剩下才是「本群特有」的用语
GENERIC = set(
    """哈哈哈 哈哈 笑死 真的吗 什么情况 怎么办 为什么 可以啊 不是吧 好吧 好的 收到 谢谢
    你好 早上好 晚上好 在吗 在的 有没有 是这样 我觉得 我也是 不知道 没看懂 有点儿 还是
    真的是 服了 牛逼 厉害 卧槽 无语 无所谓的 没事 没事的 问题不大 稍等 了解一下
    这个 那个 怎么 如何 哪里 什么时候 多少 几个 一下 现在 今天 明天 昨天 刚才
    是这样的 你说的 我说的 对的 是的 没有 有的 算了 行吧 嗯嗯 哦哦 啊啊 诶 啊
    """.split()
)

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _gini(values: list[float]) -> float:
    """基尼系数：0=完全平均，越接近1=越集中。"""
    v = sorted(values)
    n = len(v)
    s = sum(v)
    if n == 0 or s == 0:
        return 0.0
    cum = 0.0
    g = 0.0
    for i, x in enumerate(v, 1):
        cum += x
        g += cum / s
    return 1.0 - 2.0 * (g / n) + 1.0 / n


def _hour_buckets(hours: Counter[int]) -> list[tuple[str, int]]:
    """把 24 小时聚成人能读的时段。"""
    buckets = [
        ("深夜 00-06", range(0, 6)),
        ("清晨 06-09", range(6, 9)),
        ("上午 09-12", range(9, 12)),
        ("午后 12-14", range(12, 14)),
        ("下午 14-18", range(14, 18)),
        ("晚间 18-22", range(18, 22)),
        ("夜里 22-24", range(22, 24)),
    ]
    return [(name, sum(hours.get(h, 0) for h in rng)) for name, rng in buckets]


def group_profile(
    recs,
    speakers: Counter,
    net,
    edges: list[dict],
    global_df: Counter,
    total_docs: int,
) -> dict:
    """算出一份只属于这个会话的档案。"""
    if not recs:
        return {}

    total = len(recs)
    ts_all = [r.ts for r in recs]
    lo, hi = min(ts_all), max(ts_all)
    days = max(1, int((hi - lo) / 86400) + 1)

    # ---------- 1. 内容构成 ----------
    kinds: Counter = Counter(r.kind for r in recs)
    kind_mix = [
        {"k": k, "label": _KIND_LABEL.get(k, k), "n": n, "pct": round(n / total * 100, 1)}
        for k, n in kinds.most_common(6)
    ]

    # ---------- 2. 发言集中度 / 独角戏程度 ----------
    counts = [c for _, c in speakers.most_common()]
    top1 = counts[0] if counts else 0
    top1_share = round(top1 / total * 100, 1) if total else 0.0
    top5_share = round(sum(counts[:5]) / total * 100, 1) if total else 0.0
    gini = round(_gini([float(c) for c in counts]), 3)
    # 发言过的人数 vs 总消息量：同样消息量下人越多越"网状"
    n_speakers = len(speakers)
    msgs_per_speaker = round(total / n_speakers, 1) if n_speakers else 0

    # ---------- 3. 互动度：有多少消息真的在"对着别人说" ----------
    quoted = sum(1 for r in recs if r.quote_name)
    mentioned = sum(1 for r in recs if r.mentions)
    reply_rate = round((quoted + mentioned) / total * 100, 1) if total else 0.0

    # ---------- 4. 话题（TF-IDF 近似）----------
    topics: list[str] = []
    if global_df and total_docs:
        scored: list[tuple[float, str, int]] = []
        for w, df in global_df.items():
            if len(w) < 2 or w.isdigit():
                continue
            if df < max(3, total_docs * 0.003):
                continue
            idf = math.log((total_docs + 1) / (df + 1)) + 1.0
            scored.append((df * idf, w, df))
        scored.sort(reverse=True)
        topics = [w for _, w, _ in scored[:14]]

    # ---------- 5. 本群特有口语 ----------
    phrases = T.top_phrases([r.text for r in recs if r.kind in ("text", "quote")], min_count=max(3, total // 900), top=16)
    local_slang = [p for p, n in phrases if p not in GENERIC][:8]

    # ---------- 6. 节律 ----------
    hours: Counter = Counter()
    wdays: Counter = Counter()
    by_day: Counter = Counter()
    for r in recs:
        dt = datetime.fromtimestamp(r.ts)
        hours[dt.hour] += 1
        wdays[dt.weekday()] += 1
        by_day[dt.strftime("%Y-%m-%d")] += 1
    hour_rank = sorted(_hour_buckets(hours), key=lambda x: -x[1])
    wday_rank = [(WEEKDAYS[d], n) for d, n in wdays.most_common(3)]
    avg_day = total / days
    bursts = [
        {"day": d, "n": n, "x": round(n / avg_day, 1)}
        for d, n in by_day.most_common(5)
        if avg_day > 0 and n >= avg_day * 1.8
    ]

    # ---------- 7. 互动结构 ----------
    deg: Counter = Counter()
    out_deg: Counter = Counter()
    in_deg: Counter = Counter()
    for (a, b), n in net.reply_dir.items():
        deg[a] += n
        deg[b] += n
        out_deg[a] += n
        in_deg[b] += n
    for (a, b), n in net.mention_dir.items():
        deg[a] += n
        deg[b] += n
        out_deg[a] += n
        in_deg[b] += n
    pair_space = n_speakers * (n_speakers - 1) / 2
    density = round(len(edges) / pair_space * 100, 1) if pair_space else 0.0

    core = [{"n": s, "deg": n} for s, n in deg.most_common(5)]
    # 主动找人（出度明显大于入度）vs 被人找
    chasers = sorted(
        ((s, out_deg[s], in_deg[s]) for s in set(out_deg) | set(in_deg)),
        key=lambda x: -(x[1] - x[2]),
    )[:4]
    magnets = sorted(
        ((s, in_deg[s], out_deg[s]) for s in set(out_deg) | set(in_deg)),
        key=lambda x: -(x[1] - x[2]),
    )[:4]
    quiet_line = max(3, int(total * 0.005))
    lurkers = sum(1 for _, c in speakers.items() if c <= quiet_line)

    # ---------- 8. 性质判定（每个标签都带依据数值）----------
    traits: list[dict] = []
    sticker_pct = kinds.get("sticker", 0) / total * 100
    image_pct = kinds.get("image", 0) / total * 100
    voice_pct = kinds.get("voice", 0) / total * 100

    if top1_share >= 55 and reply_rate < 12:
        traits.append({
            "tag": "单向广播",
            "why": f"最多的人占了 {top1_share}% 的发言，而只有 {reply_rate}% 的消息带 @或引用 —— 更像发通知，不是来回聊。",
        })
    elif reply_rate >= 30:
        traits.append({
            "tag": "高频互动",
            "why": f"{reply_rate}% 的消息带 @或引用回复，说明这里主要在「对着人说话」。",
        })
    elif reply_rate <= 8:
        traits.append({
            "tag": "各说各的",
            "why": f"只有 {reply_rate}% 的消息带 @或引用 —— 大家基本在自说自话，少有来回。",
        })
    if sticker_pct >= 20:
        traits.append({"tag": "表情包驱动", "why": f"表情包占了 {sticker_pct:.0f}% —— 文字之外主要靠图传意。"})
    if image_pct >= 15:
        traits.append({"tag": "图片为主", "why": f"图片消息占 {image_pct:.0f}%。"})
    if voice_pct >= 15:
        traits.append({"tag": "语音为主", "why": f"语音占 {voice_pct:.0f}%（语音内容无法读取，只统计条数）。"})
    if gini >= 0.75:
        traits.append({"tag": "少数人撑场", "why": f"发言集中度（基尼）{gini}，前 5 人占 {top5_share}% —— 话题由少数人推动。"})
    elif gini <= 0.45 and n_speakers >= 8:
        traits.append({"tag": "众人均衡", "why": f"发言集中度（基尼）{gini}，前 5 人只占 {top5_share}% —— 不是少数人的独角戏。"})
    if avg_day >= 200:
        traits.append({"tag": "高强度", "why": f"日均 {avg_day:.0f} 条，跨度 {days} 天。"})
    elif avg_day <= 5:
        traits.append({"tag": "低频群", "why": f"日均仅 {avg_day:.1f} 条 —— 偶尔才有人说一句。这套统计的样本偏薄，结论要打折看。"})
    if density >= 25:
        traits.append({"tag": "网状连接", "why": f"{n_speakers} 位发言者之间连出 {len(edges)} 条边，网络密度 {density}% —— 不是只围着一个人转。"})

    # 互动方式偏好：靠引用接话 vs 靠 @ 点名
    if quoted >= mentioned * 1.5 and quoted >= 50:
        traits.append({"tag": "爱引用接话", "why": f"引用 {quoted:,} 次 vs @ {mentioned:,} 次 —— 更习惯引用别人的话往下接。"})
    elif mentioned >= quoted * 1.5 and mentioned >= 50:
        traits.append({"tag": "爱 @ 点名", "why": f"@ {mentioned:,} 次 vs 引用 {quoted:,} 次 —— 更习惯直接点名。"})

    # 表达形态：长句 vs 碎聊，纯文字 vs 多媒体
    text_lens = [len(r.text) for r in recs if r.kind in ("text", "quote") and r.text]
    if text_lens:
        avg_len = sum(text_lens) / len(text_lens)
        text_pct = kinds.get("text", 0) / total * 100
        if avg_len <= 14 and text_pct >= 50:
            traits.append({"tag": "碎聊", "why": f"平均每条只有 {avg_len:.0f} 个字 —— 短句刷屏式聊天。"})
        elif avg_len >= 45:
            traits.append({"tag": "长文为主", "why": f"平均每条 {avg_len:.0f} 字 —— 在里面认真写长段。"})
        if text_pct >= 70:
            traits.append({"tag": "纯文字为主", "why": f"文字消息占 {text_pct:.0f}%，多媒体很少。"})
        elif text_pct <= 50:
            traits.append({"tag": "多媒体为主", "why": f"文字只占 {text_pct:.0f}%，大部分是图/表情/语音。"})

    return {
        "days": days,
        "avg_day": round(avg_day, 1),
        "kind_mix": kind_mix,
        "top1_share": top1_share,
        "top5_share": top5_share,
        "gini": gini,
        "msgs_per_speaker": msgs_per_speaker,
        "reply_rate": reply_rate,
        "quoted": quoted,
        "mentioned": mentioned,
        "topics": topics,
        "slang": local_slang,
        "hours": hour_rank,
        "wdays": wday_rank,
        "bursts": bursts,
        "density": density,
        "core": core,
        "chasers": [{"n": s, "out": o, "in": i} for s, o, i in chasers],
        "magnets": [{"n": s, "in": i, "out": o} for s, i, o in magnets],
        "lurkers": lurkers,
        "traits": traits,
    }


_KIND_LABEL = {
    "text": "文字",
    "quote": "引用回复",
    "image": "图片",
    "sticker": "表情包",
    "voice": "语音",
    "video": "视频",
    "link": "链接",
    "file": "文件",
    "card": "名片",
    "location": "位置",
    "call": "通话",
    "system": "系统",
    "other": "其他",
}
