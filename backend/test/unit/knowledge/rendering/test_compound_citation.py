"""复合意图与引用完备性单测：意图分解、locator 引用并入、反向绑定、定位行保障、未依据明示。"""


from yuxi.knowledge.evidence.quote_locator import (
    LOCATOR_KIND_QUOTE,
    SUB_INTENT_EXPLANATION,
    SUB_INTENT_LOCATOR,
    decompose_question_intents,
)
from yuxi.knowledge.rendering.citation_channel import (
    NARRATIVE_LOCATOR_MARKER,
    append_locator_citations,
    apply_citation_channel,
    reverse_bind_citations,
)

SANT_QUOTE = (
    "Rice endosperm starch biosynthesis is a critical factor. The structure of OsMYB73 "
    "protein was also predicted and the results revealed that it has two typical SANT "
    "domains between 115-164 and 167-215 amino acids."
)


def _citation(ref: str, page: int, quote: str, *, zone: str = "MAIN_TEXT") -> dict:
    return {
        "ref": ref,
        "evidence_id": f"ev-{ref}",
        "kb_id": "kb-1",
        "file_id": "file-1",
        "filename": "paper.pdf",
        "zone": zone,
        "page_numbers": [page],
        "primary_page": page,
        "quote_head": quote[:80],
        "anchor_ids": [f"ea-{ref}"],
        "locatable": True,
        "toc_line": False,
        "secondary_of": None,
        "_quote_norm": __import__("yuxi.knowledge.rendering.claim_evidence_resolver", fromlist=["normalize_for_match"])
        .normalize_for_match(quote),
    }


# ---------- 意图分解 ----------


def test_decompose_compound_locator_plus_explanation():
    result = decompose_question_intents(
        "The structure of OsMYB73 protein was also predicted and the results revealed "
        "这句原文出现在论文的正文第几页，具体是什么意思？"
    )
    assert result["kind"] == LOCATOR_KIND_QUOTE
    assert result["sub_intents"] == [SUB_INTENT_LOCATOR, SUB_INTENT_EXPLANATION]
    assert result["compound"] is True
    assert "OsMYB73" in result["quote_text"]


def test_decompose_pure_locator_stays_single_intent():
    result = decompose_question_intents(
        "The structure of OsMYB73 protein was also predicted and the results revealed "
        "这句原文出现在论文的正文第几页？"
    )
    assert result["sub_intents"] == [SUB_INTENT_LOCATOR]
    assert result["compound"] is False


def test_decompose_explanation_only_is_not_locator():
    result = decompose_question_intents("OsMYB73 的 SANT 结构域是什么意思？")
    assert result["kind"] is None
    assert result["compound"] is False


# ---------- locator 引用并入 ----------


def test_append_locator_citations_includes_backlinks():
    locator = {
        "status": "VERIFIED",
        "page": 17,
        "zone": "SUPPORTING_INFO",
        "anchor_id": "ea-cap",
        "quote": "Figure S8 Rice grain starch physicochemical characteristics comparison.",
        "quote_head": "Figure S8 Rice grain starch physicochemical...",
        "filename": "paper.pdf",
        "file_id": "file-1",
        "kb_id": "kb-1",
        "evidence_id": "ev-cap",
        "backlinks": [
            {
                "page": 6,
                "zone": "MAIN_TEXT",
                "anchor_id": "ea-main",
                "quote": "The wild-type and the two mutants were having soft gel consistency (>=61 mm).",
                "quote_head": "The wild-type and the two mutants...",
                "filename": "paper.pdf",
                "file_id": "file-1",
                "kb_id": "kb-1",
            }
        ],
    }
    base = [_citation("E1", 3, SANT_QUOTE)]
    merged = append_locator_citations(base, locator)
    assert [row["ref"] for row in merged] == ["E1", "E2", "E3"]
    assert merged[1]["page_numbers"] == [17] and merged[1]["zone"] == "SUPPORTING_INFO"
    assert merged[1]["toc_line"] is True  # 图注本身标注 toc_line，禁止作为正文句定位
    assert merged[2]["page_numbers"] == [6] and merged[2]["zone"] == "MAIN_TEXT"


def test_append_locator_citations_ignores_unverified():
    base = [_citation("E1", 3, SANT_QUOTE)]
    assert append_locator_citations(base, {"status": "NOT_FOUND"}) == base
    assert append_locator_citations(base, None) == base


# ---------- 反向绑定与未依据明示 ----------


def test_reverse_binding_adds_chip_to_uncited_hard_sentence():
    citations = [_citation("E1", 3, SANT_QUOTE), _citation("E2", 8, "Unrelated localization content GFP nucleus.")]
    text = "OsMYB73 含两个 SANT 结构域，位于 115-164 与 167-215 氨基酸之间。"
    bound, count, uncovered = reverse_bind_citations(text, citations)
    assert count == 1
    assert "〔证据E1｜正文·第3页｜paper.pdf〕" in bound
    assert uncovered == []


def test_reverse_binding_skips_cited_and_soft_sentences():
    citations = [_citation("E1", 3, SANT_QUOTE)]
    text = "结构域位于 115-164 aa〔证据E1｜正文·第3页｜paper.pdf〕。这概括了蛋白特征。"
    bound, count, uncovered = reverse_bind_citations(text, citations)
    assert count == 0
    assert bound.count("〔证据E1") == 1  # 不重复标注
    assert uncovered == []


def test_reverse_binding_reports_uncovered_claim():
    citations = [_citation("E1", 3, SANT_QUOTE)]
    text = "OsMYB73 定位于细胞核并具有激酶活性 9999 位点。"
    _bound, count, uncovered = reverse_bind_citations(text, citations)
    assert count == 0
    assert uncovered and "9999" in uncovered[0]


# ---------- 定位行保障（apply 集成） ----------


def _locator() -> dict:
    return {
        "status": "VERIFIED",
        "page": 17,
        "zone": "SUPPORTING_INFO",
        "filename": "Plant Biotechnology Journal - 2024 - Liu - OsMYB73.pdf",
    }


def test_apply_prepends_locator_line_for_compound_answer():
    citations = [_citation("E1", 6, "The wild-type and the two mutants were having soft gel consistency.")]
    guarded, validation = apply_citation_channel(
        "这是补充材料中的图注，展示了突变体与野生型的淀粉理化特性对比。", citations, locator=_locator()
    )
    assert guarded.startswith("已可靠定位到原文：〔引文定位｜补充材料·第17页｜")
    assert validation["locator_line_prepended"] is True


def test_apply_does_not_duplicate_locator_line():
    citations = [_citation("E1", 6, "The wild-type and the two mutants were having soft gel consistency.")]
    text = (
        "已可靠定位到原文：〔引文定位｜补充材料·第17页｜Plant Biotechnology Jou…〕\n\n"
        "该图注描述了 T1 代种子的淀粉理化特性。"
    )
    guarded, _validation = apply_citation_channel(text, citations, locator=_locator())
    assert guarded.count("已可靠定位到原文") == 1


def test_bifc_wrong_model_page_is_replaced_by_frozen_page15():
    """事故回归：模型写第 9 页时，最终答案只能保留冻结锚点的第 15 页。"""
    locator = {
        "status": "VERIFIED",
        "page": 15,
        "zone": "MAIN_TEXT",
        "filename": "Plant Biotechnology Journal - 2024 - Liu - OsMYB73.pdf",
    }
    citations = [
        _citation(
            "E1",
            15,
            "Two pairs of constructs, OsMYB73-VN173 and OsNF-YB1-VC155, were transformed into tobacco leaf cells.",
        )
    ]
    guarded, validation = apply_citation_channel(
        "已可靠定位到原文：〔引文定位｜正文·第9页｜paper.pdf〕\n\n"
        "这是一段 BiFC 实验方法描述。",
        citations,
        locator=locator,
    )

    assert "第9页" not in guarded
    assert guarded.startswith("已可靠定位到原文：〔引文定位｜正文·第15页｜")
    assert guarded.count("已可靠定位到原文") == 1
    assert validation["changed"] is True


def test_apply_appends_uncovered_notice():
    citations = [_citation("E1", 3, SANT_QUOTE)]
    guarded, validation = apply_citation_channel(
        "OsMYB73 定位于细胞核并具有激酶活性 9999 位点。", citations
    )
    assert "未在原文中定位到对应依据" in guarded
    assert validation["uncovered_claims"]


def test_reverse_binding_converts_mimicked_marker_to_chip():
    """模型模仿的 fail-closed 标记视为引用意图：VERIFIED 替换为真芯片。"""
    citations = [_citation("E1", 3, SANT_QUOTE)]
    text = f"OsMYB73 含两个 SANT 结构域，位于 115-164 aa。 {NARRATIVE_LOCATOR_MARKER}"
    bound, count, uncovered = reverse_bind_citations(text, citations)
    assert count == 1
    assert "〔证据E1｜正文·第3页｜paper.pdf〕" in bound
    assert uncovered == []


def test_reverse_binding_records_unresolvable_marker_sentence():
    """v3：绑定失败的标记句不再保留行内标记——记入未定位清单，由文末提示统一披露。"""
    citations = [_citation("E1", 3, SANT_QUOTE)]
    text = f"OsMYB73 具有激酶活性 9999 位点。 {NARRATIVE_LOCATOR_MARKER}"
    bound, count, uncovered = reverse_bind_citations(text, citations)
    assert count == 0
    assert uncovered and "9999" in uncovered[0]  # 进文末提示，不静默


def test_ensure_locator_line_dedupes_with_minor_variance():
    """模型写的定位行与后端行有微小差异（多余空格）也只保留权威行一次。"""
    citations = [_citation("E1", 6, "The wild-type and the two mutants were having soft gel consistency.")]
    text = (
        "已可靠定位到原文：〔引文定位｜补充材料·第17页｜Plant Biotechnology Journal - 2024 - Liu - OsMYB73.pdf〕\n\n"
        "正文。\n"
        "已可靠定位到原文：〔引文定位｜补充材料·第17页｜Plant Biotechnology Journal - 2024 - Liu - OsMYB73.pdf〕\n"
    )
    guarded, _validation = apply_citation_channel(text, citations, locator=_locator())
    assert guarded.count("已可靠定位到原文") == 1
    assert guarded.startswith("已可靠定位到原文：〔引文定位｜补充材料·第17页｜Plant Biotechnology Jou…〕")
