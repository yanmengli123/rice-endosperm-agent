"""证据高亮精化：段落级 anchor → 句子级 highlight（只读投影）。

MinerU 锚点是块粒度，quote/bbox 常为整段（数百字符 + 整栏矩形）；问答场景
用户需要的是"这段里哪一句回应了问题"。本模块在组装期用确定性词法重叠
选出最佳句，写入 ``locator.highlight`` 供查看器做文本层精确定位：

- 切句复用 :mod:`sentence_splitter`（与 span 体系同源，DOI/缩写保护一致）；
- 打分 = 句子内容词 ∩ 问题内容词；平分取词密度高者、再取更早的句；
- 命中数须达到问题内容词的 40%（下限 2、上限 4），泛词偶合不构成句子级证据；
- 最佳句必须是 anchor quote 的逐字子串，否则不产出（fail-closed，
  绝不生成近似句子）。本模块不改变 evidence_id / fragments / 验证结果。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from yuxi.knowledge.evidence.sentence_splitter import split_sentences

HIGHLIGHT_REFINER_VERSION = "sentence_lexical_overlap_v1"

MIN_MATCHED_TERMS = 2
MAX_REQUIRED_TERMS = 4
REQUIRED_COVERAGE = 0.4

# 英文停用词 + 中文高频虚词：打分前从两侧剔除，避免 "the/of/的" 主导重叠
_STOPWORDS = frozenset(
    """
    the a an of to and or in on for was were is are be been being with also
    this that these those it its as at by from which who whom whose what when
    where how why do does did not no than then so such very can could will
    would should shall may might must have has had about into over under
    的 了 是 在 和 与 也 就 都 而 及 对 从 被 把 这 那 吗 呢 吧 请
    """.split()
)

_ASCII_WORD = re.compile(r"[0-9A-Za-z]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


@dataclass(frozen=True)
class RefinedHighlight:
    """选出的句子及其可审计的匹配强度。"""

    quote: str
    matched_terms: int
    coverage: float


def _content_tokens(text: str) -> set[str]:
    """内容词集合：英文整词小写化（≥2 字符），中文按字符二元组；停用词剔除。"""
    text = str(text or "")
    tokens = {word.lower() for word in _ASCII_WORD.findall(text) if len(word) >= 2}
    tokens -= _STOPWORDS
    for run in _CJK_RUN.findall(text):
        cleaned = "".join(char for char in run if char not in _STOPWORDS)
        if len(cleaned) == 1:
            tokens.add(cleaned)
        else:
            tokens.update(cleaned[index : index + 2] for index in range(len(cleaned) - 1))
    return tokens


def refine_highlight_quote(*, anchor_quote: str, question_text: str) -> RefinedHighlight | None:
    """从 anchor quote 中选出与问题词法重叠最高的句子；信号不足时返回 None。"""
    quote = str(anchor_quote or "").strip()
    question = str(question_text or "").strip()
    if not quote or not question:
        return None
    question_tokens = _content_tokens(question)
    if not question_tokens:
        return None
    required = min(MAX_REQUIRED_TERMS, max(MIN_MATCHED_TERMS, math.ceil(REQUIRED_COVERAGE * len(question_tokens))))

    best_key: tuple[int, float, int] | None = None
    best_sentence = ""
    for index, sentence in enumerate(split_sentences(quote)):
        sentence = sentence.strip()
        if len(sentence) < 8:
            continue
        tokens = _content_tokens(sentence)
        if not tokens:
            continue
        matched = len(tokens & question_tokens)
        if matched < required:
            continue
        key = (matched, matched / len(tokens), -index)
        if best_key is None or key > best_key:
            best_key = key
            best_sentence = sentence

    # fail-closed：切句不应增删字符，但任何漂移都直接放弃精化
    if best_key is None or best_sentence not in quote:
        return None
    return RefinedHighlight(
        quote=best_sentence,
        matched_terms=best_key[0],
        coverage=round(best_key[0] / len(question_tokens), 2),
    )
