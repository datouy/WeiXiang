"""中文文本工具：分词、停用词、情绪/交易/现实关系词典、关键词提取。

全部离线，不调用外部模型。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable

try:
    import jieba

    jieba.setLogLevel(60)
    _HAS_JIEBA = True
except Exception:  # pragma: no cover
    _HAS_JIEBA = False

WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-\.]{1,}|[0-9]{2,}")
CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
URL_RE = re.compile(r"https?://\S+")
AT_RE = re.compile(r"@[^\s@\u2005]{1,32}")

STOPWORDS = set(
    """的 了 是 在 我 你 他 她 它 们 这 那 有 和 就 不 也 都 还 要 会 说 到 去 来 个 上 下 什 么 怎么
    吗 呢 吧 啊 呀 哦 嗯 哈 呵 诶 哎 唉 我 你们 我们 他们 自己 一个 什么 没有 不是 就是 可以 这个 那个
    现在 时候 一下 一样 这样 那样 因为 所以 但是 如果 或者 而且 然后 已经 还是 真的 感觉 觉得 知道
    没 很 太 更 最 再 又 只 被 把 给 让 从 对 跟 向 于 与 及 而 则 之 其 此 该 些 各 每 另 别 才 刚
    能 想 得 着 过 起 开 拿 放 走 看 听 讲 问 答 好 坏 大 小 多 少 高 低 长 短 快 慢 新 老
    le la wo ni ta de shi le ok OK Ok""".split()
)

# 攻击 / 对立语气
NEG_WORDS = set(
    """傻逼 沙雕 智障 弱智 脑残 白痴 废物 垃圾 滚蛋 滚 闭嘴 神经病 有病 贱 死 操 草 妈的 他妈
    尼玛 你妈 卧槽 我操 靠 狗东西 畜生 孙子 龟儿子 王八 去死 恶心 讨厌 烦人 别废话 神经 蠢 笨
    拉黑 举报 抬杠 抬你 杠精 喷 怼 骂 sb SB 辣鸡 弱鸡 菜 傻 蠢货 鸟人 吊 屌 装逼 装 忽悠 骗子
    服了 无语 呵呵 笑话 井底之蛙 文盲 卢瑟 屌丝 穷逼 乡巴佬""".split()
)

# 附和 / 友善
POS_WORDS = set(
    """哈哈哈 哈哈 笑死 牛逼 牛 厉害 强 赞 支持 同意 说得对 有道理 确实 是的 没错 对对对 就是
    感谢 谢谢 多谢 辛苦 加油 好的 可以 优秀 佩服 高手 大佬 学到了 受教 靠谱 顶 好活 妙 绝了
    真实 真相 说得真好 附议 同感 我也是 感动 温暖 可爱 喜欢""".split()
)

# 交易 / 供应链
TRADE_WORDS = set(
    """价格 多少钱 报价 优惠 便宜 贵 打折 折扣 拼车 代开 开通 订阅 续费 会员 账号 激活码 卡密
    中轉 中转 倍率 token Token TOKEN 额度 余额 充值 提现 付款 转账 红包 收款 支付 链接 下单
    发货 快递 包邮 秒杀 拼团 团购 返利 渠道 代理 批发 拿货 供应商 库存 成本 利润 赚 亏 生意
    买 卖 出 收 卖号 出号 收号 批发价 代购 代充""".split()
)

# 现实关系
REAL_WORDS = set(
    """线下 面基 见面 见过 同学 老乡 同事 校友 高中 大学 公司 上班 工位 一起吃饭 请客 约
    电话 手机 号码 微信 私聊 加个 小群 邻居 同一 城市 出差 过去 来找我 来我这 老家 家里""".split()
)

# 指令（/xxx）
CMD_RE = re.compile(r"(?<![A-Za-z0-9_/])/([A-Za-z0-9_\-\u4e00-\u9fff\.]{1,24})")

# 金额 / 数字线索
MONEY_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?\s*(?:元|块|万|k|K|w|W|亿|毛|角)|\b[0-9]{2,6}\b")

_SENT_SPLIT = re.compile(r"[。！？!?\n]+")


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    text = URL_RE.sub(" ", text)
    text = AT_RE.sub(" ", text)
    out: list[str] = []
    if _HAS_JIEBA:
        for w in jieba.cut(text):
            w = w.strip()
            if not w or w in STOPWORDS:
                continue
            if len(w) == 1 and not w.isalnum():
                continue
            out.append(w)
    else:
        for m in CJK_RE.finditer(text):
            s = m.group(0)
            for i in range(len(s) - 1):
                out.append(s[i : i + 2])
        out.extend(WORD_RE.findall(text))
    return out


def count_hits(text: str, vocab: set[str]) -> int:
    if not text:
        return 0
    return sum(1 for w in tokenize(text) if w.lower() in vocab or w in vocab)


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]


def extract_keywords(
    docs: Iterable[list[str]],
    min_count: int = 5,
    top: int = 30,
) -> list[tuple[str, int]]:
    """全局词频 → 关键词。docs 是每条消息的 token 列表。"""
    df = Counter()
    for toks in docs:
        for t in set(toks):
            df[t] += 1
    return [(w, c) for w, c in df.most_common(top * 6) if c >= min_count][:top]


def distinctive_words(
    person_docs: list[list[str]],
    global_df: Counter,
    total_docs: int,
    top: int = 8,
) -> list[str]:
    """用 TF-IDF 近似挑某个人的特征词（口头禅）。"""
    tf = Counter()
    for toks in person_docs:
        tf.update(toks)
    scored: list[tuple[float, str, int]] = []
    for w, c in tf.items():
        if c < 3 or len(w) < 2 and not w.isascii():
            continue
        g = global_df.get(w, 0)
        idf = math.log((total_docs + 1) / (g + 1)) + 1.0
        scored.append((c * idf, w, c))
    scored.sort(reverse=True)
    return [w for _, w, _ in scored[:top]]


def top_phrases(texts: list[str], min_count: int = 3, top: int = 10) -> list[tuple[str, int]]:
    """短语/口头禅候选：2~6 个中文字符的片段频次。"""
    c = Counter()
    for t in texts:
        t = URL_RE.sub(" ", t)
        for m in CJK_RE.finditer(t):
            s = m.group(0)
            for L in (3, 4, 5):
                for i in range(0, max(0, len(s) - L + 1)):
                    sub = s[i : i + L]
                    if any(ch in "的了是我你他她它这那" for ch in sub[:1]):
                        continue
                    c[sub] += 1
    out = [(w, n) for w, n in c.most_common(top * 8) if n >= min_count]
    # 去掉被更长片段包含且频次接近的
    out.sort(key=lambda kv: (-len(kv[0]), -kv[1]))
    kept: list[tuple[str, int]] = []
    for w, n in out:
        if any(w != k and w in k and n <= v * 1.05 for k, v in kept):
            continue
        kept.append((w, n))
        if len(kept) >= top:
            break
    return kept
