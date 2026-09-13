"""解释 Claim 分类测试（P4：Scientific Explanation Binding）。"""

from __future__ import annotations

import re
import unicodedata

from yuxi.knowledge.rendering.explanation_claims import (
    CLAIM_CAPTION_FACT,
    CLAIM_TEXT_SUPPORTED_INTERPRETATION,
    CLAIM_UNSUPPORTED_INTERPRETATION,
    CLAIM_VISUAL_OBSERVATION,
    classify_explanation_claims,
)


def _norm(text: str) -> str:
    value = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z一-鿿]+", " ", value.casefold())).strip()


def _citation(ref: str, page: int, quote: str, *, zone: str = "MAIN_TEXT") -> dict:
    return {
        "ref": ref,
        "evidence_id": f"ev-{ref}",
        "kb_id": "kb-a",
        "file_id": "file-a",
        "filename": "paper.pdf",
        "zone": zone,
        "page_numbers": [page],
        "primary_page": page,
        "quote_head": quote[:80],
        "anchor_ids": [f"ea-{ref}"],
        "locatable": True,
        "toc_line": False,
        "secondary_of": None,
        "_anchor_id": f"ea-{ref}",
        "_physical_evidence_id": f"ev-{ref}",
        "_retrieval_channel": "DOCUMENT",
        "_span_id": f"es-{ref}",
        "_span_evidence_id": f"evs-{ref}",
        "_parse_revision_id": "pr-active",
        "_index_revision_id": "ir-active",
        "_source_sha256": "a" * 64,
        "_quote": quote,
        "_quote_norm": _norm(quote),
    }


_CAPTION_QUOTE = (
    "Figure 1. Expression patterns of OsMYB73 in rice seeds. (a) Relative expression levels of OsMYB73 "
    "during seed development. (b) Histochemical GUS staining of transgenic rice seeds."
)
_MAIN_TEXT_QUOTE = (
    "These results indicate that OsMYB73 functions as a transcriptional regulator during rice seed "
    "development, consistent with its expression in the 5 DAF embryo."
)

_CITATIONS = [
    _citation("E1", 4, _CAPTION_QUOTE, zone="SUPPORTING_INFO"),
    _citation("E2", 6, _MAIN_TEXT_QUOTE, zone="MAIN_TEXT"),
]
_LOCATOR = {
    "status": "VERIFIED",
    "page": 4,
    "anchor_id": "ea-E1",
    "zone": "SUPPORTING_INFO",
    "quote": _CAPTION_QUOTE,
}
_OBSERVATION = {"schema_version": "visual-observation.v1", "figure_label": "Figure 1"}


def test_visual_observation_claim_supported_by_observation_contract():
    result = classify_explanation_claims(
        "图中包含 a/b/c 三个 panel。",
        citations=_CITATIONS,
        locator=_LOCATOR,
        observation=_OBSERVATION,
    )
    assert result["claims"][0]["claim_type"] == CLAIM_VISUAL_OBSERVATION
    assert result["claims"][0]["supported"] is True


def test_visual_observation_without_contract_is_not_claimed():
    """无观察契约时，定性图面描述不冒充 VISUAL_OBSERVATION，也不记为失败。"""
    result = classify_explanation_claims(
        "图中包含 a/b/c 三个 panel。",
        citations=_CITATIONS,
        locator=_LOCATOR,
        observation=None,
    )
    assert result["claims"] == []
    assert result["unsupported_count"] == 0


def test_caption_fact_binds_to_caption_carrier():
    result = classify_explanation_claims(
        "Figure 1 包含 OsMYB73 的相对表达量检测与 GUS 染色。",
        citations=_CITATIONS,
        locator=_LOCATOR,
        observation=_OBSERVATION,
    )
    caption_claims = [claim for claim in result["claims"] if claim["claim_type"] == CLAIM_CAPTION_FACT]
    assert caption_claims and caption_claims[0]["supported"] is True
    assert caption_claims[0]["ref"]


def test_text_supported_interpretation_requires_main_text_binding():
    """作者推断句带经验证的正文芯片 → TEXT_SUPPORTED_INTERPRETATION（绑定 E2）。"""
    result = classify_explanation_claims(
        "作者据此认为 OsMYB73 在种子发育中发挥转录调控作用。〔证据E2｜正文·第6页｜paper.pdf〕",
        citations=_CITATIONS,
        locator=_LOCATOR,
        observation=_OBSERVATION,
    )
    interpretation = [claim for claim in result["claims"] if claim["claim_type"] == CLAIM_TEXT_SUPPORTED_INTERPRETATION]
    assert interpretation and interpretation[0]["ref"] == "E2"
    assert interpretation[0]["supported"] is True


def test_caption_chip_sentence_classifies_as_caption_fact():
    """题注芯片（补充材料分区）→ CAPTION_FACT，不误判为作者推断。"""
    result = classify_explanation_claims(
        "Figure 1 包含 OsMYB73 的表达谱检测。〔证据E1｜补充材料·第4页｜paper.pdf〕",
        citations=_CITATIONS,
        locator=_LOCATOR,
        observation=_OBSERVATION,
    )
    caption_claims = [claim for claim in result["claims"] if claim["claim_type"] == CLAIM_CAPTION_FACT]
    assert caption_claims and caption_claims[0]["ref"] == "E1"


def test_unbound_hard_constraint_is_unsupported_interpretation():
    """无正文反链的机制推断 → UNSUPPORTED（不得混在支持结论中静默输出）。"""
    result = classify_explanation_claims(
        "OsMYB73 直接调控 OsISA2 与 OsLTPL36 的表达。",
        citations=_CITATIONS,
        locator=_LOCATOR,
        observation=_OBSERVATION,
    )
    assert result["claims"][0]["claim_type"] == CLAIM_UNSUPPORTED_INTERPRETATION
    assert result["unsupported_count"] == 1


def test_backend_artifact_lines_are_not_classified():
    answer = (
        "已可靠定位到原文：〔引文定位｜补充材料·第4页｜paper.pdf〕\n\n"
        "【证据引用】（后端渲染，页码来自证据锚点）\n"
        "- E1｜补充材料·第4页｜paper.pdf｜ea-E1\n\n"
        "（注：以下结论未在原文中定位到对应依据，请谨慎采信）"
    )
    result = classify_explanation_claims(answer, citations=_CITATIONS, locator=_LOCATOR, observation=_OBSERVATION)
    assert result["claims"] == []


def test_qualitative_preamble_without_constraints_is_skipped():
    result = classify_explanation_claims(
        "这张图整体展示了表达模式。",
        citations=_CITATIONS,
        locator=_LOCATOR,
        observation=None,
    )
    # 无硬约束且无观察标记的铺垫句不构成可证伪科研结论
    assert result["claims"] == []
