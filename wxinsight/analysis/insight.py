"""语义层：把「统计出来的数」翻译成「关于这群人的结论」。

profile / persona 负责算出有多少条、多少百分比；这里负责回答四个问题：
  1. 他们到底在聊什么  —— 话题归纳（不是词频列表）
  2. 谁在针对谁        —— 攻击 / 对立线路
  3. 这个人什么性格    —— 多维行为倾向，每条都带证据
  4. 这个人怎么打字    —— 聊天格式（句长 / 标点 / 表情 / 连发…）

全部离线规则，不调用大模型。所有结论必须能追到具体数字或原话，
写不出证据的标签宁可不输出 —— 这是它和「数据复读机」的区别。
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from statistics import mean

from . import text as T

# --------------------------------------------------------------------------
# 1. 话题归纳
# --------------------------------------------------------------------------

_MIN_KEY_DF_RATIO = 0.004      # 一个词至少要在这么多比例的消息里出现
_KEYWORD_POOL = 70             # 参与聚类的候选词规模
_MAX_TOPICS = 6


def topic_clusters(rec_docs: list, total_docs: int, quote_rank: Counter | None = None):
    """把「高频词」聚成「议题」，每条给出参与人、规模与代表性原话。

    rec_docs: [(Rec, tokens)] —— 复用 analyze 已经分好词的结果，避免二次分词。
    """
    if not rec_docs:
        return []

    # --- 候选词：全局加权（频次 × 逆文档频率） ---
    df: Counter = Counter()
    for _r, toks in rec_docs:
        for t in set(toks):
            df[t] += 1
    min_df = max(3, int(total_docs * _MIN_KEY_DF_RATIO))
    scored: list[tuple[float, str, int]] = []
    for w, c in df.items():
        if len(w) < 2 or w.isdigit() or c < min_df:
            continue
        idf = math.log((total_docs + 1) / (c + 1)) + 1.0
        scored.append((c * idf, w, c))
    if not scored:
        return []
    scored.sort(reverse=True)
    pool = [(w, c) for _, w, c in scored[:_KEYWORD_POOL]]
    kw_df = dict(pool)
    kw_idx = {w: i for i, (w, _c) in enumerate(pool)}

    # --- 共现矩阵（只算候选词之间，且有上限，防止长消息爆炸） ---
    co: dict[tuple[str, str], int] = defaultdict(int)
    for _r, toks in rec_docs:
        ks = sorted({t for t in toks if t in kw_idx})
        if len(ks) < 2:
            continue
        if len(ks) > 12:
            ks = ks[:12]
        for i in range(len(ks)):
            for j in range(i + 1, len(ks)):
                co[(ks[i], ks[j])] += 1

    # --- 贪心聚类：按权重从高到低，拉入共现足够强的词 ---
    assigned: dict[str, int] = {}
    clusters: list[list[str]] = []
    for w, _df_w in pool:
        if w in assigned:
            continue
        cid = len(clusters)
        members = [w]
        assigned[w] = cid
        for x, _df_x in pool:
            if x in assigned:
                continue
            c = co.get((w, x) if w < x else (x, w), 0)
            if c < 3:
                continue
            # 归一化共现：两个词一起出现的频率，要能盖过各自单独出现的频率
            if c / math.sqrt(kw_df[w] * kw_df[x]) >= 0.16:
                members.append(x)
                assigned[x] = cid
        clusters.append(members)

    # --- 把消息分配到议题，统计规模 / 参与人 / 代表句 ---
    out = []
    for members in clusters:
        mset = set(members)
        hits = [(r, toks) for r, toks in rec_docs if mset & set(toks)]
        if len(hits) < 5:
            continue
        senders = Counter(r.sender for r, _t in hits if r.sender)
        # 代表句：命中该议题词最多、长度适中、最好是被引用过的那句
        best = None
        best_score = -1.0
        for r, toks in hits:
            t = _strip_quote_prefix(r.text)
            if not (8 <= len(t) <= 90) or T.is_xml_noise(t):
                continue
            n_hit = len(mset & set(toks))
            s = n_hit * 3.0 + min(len(t), 60) / 60.0
            if quote_rank:
                s += quote_rank.get(t[:120], 0) * 2.0
            if s > best_score:
                best_score, best = s, t
        top_words = sorted(members, key=lambda w: -kw_df[w])[:3]
        out.append({
            "words": top_words,
            "all_words": members[:8],
            "msgs": len(hits),
            "speakers": len(senders),
            "top": [s for s, _n in senders.most_common(3)],
            "sample": best or "",
        })

    out.sort(key=lambda c: -c["msgs"])
    return out[:_MAX_TOPICS]


def describe_topics(clusters: list, total_msgs: int) -> list[dict]:
    """给每个议题写一句人话；不同群的数据不同，这句话必然不同。"""
    if not clusters:
        return []
    out = []
    biggest = clusters[0]["msgs"] or 1
    for idx, c in enumerate(clusters):
        w = "、".join(c["words"])
        rel = c["msgs"] / biggest
        if idx == 0:
            heat = "最热的一条线"
        elif rel > 0.45:
            heat = "主线之一"
        else:
            heat = "支线"
        drivers = "、".join(c["top"][:3])
        who = f"主要由 {drivers} 推动" if drivers else ""
        spread = (
            f"{c['speakers']} 人参与" if c["speakers"] > 1 else "基本是一个人在说"
        )
        out.append({
            "words": w,
            "heat": heat,
            "n": c["msgs"],
            "pct": round(c["msgs"] / max(1, total_msgs) * 100, 1),
            "speakers": c["speakers"],
            "top": c["top"],
            "sample": c["sample"],
            "why": f"围绕「{w}」{spread}（{c['msgs']:,} 条），{who}。",
        })
    return out


# --------------------------------------------------------------------------
# 2. 攻击 / 对立识别
# --------------------------------------------------------------------------

def _neg_hits(msg: str) -> list[str]:
    low = msg.lower()
    return [w for w in T.NEG_WORDS if w.lower() in low]


def _pos_hits(msg: str) -> list[str]:
    low = msg.lower()
    return [w for w in T.POS_WORDS if w.lower() in low]


def _praise_stats(rs, speakers: set[str]) -> tuple[int, int]:
    """某人「含夸奖词的消息」总数，以及其中「明确指向具体他人」的条数。

    用来区分「真捧场」vs「行话/公告/感叹」：真捧场是抬举「人」——
    用 @ 或引用钉住某个具体群友；而活动公告模板（含「可以」等）、游戏感叹
    （「高手高手！」）、日常应答里的夸奖词并不指向任何人。只看词频会把发公告、
    喊口号的人误标成「爱捧场」，所以必须过方向这一关。
    """
    pos_msgs = 0
    directed = 0
    for r in rs:
        if r.kind not in ("text", "quote") or not r.text:
            continue
        if not _pos_hits(r.text):
            continue
        pos_msgs += 1
        targets = set(r.mentions or ())
        if r.quote_name:
            targets.add(r.quote_name)
        real = set()
        for t in targets:
            c = _canon_target(t, speakers)
            if c and c != r.sender:
                real.add(c)
        if real:
            directed += 1
    return pos_msgs, directed


# [强] / [偷笑] 这类是表情占位符不是人话，混进格式与复读统计会把结论带偏
PLACEHOLDER_RE = re.compile(r"^\s*\[[^\]]{1,10}\]\s*$")


def _clean_texts(rs) -> list[str]:
    """只留真正的文字消息，剔除表情占位符与 XML 残留。"""
    out = []
    for r in rs:
        if r.kind not in ("text", "quote") or not r.text:
            continue
        t = (r.text or "").strip()
        if not t or PLACEHOLDER_RE.match(t) or T.is_xml_noise(t):
            continue
        out.append(t)
    return out


# 「@所有人」不是针对某个人；其余带 XML 残渣的也不是
_NOISE_TARGET = re.compile(r"[/<>]|fromusr|msgsource|svrid|^\{\}|chatroom", re.I)
_GENERIC_TARGETS = {"所有人", "群主", "大家", ""}


def _canon_target(name: str, speakers: set[str]) -> str:
    """把 @ 出来的残缺昵称还原成完整发言者。

    XML 里的 talker 属性遇到昵称带空格时会被截断（「闲づ 菜瓜」只剩「闲づ」），
    这里按唯一前缀匹配还原；找不到唯一人选就保留原样，宁可不确定也不猜。
    """
    if not name or name in _GENERIC_TARGETS:
        return ""
    if _NOISE_TARGET.search(name):
        return ""
    if name in speakers:
        return name
    cands = [s for s in speakers if s == name or s.startswith(name + " ") or name.startswith(s + " ")]
    if len(cands) == 1:
        return cands[0]
    # 「@张三 李四」这种连着两个昵称的情况，取第一个能对上的
    first = name.split()[0]
    cands = [s for s in speakers if s.startswith(first) and len(first) >= 2]
    if len(cands) == 1:
        return cands[0]
    return ""


def conflict_map(recs, net, speakers: set[str], quote_rank: Counter | None = None) -> dict:
    """谁在针对谁：只有「带负面词 + 明确指向对象」的消息才算针对性攻击。

    明确指向 = @到某人，或直接引用了某人的话。没有目标的不算，避免把
    群里的口头禅误判成骂人。
    """
    directed: Counter = Counter()          # (攻方, 受方) -> 次数
    evidence: dict[tuple[str, str], str] = {}
    attacks_by: Counter = Counter()        # 攻方 -> 次数
    hits_by: Counter = Counter()           # 受方 -> 次数
    neg_msgs = 0
    neg_targeted = 0
    text_total = 0

    for r in recs:
        if r.kind not in ("text", "quote") or not r.text:
            continue
        text_total += 1
        words = _neg_hits(r.text)
        if not words:
            continue
        neg_msgs += 1
        targets = set(r.mentions or ())
        if r.quote_name:
            targets.add(r.quote_name)
        targets = {_canon_target(t, speakers) for t in targets}
        targets.discard("")
        targets.discard(r.sender)
        if not targets:
            continue
        neg_targeted += 1
        for t in targets:
            directed[(r.sender, t)] += 1
            attacks_by[r.sender] += 1
            hits_by[t] += 1
            key = (r.sender, t)
            cand = _strip_quote_prefix(r.text)[:80]
            if key not in evidence or len(cand) > len(evidence[key]):
                evidence[key] = cand
            if not evidence[key]:
                evidence[key] = _strip_quote_prefix(r.text)[:80]

    if text_total == 0:
        return {"lines": [], "neg_rate": 0.0, "targeted_rate": 0.0}

    lines = []
    for (a, b), n in directed.most_common(12):
        if n < 2:
            continue
        lines.append({
            "who": a, "target": b, "n": n,
            "back": directed.get((b, a), 0),
            "mutual": net.reply_dir.get((a, b), 0) + net.reply_dir.get((b, a), 0),
            "sample": evidence.get((a, b), ""),
        })

    return {
        "lines": lines[:8],
        "attackers": [{"n": s, "cnt": c} for s, c in attacks_by.most_common(6)],
        "victims": [{"n": s, "cnt": c} for s, c in hits_by.most_common(6)],
        "neg_rate": round(neg_msgs / text_total * 100, 2),
        "targeted_rate": round(neg_targeted / text_total * 100, 2),
        "neg_msgs": neg_msgs,
        "text_total": text_total,
    }


# --------------------------------------------------------------------------
# 3 & 4. 个人：聊天格式 + 性格
# --------------------------------------------------------------------------

EXCLAM_RE = re.compile(r"[!！]+")
QUEST_RE = re.compile(r"[?？]+")
ELLIP_RE = re.compile(r"(?:\.{3,}|…+|~+)")
EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u2190-\u21FF]"
)
REPEAT_RE = re.compile(r"(.)\1{2,}")
URL_RE = re.compile(r"https?://")


QUOTE_PREFIX_RE = re.compile(r"^\s*\[引用[^\]]{0,40}\]\s*")


CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _strip_quote_prefix(msg: str) -> str:
    """去掉引用回复自带的「[引用 某某]」前缀，让证据句本身可读。"""
    t = (msg or "").strip()
    # 引用内容本身可能是 XML 残渣，优先保留「被引用原文」之后那部分（即本人的话）
    parts = QUOTE_PREFIX_RE.split(t)
    best = ""
    for cand in (t, *parts):
        cand = cand.strip()
        # 证据句必须看得懂：至少 4 个中文字，且不是 XML 渣子
        if len(CJK_RE.findall(cand)) >= 4 and not T.is_xml_noise(cand):
            if len(cand) > len(best):
                best = cand
    return best


def person_style(rs) -> dict:
    """这个人的「打字方式」—— 全部从他自己的消息里算，不做跨人假设。"""
    texts = _clean_texts(rs)
    total_msgs = len(rs)
    out: dict = {"n_text": len(texts), "n_msg": total_msgs}

    kind_c = Counter(r.kind for r in rs)
    labels = {"image": "图片", "sticker": "表情", "voice": "语音", "video": "视频",
              "link": "链接", "file": "文件"}
    out["kind_mix"] = [
        {"label": labels.get(k, k), "pct": round(v / total_msgs * 100, 1)}
        for k, v in kind_c.most_common(4) if k in labels
    ]

    if not texts:
        out["no_text"] = True
        return out

    lens = [len(t) for t in texts]
    out["avg_len"] = round(mean(lens), 1)
    out["max_len"] = max(lens)
    out["long_rate"] = round(sum(1 for x in lens if x >= 50) / len(lens) * 100, 1)
    out["short_rate"] = round(sum(1 for x in lens if x <= 8) / len(lens) * 100, 1)
    out["exclam_rate"] = round(sum(1 for t in texts if EXCLAM_RE.search(t)) / len(texts) * 100, 1)
    out["quest_rate"] = round(sum(1 for t in texts if QUEST_RE.search(t)) / len(texts) * 100, 1)
    out["ellip_rate"] = round(sum(1 for t in texts if ELLIP_RE.search(t)) / len(texts) * 100, 1)
    out["emoji_rate"] = round(sum(1 for t in texts if EMOJI_RE.search(t)) / len(texts) * 100, 1)
    out["repeat_rate"] = round(sum(1 for t in texts if REPEAT_RE.search(t)) / len(texts) * 100, 1)
    out["url_rate"] = round(sum(1 for t in texts if URL_RE.search(t)) / len(texts) * 100, 1)
    out["multiline_rate"] = round(sum(1 for t in texts if "\n" in t) / len(texts) * 100, 1)

    # 复读 / 连发：完全重复的话，以及同一分钟内连着刷的条数
    dup = Counter(t for t in texts if len(t) >= 2)
    out["dup_top"] = dup.most_common(1)[0] if dup else ("", 0)

    burst_max = 1
    run = 1
    prev = None
    for r in rs:
        if prev is not None and r.ts - prev <= 60:
            run += 1
            burst_max = max(burst_max, run)
        else:
            run = 1
        prev = r.ts
    out["burst_max"] = burst_max

    # 情绪密度（供 person_desc 引用，量纲与其他指标一致：每百条几次）
    out["_neg_rate"] = round(sum(len(_neg_hits(t)) for t in texts) / len(texts) * 100, 2)
    out["_pos_rate"] = round(sum(len(_pos_hits(t)) for t in texts) / len(texts) * 100, 2)

    return out


def style_lines(st: dict) -> list[str]:
    """把格式指标写成几句能读的话。"""
    out: list[str] = []
    if st.get("no_text"):
        return ["纯多媒体发言，没有可分析的文字。"]

    n = st.get("n_text", 0)
    avg = st["avg_len"]
    if avg <= 12:
        out.append(f"话说得很短，平均每条 {avg} 个字")
    elif avg >= 40:
        out.append(f"爱写长段，平均每条 {avg} 个字")
    else:
        out.append(f"平均每条 {avg} 个字")
    if st["long_rate"] >= 15:
        out.append(f"{st['long_rate']}% 的发言超过 50 字（最长 {st['max_len']} 字）")
    if st["short_rate"] >= 45:
        out.append(f"{st['short_rate']}% 是 8 字以内的短句")

    punct = []
    if st["exclam_rate"] >= 15:
        punct.append(f"感叹号 {st['exclam_rate']}%")
    if st["quest_rate"] >= 15:
        punct.append(f"问号 {st['quest_rate']}%")
    if st["ellip_rate"] >= 10:
        punct.append(f"省略号/拖音 {st['ellip_rate']}%")
    if st["repeat_rate"] >= 20:
        punct.append(f"叠字拉长音 {st['repeat_rate']}%")
    if punct:
        out.append("标点习惯：" + "、".join(punct))

    if st["emoji_rate"] >= 20:
        out.append(f"emoji 使用率 {st['emoji_rate']}%")
    if st.get("kind_mix"):
        top = st["kind_mix"][0]
        if top["pct"] >= 20:
            out.append(f"{top['label']}占 {top['pct']}%")
    if st["url_rate"] >= 10:
        out.append(f"{st['url_rate']}% 的发言带链接")
    word, cnt = st.get("dup_top", ("", 0))
    if cnt >= 3 and word:
        out.append(f"「{word[:14]}」这句话重复发了 {cnt} 次")
    if st.get("burst_max", 1) >= 5:
        out.append(f"同一分钟内最多连发 {st['burst_max']} 条")
    if st["multiline_rate"] >= 15:
        out.append(f"{st['multiline_rate']}% 的发言是多行长消息")
    return out


def person_traits(rs, st: dict, p: dict, active_days: int, span_days: int,
                  speakers: set[str] = frozenset()) -> list[dict]:
    """性格倾向：每个结论必须能落到具体数字，且是从他自己的行为里算出来的。"""
    out: list[dict] = []
    texts = _clean_texts(rs)
    total_msgs = len(rs)
    if not texts or total_msgs < 3:
        return out

    # ---- 情感倾向（两边都按「每百条出现几次」算，量纲必须一致才可比）----
    neg = sum(len(_neg_hits(t)) for t in texts)
    pos = sum(len(_pos_hits(t)) for t in texts)
    neg_rate = neg / len(texts) * 100
    pos_rate = pos / len(texts) * 100
    # 方向判定：夸奖落没落在「具体的人」身上（@/引用真实群友）。
    pos_msgs, directed = _praise_stats(rs, speakers)
    p["praise_directed"] = directed
    p["is_cheer"] = False
    if neg_rate >= 2.5 and neg_rate > pos_rate:
        out.append({
            "tag": "带刺",
            "why": f"每 100 条里有 {neg_rate:.0f} 处脏话/攻击词，同期正面词只有 {pos_rate:.0f} 处。",
        })
    elif neg_rate >= 2.5 and pos_rate >= 5:
        # 负面与正面都高：放在「爱捧场」之前 —— 否则像 ⚡️轮回⚡️ 这种又骂又捧的
        # 竞技群玩家会被误标成纯「爱捧场」，而他的 neg_rate(3.35) 其实已高于群均值。
        out.append({
            "tag": "互损型",
            "why": f"负面 {neg_rate:.0f} 处/百条、正面也有 {pos_rate:.0f} 处 —— 更像嘴上互怼又互捧，不是真翻脸。",
        })
    elif pos_rate >= 8 and pos_rate > neg_rate * 2:
        # 方向判定：夸奖必须「钉在具体的人身上」才算捧场，否则只是公告/行话/感叹。
        # 例如反复发「【漫时光狼人杀俱乐部】…可以…」活动模板、或满屏「高手高手」
        # 的人，夸奖词密度再高也不是在捧谁，不能标「爱捧场」。
        if directed >= 2:
            out.append({
                "tag": "爱捧场",
                "why": f"每 100 条有 {pos_rate:.0f} 处夸奖/附和，其中 {directed} 处是专门 @/引用具体的人捧场。",
            })
            p["is_cheer"] = True
        # 否则：夸奖词多但不指向具体的人（活动公告模板 / 游戏感叹 / 语气词），
        # 不贴「爱捧场」标签，避免把发公告、喊口号的人误判成捧场王。

    # ---- 主动性：他找别人，还是别人找他 ----
    ro, ri = p.get("reply_out", 0), p.get("reply_in", 0)
    mo, mi = p.get("mention_out", 0), p.get("mention_in", 0)
    out_deg, in_deg = ro + mo, ri + mi
    if out_deg >= 10 and out_deg >= in_deg * 1.8:
        out.append({
            "tag": "主动搭话型",
            "why": f"主动指向别人 {out_deg} 次，被别人指向只有 {in_deg} 次。",
        })
    elif in_deg >= 10 and in_deg >= out_deg * 1.8:
        out.append({
            "tag": "被围着转",
            "why": f"被别人 @ / 引用 {in_deg} 次，自己主动出击只有 {out_deg} 次。",
        })

    # ---- 存在感 ----
    share = p.get("share", 0)
    if share >= 20:
        out.append({"tag": "话题发动机", "why": f"一人占了全群 {share}% 的发言。"})
    elif share <= 1 and total_msgs >= 5:
        out.append({"tag": "边缘参与者", "why": f"只占全群 {share}% 的发言。"})

    # ---- 稳定性 ----
    if span_days >= 7:
        att = active_days / span_days * 100
        if att >= 40:
            out.append({
                "tag": "常驻",
                "why": f"近 {span_days} 天里有 {active_days} 天说过话（{att:.0f}%）。",
            })
        elif att <= 8 and active_days >= 2:
            out.append({
                "tag": "偶尔冒泡",
                "why": f"{span_days} 天里只在 {active_days} 天露过面。",
            })

    # ---- 表达形态 ----
    if st.get("quest_rate", 0) >= 20:
        out.append({"tag": "提问型", "why": f"{st['quest_rate']}% 的发言是问句。"})
    if st.get("exclam_rate", 0) >= 25:
        out.append({"tag": "情绪外放", "why": f"{st['exclam_rate']}% 的发言带感叹号。"})
    if st.get("avg_len", 0) >= 45:
        out.append({"tag": "长文型", "why": f"平均每条 {st['avg_len']} 字，习惯把话说完整。"})
    elif st.get("short_rate", 0) >= 45:
        out.append({"tag": "碎片刷屏", "why": f"{st['short_rate']}% 是 8 字以内短句，习惯一句一发。"})
    kind0 = (st.get("kind_mix") or [{}])[0]
    if kind0.get("label") == "表情" and kind0.get("pct", 0) >= 35:
        out.append({"tag": "表情型选手", "why": f"表情占全部发言的 {kind0['pct']}%。"})
    if kind0.get("label") == "语音" and kind0.get("pct", 0) >= 30:
        out.append({"tag": "语音党", "why": f"语音占 {kind0['pct']}%（语音内容读不到，只计条数）。"})

    # ---- 复读 ----
    word, cnt = st.get("dup_top", ("", 0))
    if cnt >= 4:
        out.append({"tag": "复读机", "why": f"同一句话最多重复了 {cnt} 次。"})

    return out[:6]


def person_desc(name, p: dict, st: dict, traits: list[dict],
                rank: int = 0, n_speakers: int = 0) -> str:
    """把这个人写成一段「看完就知道他什么样」的话。

    刻意不写成清单：每段都由两个以上信号合成，并且每条都能追到具体数字。
    """
    n = p.get("count", 0)
    share = p.get("share", 0.0)

    # --- 位置 ---
    if rank == 1:
        loc = f"群里说话最多的人（{n:,} 条，占 {share}%）"
    elif rank and rank <= 3:
        loc = f"发言量排第 {rank}（{n:,} 条，占 {share}%）"
    elif share >= 8:
        loc = f"属于撑起话题的少数几个人（{n:,} 条，占 {share}%）"
    elif share <= 0.5:
        loc = f"存在感很弱，只发了 {n:,} 条（占 {share}%）"
    else:
        loc = f"{n:,} 条，占全群 {share}%"

    sents = [loc]

    # --- 他怎么跟别人相处 ---
    ro, ri = p.get("reply_out", 0), p.get("reply_in", 0)
    mo, mi = p.get("mention_out", 0), p.get("mention_in", 0)
    partner = p.get("partner", "")
    mutual = p.get("mutual", 0)
    if ro + mo >= 30 and (ro + mo) >= (ri + mi) * 1.8:
        rel = (f"他一直在主动找人聊（主动指向别人 {ro + mo} 次，别人主动找他只有 {ri + mi} 次）"
               f"{f'，来回最多的是「{partner}」（{mutual} 次）' if partner and mutual else ''}。")
    elif ri + mi >= 30 and (ri + mi) >= (ro + mo) * 1.8:
        rel = (f"多数时候是别人来找他（被 @/引用 {ri + mi} 次，他自己主动出击只有 {ro + mo} 次）"
               f"{f'，跟他来回最多的是「{partner}」（{mutual} 次）' if partner and mutual else ''}。")
    elif partner and mutual:
        rel = f"跟「{partner}」互回 {mutual} 次，是他在这群里最稳定的来回。"
    else:
        rel = ""
    if rel:
        sents.append(rel)

    # --- 脾气（来自实测正负词密度） ---
    neg5 = st.get("_neg_rate")
    pos5 = st.get("_pos_rate")
    if neg5 is not None and pos5 is not None:
        if neg5 >= 2.5 and neg5 > pos5:
            sents.append(f"带刺：每 100 条有 {neg5:.0f} 处攻击性用词，正面表达只有 {pos5:.0f} 处 —— 吵架是常态，不是偶发。")
        elif neg5 >= 2.5 and pos5 >= 5:
            sents.append(f"互损：负面 {neg5:.0f} 处、正面 {pos5:.0f} 处/百条 —— 又骂又捧，更像竞技群的嘴上互怼。")
        elif p.get("is_cheer"):
            directed = p.get("praise_directed", 0)
            sents.append(
                f"嘴甜：每 100 条有 {pos5:.0f} 处夸奖附和，其中 {directed} 处是专门捧具体的人，"
                f"攻击性用词 {neg5:.0f} 处。"
            )

    # --- 打字方式 ---
    styles = style_lines(st)
    if styles:
        sents.append("；".join(styles[:3]) + "。")

    # --- 关键词 ---
    kws = p.get("keywords") or []
    if kws:
        sents.append("他常挂嘴边的： " + "、".join(kws[:6]) + "。")

    return "".join(sents)
