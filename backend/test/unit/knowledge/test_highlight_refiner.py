"""句子级高亮精化（highlight_refiner）与英文句号切分（splitter 1.1）单测。"""

from __future__ import annotations

import pytest

from yuxi.knowledge.evidence.highlight_refiner import HIGHLIGHT_REFINER_VERSION, refine_highlight_quote
from yuxi.knowledge.evidence.sentence_splitter import SPLITTER_VERSION, split_sentences

pytestmark = [pytest.mark.unit]

# 取自 OsMYB73 论文第 3 页左栏的段落级锚点（MinerU 块粒度：整段一条 quote）
PARAGRAPH = (
    "Rice endosperm starch biosynthesis is a critical factor in grain quality and nutrition, "
    "scientists found co-expressed transcription factors with starch synthesis genes. "
    "To study the regulatory function of rice OsMYB73, we first analysed its phylogenetic "
    "relationship with other MYB proteins using MEGA 7.0 software (Kumar et al. 2016). "
    "The phylogenetic tree revealed that OsMYB73 clustered with AtMYB73 and ZmMYB31 (Figure 1a). "
    "The structure of OsMYB73 protein was also predicted and the results revealed that OsMYB73 "
    "contained two typical SANT domains between 115–164 and 167–215 (Figure S1)."
)
QUESTION = "The structure of OsMYB73 protein was also predicted and the results revealed ？"


def test_split_sentences_handles_english_periods_and_protects_abbreviations():
    assert SPLITTER_VERSION == "1.2"
    sentences = split_sentences(PARAGRAPH)
    assert len(sentences) == 4
    # "et al." / "7.0" / "(Figure 1a)." 不产生伪边界
    assert sentences[1].endswith("(Kumar et al. 2016).")
    assert "MEGA 7.0 software" in sentences[1]
    # 切分不增删字符：拼回等于原文
    assert "".join(sentences) == PARAGRAPH


def test_split_sentences_protects_initials_doi_and_chinese_boundaries():
    assert split_sentences("Data from J. Liu were reused. See doi:10.1111/pbi.14558 for details.") == [
        "Data from J. Liu were reused.",
        " See doi:10.1111/pbi.14558 for details.",
    ]
    # 中文全角句号后无空格也必须切开
    assert split_sentences("OsMYB73 调控淀粉合成。它含两个 SANT 结构域！") == [
        "OsMYB73 调控淀粉合成。",
        "它含两个 SANT 结构域！",
    ]


def test_refiner_picks_the_sentence_answering_the_question():
    refined = refine_highlight_quote(anchor_quote=PARAGRAPH, question_text=QUESTION)
    assert refined is not None
    assert refined.quote.startswith("The structure of OsMYB73 protein was also predicted")
    assert refined.quote.endswith("(Figure S1).")
    # 6 个问题内容词（structure/osmyb73/protein/predicted/results/revealed）全部命中
    assert refined.matched_terms == 6
    assert refined.coverage == 1.0
    # fail-closed 保证：精化句必须是 anchor quote 的逐字子串
    assert refined.quote in PARAGRAPH
    assert HIGHLIGHT_REFINER_VERSION == "sentence_lexical_overlap_v1"


def test_refiner_is_deterministic_and_fails_closed_without_signal():
    first = refine_highlight_quote(anchor_quote=PARAGRAPH, question_text=QUESTION)
    second = refine_highlight_quote(anchor_quote=PARAGRAPH, question_text=QUESTION)
    assert first == second
    assert refine_highlight_quote(anchor_quote=PARAGRAPH, question_text="") is None
    assert refine_highlight_quote(anchor_quote="", question_text=QUESTION) is None
    # 只有停用词的问题没有内容词 → 不精化
    assert refine_highlight_quote(anchor_quote=PARAGRAPH, question_text="the of and") is None
    # 单个内容词命中不足以构成句子级证据（避免把"OsMYB73"命中的任意句当答案）
    assert refine_highlight_quote(anchor_quote=PARAGRAPH, question_text="OsMYB73 数据") is None


def test_refiner_rejects_weak_overlap_on_unrelated_paragraph():
    # 6 个内容词的问题至少要命中 3 个；仅 osmyb73/results 泛词偶合的段落不精化
    unrelated = (
        "OsMYB73 was cloned into pGEX-4T-1 for recombinant expression. "
        "The results of the pull-down assay confirmed the interaction in vitro."
    )
    assert refine_highlight_quote(anchor_quote=unrelated, question_text=QUESTION) is None


def test_refiner_prefers_denser_sentence_on_score_tie():
    quote = (
        "OsMYB73 regulates starch synthesis in rice endosperm through a long chain of intermediate factors. "
        "OsMYB73 regulates starch synthesis."
    )
    refined = refine_highlight_quote(anchor_quote=quote, question_text="How does OsMYB73 regulate starch synthesis?")
    # 两句命中数相同（osmyb73/starch/synthesis），短句词密度更高
    assert refined is not None
    assert refined.quote == "OsMYB73 regulates starch synthesis."


def test_refiner_matches_chinese_by_character_bigrams():
    quote = "OsMYB73 是水稻胚乳中的转录因子。该蛋白的结构预测显示其含有两个典型的 SANT 结构域。它与 AtMYB73 聚为一类。"
    refined = refine_highlight_quote(anchor_quote=quote, question_text="OsMYB73 蛋白结构预测结果显示什么？")
    assert refined is not None
    assert refined.quote == "该蛋白的结构预测显示其含有两个典型的 SANT 结构域。"
