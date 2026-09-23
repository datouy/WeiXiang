"""关系网：@提及 / 引用回复 计数 → 边、关系类型、角色。"""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations

from .. import config as C
from . import text as T
from .model import Rec


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


class Network:
    def __init__(self, self_name: str = "") -> None:
        self.self_name = self_name
        # 有向计数
        self.mention_dir: Counter[tuple[str, str]] = Counter()  # a 提及 b
        self.reply_dir: Counter[tuple[str, str]] = Counter()  # a 引用 b
        # 每个成员的消息文本（用于关系定性）
        self.pair_text: dict[tuple[str, str], list[str]] = defaultdict(list)
        # 被引用的原话
        self.quote_text_counter: Counter[str] = Counter()

    # ------------------------------------------------------------------
    def feed(self, rec: Rec, speakers: set[str]) -> None:
        s = rec.sender
        if not s:
            return

        for m in rec.mentions:
            m = m.strip()
            if not m or m == s:
                continue
            # @ 后面可能是昵称的一部分，宽松匹配
            target = _match_speaker(m, speakers)
            if target and target != s:
                self.mention_dir[(s, target)] += 1
                self.pair_text[_pair(s, target)].append(rec.text)

        if rec.quote_name:
            target = _match_speaker(rec.quote_name, speakers)
            if target and target != s:
                self.reply_dir[(s, target)] += 1
                self.pair_text[_pair(s, target)].append(rec.text)
                if rec.quote_text:
                    self.quote_text_counter[rec.quote_text.strip()[:120]] += 1

    # ------------------------------------------------------------------
    def edges(self, min_weight: int = 3) -> list[dict]:
        """无向边：权重 = 互回次数（引用回复取小值）+ 提及次数。"""
        agg: dict[tuple[str, str], dict] = defaultdict(
            lambda: {"reply": 0, "reply_min": 0, "mention": 0, "texts": []}
        )
        for (a, b), n in self.reply_dir.items():
            agg[_pair(a, b)]["reply"] += n
        for (a, b), n in self.mention_dir.items():
            agg[_pair(a, b)]["mention"] += n

        # 互回次数 = 双向引用的较小值
        for pair in list(agg):
            a, b = pair
            agg[pair]["reply_min"] = min(self.reply_dir.get((a, b), 0), self.reply_dir.get((b, a), 0))

        out: list[dict] = []
        for pair, d in agg.items():
            a, b = pair
            texts = self.pair_text.get(pair, [])
            weight = d["reply"] * 2 + d["mention"]
            if weight < min_weight:
                continue
            rel, meta = classify_relation(texts, d["reply_min"], d["reply"] + d["mention"])
            label = _edge_label(pair, d, self.self_name)
            out.append(
                {
                    "a": a,
                    "b": b,
                    "t": rel,
                    "w": weight,
                    "mutual": d["reply_min"],
                    "reply": d["reply"],
                    "mention": d["mention"],
                    "l": label,
                    **meta,
                }
            )
        out.sort(key=lambda e: -e["w"])
        return out


def _match_speaker(token: str, speakers: set[str]) -> str | None:
    """把 @昵称 / 引用里的名字匹配到真实发言者。"""
    if not token:
        return None
    if token in speakers:
        return token
    tok = token.strip().lstrip("@")
    if tok in speakers:
        return tok
    # 前缀 / 包含匹配，取最长的候选，避免 "小中头" 命中 "中头"
    best = None
    for s in speakers:
        if len(s) < 2:
            continue
        if s == tok or tok.startswith(s) or s.startswith(tok) or s in tok:
            if best is None or len(s) > len(best):
                best = s
    return best


def classify_relation(texts: list[str], mutual: int, total: int) -> tuple[str, dict]:
    """按词典命中把一对关系定性。"""
    joined = "\n".join(texts[-400:]) if texts else ""
    neg = T.count_hits(joined, T.NEG_WORDS)
    pos = T.count_hits(joined, T.POS_WORDS)
    trade = T.count_hits(joined, T.TRADE_WORDS)
    real = T.count_hits(joined, T.REAL_WORDS)

    scores = {"trade": trade * 1.6, "real": real * 2.0}
    meta = {"neg": neg, "pos": pos, "trade": trade, "real": real}

    # 交易 / 现实关系门槛更低，因为这类词更罕见、更具体
    if trade >= 4 and trade >= neg * 0.5 and trade * 1.6 >= real * 2.0:
        return "trade", meta
    if real >= 3 and real * 2.0 >= trade * 1.6:
        return "real", meta
    if neg + pos == 0:
        return "meme", meta
    if neg > pos * 1.2:
        return "foe", meta
    if pos > neg * 1.2:
        return "ally", meta
    return "meme", meta


def _edge_label(pair: tuple[str, str], d: dict, self_name: str) -> str:
    a, b = pair
    parts = []
    if d["reply_min"]:
        parts.append(f"互回 {d['reply_min']} 次")
    if d["reply"]:
        parts.append(f"引用 {d['reply']} 次")
    if d["mention"]:
        parts.append(f"@ {d['mention']} 次")
    return " · ".join(parts) or "关联"


def compute_roles(
    speakers: Counter,
    reply_in: Counter,
    reply_out: Counter,
    mention_in: Counter,
    mention_out: Counter,
    self_name: str,
    owner: str = "",
) -> dict[str, str]:
    """角色判定：hub / key / admin / self / meme / member。"""
    roles: dict[str, str] = {}
    if not speakers:
        return roles

    influence = {
        s: reply_in[s] * 2 + mention_in[s] + reply_out[s] + mention_out[s] * 0.5
        for s in speakers
    }
    ranked = sorted(influence.items(), key=lambda kv: -kv[1])

    for s in speakers:
        if s == self_name:
            roles[s] = "self"
        elif owner and s == owner:
            roles[s] = "admin"

    if ranked:
        roles.setdefault(ranked[0][0], "hub")
    # 关键人物：发言量前 25% 且影响力前 8 名
    vol_ranked = [s for s, _ in speakers.most_common()]
    cutoff = max(3, len(vol_ranked) // 4)
    for s, _ in ranked[:8]:
        roles.setdefault(s, "key")
    for s in vol_ranked[:cutoff]:
        roles.setdefault(s, "key")
    for s in speakers:
        roles.setdefault(s, "member")
    return roles
