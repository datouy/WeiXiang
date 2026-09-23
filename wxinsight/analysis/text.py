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
# 消息里的方括号标记（[引用 xx] / [图片] / [链接] …）与 XML 残留，都不该进词频
BRACKET_RE = re.compile(r"\[[^\]]{0,40}\]")
XML_NOISE_RE = re.compile(r"<?\s*(?:xml|msg|appmsg|refermsg|msgsource)\b[\s\S]{0,400}?>", re.I)

# 技术噪声词：来自微信 XML / 引用的是链接或文件消息时残留的字段，不是聊天内容
TECH_NOISE = set(
    """xml XML Xml version appmsg appid sdkver refermsg msgsource title des url type appattach
    totallen attachid cdnthumbaeskey aeskey fileext md5 fromusr chatusr svrid createtime strid
    alnode silence membercount signature tmp_node publisher fr msg
    """.split()
)

STOPWORDS = set(
    """的 了 是 在 我 你 他 她 它 们 这 那 有 和 就 不 也 都 还 要 会 说 到 去 来 个 上 下 什 么 怎么
    吗 呢 吧 啊 呀 哦 嗯 哈 呵 诶 哎 唉 我 你们 我们 他们 自己 一个 什么 没有 不是 就是 可以 这个 那个
    现在 时候 一下 一样 这样 那样 因为 所以 但是 如果 或者 而且 然后 已经 还是 真的 感觉 觉得 知道
    没 很 太 更 最 再 又 只 被 把 给 让 从 对 跟 向 于 与 及 而 则 之 其 此 该 些 各 每 另 别 才 刚
    能 想 得 着 过 起 开 拿 放 走 看 听 讲 问 答 好 坏 大 小 多 少 高 低 长 短 快 慢 新 老
    le la wo ni ta de shi le ok OK Ok""".split()
)

# 话语标记 / 时间虚词：这些词几乎出现在所有群，做「议题」毫无信息量，
# 但分词只按语法切，不会替你判断它有没有语义价值 —— 这里显式排除。
DISCOURSE = set(
    """还有 而且 并且 不过 虽然 但是 而是 于是 至于 以及 甚至 反正 其实 总之 话说 话说回来
    今天 明天 昨天 后天 前天 刚才 马上 立刻 突然 偶尔 有时 平时 具体 基本 差不多
    之前 之后 以前 以后 以后 当中 的话 样子 方式 方面 部分 一半 全部 所有 一切
    其实 大概 也许 可能 应该 必须 肯定 简直 显然 果然 居然 竟然 反正
    不用 不要 不会 不能 只是 就是 也是 也是 有的 有些 一些 一点 一般 之类
    玩意 不过 而且 或者说 就是说 也就是 意味着 看起来 怎么说 比如说 一般情况下
    意思 情况 问题 结果 开始 发现 明白 记得 准备 打算 需要 要求 注意 出现
    哈哈哈 哈哈 呵呵 嘻嘻 嘿嘿 吼吼 嘿嘿嘿 略略略 嗯嗯 哦哦 啊啊 诶诶
    看看 瞧瞧 听听 说说 讲讲 想想 猜猜 试试 写着 来了 去了 得了 算了 行了 好吧
    这么 那么 多少 怎样 咋样 咋办 干嘛 干啥 一行 两句 两只 三条
    不了 可以 知道 觉得 应该 需要 想要 希望 以为 认为 表示 说明 告诉 提醒 建议
    大家 你们 我们 他们 自己 别人 别人 没人 人人 所有人 各位 大家伙
    """.split()
)
STOPWORDS |= DISCOURSE

# 攻击 / 对立语气
NEG_WORDS = set(
    """傻逼 沙雕 智障 弱智 脑残 白痴 废物 垃圾 滚蛋 滚 闭嘴 神经病 有病 贱 死 操 草 妈的 他妈
    尼玛 你妈 卧槽 我操 靠 狗东西 畜生 孙子 龟儿子 王八 去死 恶心 讨厌 烦人 别废话 神经 蠢 笨
    拉黑 举报 抬杠 抬你 杠精 喷 怼 骂 sb SB 辣鸡 弱鸡 菜 傻 蠢货 鸟人 吊 屌 装逼 装 忽悠 骗子
    服了 无语 呵呵 笑话 井底之蛙 文盲 卢瑟 屌丝 穷逼 乡巴佬""".split()
)

# 附和 / 友善（注意：把「哈哈哈」这类笑声排除 —— 它是情绪宣泄不是夸奖，
# 否则每个爱笑的人都会被误标成「爱捧场」）。
# 同时只保留「多字」的明确夸奖词：单字「强/牛/顶/妙」在游戏/群聊里大半是
# 行话（强=强力的英雄、顶=顶号/管理小号、牛/妙多为语气），算成夸奖会误标「爱捧场」。
POS_WORDS = set(
    """牛逼 厉害 赞 支持 同意 说得对 有道理 确实 是的 没错 对对对 就是
    感谢 谢谢 多谢 辛苦 加油 好的 可以 优秀 佩服 高手 大佬 学到了 受教 靠谱 好活 绝了
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
    text = XML_NOISE_RE.sub(" ", text)
    text = BRACKET_RE.sub(" ", text)
    text = re.sub(r"</?[A-Za-z][\w\-]*(?:\s[^<>]*)?>", " ", text)
    out: list[str] = []
    if _HAS_JIEBA:
        for w in jieba.cut(text):
            w = w.strip()
            if not w or w in STOPWORDS or w in TECH_NOISE:
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


def is_xml_noise(text: str) -> bool:
    """被引用的内容本身是 XML（引用了链接/文件消息）时，不算有效文本。"""
    if not text:
        return True
    return bool(re.search(r"<\s*(?:msg|appmsg)\b|<\?xml", text, re.I))


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
        if c < 3 or len(w) < 2 or w.isdigit():
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
    # 先长后频，逐个剔除「与已保留项同源」的滑动窗口碎片
    out.sort(key=lambda kv: (-len(kv[0]), -kv[1]))
    kept: list[tuple[str, int]] = []
    for w, n in out:
        dup = False
        for k, v in kept:
            if w == k or w in k or k in w:
                dup = True
                break
            # 共享 3 个以上连续字 = 同一个短语的错位窗口
            if _shared_run(w, k) >= 3:
                dup = True
                break
        if dup:
            continue
        kept.append((w, n))
        if len(kept) >= top:
            break
    return kept


def _shared_run(a: str, b: str) -> int:
    """两个串共享的最长连续子串长度（用于识别滑动窗口碎片）。"""
    best = 0
    for i in range(len(a)):
        for j in range(i + 1, len(a) + 1):
            sub = a[i:j]
            if len(sub) <= best:
                continue
            if sub in b:
                best = len(sub)
            else:
                break
    return best
