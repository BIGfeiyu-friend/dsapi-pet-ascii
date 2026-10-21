"""中文友好的检索分词：CJK 连续段切成重叠二元组 + 非虚词单字，拉丁/数字按词。

FTS5 自带 unicode61 分词器不会切分中文，因此入库前先转换成空格分隔的 token 串，
查询时用同样的规则生成 OR 查询，再在 Python 侧按加权命中（二元组 2 分、单字 1 分）做二次过滤。
例：“我家的猫叫什么” 与 “橘猫叫团子” 只共享二元组“猫叫”，加上单字“猫”“叫”才足以召回。
"""
from __future__ import annotations

import re

TOK_VERSION = 2

_CJK = "㐀-䶿一-鿿豈-﫿぀-ヿ가-힯"
_RUN_RE = re.compile(f"[{_CJK}]+|[A-Za-z0-9_]+")
_CJK_RE = re.compile(f"[{_CJK}]")

# 不作为单字 token 的高频虚字/代词/泛义动词
_STOP_CHARS = set(
    "我你他她它们的了是在有和就不也都很吗呢吧啊呀嘛哦哇哈嗯这那个些么还要会能想说看去来到着过给让被把为"
    "对从与及其或而且但如果因所以可得地之上下中多少好没再又才只最更太真啦呗噢喔唉哎诶咯么哒什"
)
# 只在查询端过滤的低信息二元组
_PARTICLES = set("的吗呢吧啊呀嘛哦哇哈嗯")
_STOP = {
    "我们", "你们", "他们", "她们", "什么", "怎么", "这个", "那个", "一个", "没有", "不是",
    "就是", "可以", "还是", "因为", "所以", "但是", "然后", "现在", "知道", "觉得", "这样",
    "那样", "这么", "那么", "一下", "一点", "有点", "真的", "已经", "我是", "你是", "我也",
    "你也", "是不", "不会", "我想", "你想", "我要", "你要", "好的", "不要", "如果", "为什",
    "还记", "记得", "得我", "记不", "不记", "叫什", "你还", "是什",
}

RECALL_CUES = (
    "记得", "记不记", "上次", "之前", "以前", "那天", "说过", "提过", "讲过", "聊过", "告诉过",
    "忘了", "还记", "当时", "上回", "前几天", "昨天", "remember", "last time",
)


def tokens(text: str) -> list[str]:
    out: list[str] = []
    for run in _RUN_RE.findall(text or ""):
        if _CJK_RE.match(run):
            if len(run) > 1:
                out.extend(run[i:i + 2] for i in range(len(run) - 1))
            out.extend(ch for ch in run if ch not in _STOP_CHARS)
        else:
            w = run.lower()
            if len(w) >= 2 or w.isdigit():
                out.append(w)
    return out


def index_text(text: str) -> str:
    return " ".join(tokens(text))


def weight(tok: str) -> int:
    return 1 if len(tok) == 1 and _CJK_RE.match(tok) else 2


def query_tokens(text: str, max_terms: int = 64) -> list[str]:
    seen: list[str] = []
    s = set()
    for t in tokens(text):
        if t in s or t in _STOP:
            continue
        if len(t) == 2 and _CJK_RE.match(t) and (t[0] in _PARTICLES or t[1] in _PARTICLES):
            continue
        s.add(t)
        seen.append(t)
        if len(seen) >= max_terms:
            break
    return seen


def total_weight(query_toks: list[str]) -> int:
    return sum(weight(t) for t in query_toks)


def fts_query(toks: list[str]) -> str | None:
    if not toks:
        return None
    return " OR ".join('"%s"' % t.replace('"', "") for t in toks)


def overlap(query_toks: list[str], text: str) -> int:
    """加权命中分：二元组/英文词 2 分，单字 1 分。"""
    if not query_toks:
        return 0
    have = set(tokens(text))
    return sum(weight(t) for t in query_toks if t in have)


def min_overlap(query_toks: list[str]) -> int:
    return 2


def has_recall_cue(text: str) -> bool:
    low = (text or "").lower()
    return any(c in low for c in RECALL_CUES)
