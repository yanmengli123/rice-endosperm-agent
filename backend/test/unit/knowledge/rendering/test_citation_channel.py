"""确定性引用通道单测：锚点→引用构建、分区、图目录降级、输出门禁（事故回归）。"""

from types import SimpleNamespace

from yuxi.knowledge.rendering.citation_channel import (
    NARRATIVE_LOCATOR_MARKER,
    ZONE_MAIN_TEXT,
    ZONE_SUPPORTING_INFO,
    apply_citation_channel,
    build_citation_rows,
    expand_placeholders,
    extract_anchor_ids_from_content,
    format_pages,
    is_toc_line,
    render_citation_chip,
    reverse_bind_citations,
    sanitize_history_text,
    strip_bare_locators,
    zone_of_page,
)
from yuxi.knowledge.evidence.quote_locator import resolve_quote_locator_from_citations

MAIN_ANCHOR_ID = "ea_8eac914e8f48cd1bd307f9cb6df7f35978e240c1"
SI_TOC_ANCHOR_ID = "ea_d82d0245a08d70c653cc5d4426d5688439a9fbf4"


def _anchor(anchor_id, page, quote, anchor_type="text", partition="UNKNOWN"):
    return SimpleNamespace(
        anchor_id=anchor_id,
        parse_revision_id="pr_active",
        page=page,
        quote=quote,
        anchor_type=anchor_type,
        locatable=True,
        document_partition=partition,
    )


MAIN_PARAGRAPH_QUOTE = (
    "Rice endosperm starch biosynthesis is a critical factor ... The structure of OsMYB73 "
    "protein was also predicted and the results revealed that it has two typical SANT domains "
    "between 115–164 and 167–215 amino acids."
)
SI_TOC_QUOTE = "Figure S1 Phylogenetic analysis and protein domain prediction of rice OsMYB73."


def _evidence_rows():
    return [
        {
            "evidence_id": "evdoc_main",
            "kb_id": "kb_1",
            "file_id": "file_main",
            "parse_revision_id": "pr_active",
            "content": f"【章节】论文标题\n【页码】3\n【证据锚点】{MAIN_ANCHOR_ID}\n正文段落……",
        },
        {
            "evidence_id": "evdoc_si",
            "kb_id": "kb_1",
            "file_id": "file_main",
            "parse_revision_id": "pr_active",
            "evidence_type": "toc_line",
            "content": f"【章节】论文标题\n【页码】17\n【证据锚点】{SI_TOC_ANCHOR_ID}\n{SI_TOC_QUOTE}",
        },
    ]


def _anchor_index():
    return {
        ("pr_active", MAIN_ANCHOR_ID): _anchor(MAIN_ANCHOR_ID, 3, MAIN_PARAGRAPH_QUOTE),
        ("pr_active", SI_TOC_ANCHOR_ID): _anchor(SI_TOC_ANCHOR_ID, 17, SI_TOC_QUOTE),
    }


def _citations():
    return build_citation_rows(
        _evidence_rows(),
        anchor_index=_anchor_index(),
        si_start_by_file={"file_main": 17},
        filename_by_file={"file_main": "Plant Biotechnology Journal - 2024 - Liu - OsMYB73.pdf"},
    )


# ---------- 基础归一化 ----------


def test_extract_anchor_ids_from_footer():
    content = "【章节】标题\n【页码】3\n【证据锚点】ea_aaa、ea_bbb, ea_aaa\n正文"
    assert extract_anchor_ids_from_content(content) == ["ea_aaa", "ea_bbb"]


def test_toc_line_and_zone_detection():
    assert is_toc_line(SI_TOC_QUOTE, evidence_type="toc_line")
    assert not is_toc_line(MAIN_PARAGRAPH_QUOTE)
    assert zone_of_page(16, 17) == ZONE_MAIN_TEXT
    assert zone_of_page(17, 17) == ZONE_SUPPORTING_INFO
    assert zone_of_page(3, None) == ZONE_MAIN_TEXT


def test_format_pages_collapses_contiguous_runs():
    assert format_pages([3]) == "3"
    assert format_pages([13, 14]) == "13-14"
    assert format_pages([3, 5, 6, 8]) == "3、5-6、8"


# ---------- 引用构建（事故场景回归） ----------


def test_main_text_citation_gets_authoritative_page_three():
    citations = _citations()
    assert citations[0]["ref"] == "E1"
    assert citations[0]["zone"] == ZONE_MAIN_TEXT
    assert citations[0]["page_numbers"] == [3]
    assert citations[0]["locatable"] is True
    assert citations[0]["toc_line"] is False


def test_si_toc_line_citation_is_secondary_and_zoned():
    citations = _citations()
    si = citations[1]
    assert si["ref"] == "E2"
    assert si["zone"] == ZONE_SUPPORTING_INFO
    assert si["toc_line"] is True
    assert si["locatable"] is False


def test_missing_anchor_fails_closed_as_unlocatable():
    rows = [
        {
            "evidence_id": "ev_x",
            "kb_id": "kb_1",
            "file_id": "file_main",
            "content": "【证据锚点】ea_deadbeef\n正文",
        }
    ]
    citations = build_citation_rows(rows, anchor_index={}, si_start_by_file={}, filename_by_file={})
    assert citations[0]["locatable"] is False
    chip = render_citation_chip(citations[0])
    assert chip == "〔证据E1｜无法定位页码〕"


def test_quote_locator_row_preserves_complete_typed_lineage():
    quote = (
        "Figure S8 Rice grain starch physicochemical characteristics comparison of wild-type "
        "ZH11 and mutants cr-myb73-35 and cr-myb73-46 in T1 generation."
    )
    anchor_id = "ea_figure_s8"
    citations = build_citation_rows(
        [
            {
                "evidence_id": "ev_physical_s8",
                "span_evidence_id": "evs_span_s8",
                "span_id": "es_span_s8",
                "retrieval_channel": "QUOTE_LOCATOR",
                "kb_id": "kb_1",
                "file_id": "file_main",
                "parse_revision_id": "pr_active",
                "_active_index_revision_id": "ir_active",
                "_source_sha256": "sha256:source",
                "anchor_id": anchor_id,
                "page_number": 17,
                "evidence_quote": quote,
            }
        ],
        anchor_index={
            ("pr_active", anchor_id): _anchor(
                anchor_id,
                17,
                quote,
                partition="SUPPORTING_INFO",
            )
        },
        si_start_by_file={},
        filename_by_file={"file_main": "supporting-information.pdf"},
    )

    citation = citations[0]
    assert citation["anchor_ids"] == [anchor_id]
    assert citation["_span_id"] == "es_span_s8"
    assert citation["_span_evidence_id"] == "evs_span_s8"
    assert citation["_physical_evidence_id"] == "ev_physical_s8"
    assert citation["_retrieval_channel"] == "QUOTE_LOCATOR"
    resolved = resolve_quote_locator_from_citations(quote_text=quote, citations=citations)
    assert resolved["status"] == "VERIFIED"
    assert resolved["page"] == 17
    assert resolved["anchor_id"] == anchor_id


def test_same_sentence_si_reference_points_to_main_text():
    """同句在正文与 SI 各有锚点：SI 引用标 secondary_of 指向正文引用（正文优先）。"""
    si_anchor = _anchor("ea_si_same", 17, MAIN_PARAGRAPH_QUOTE)
    rows = [
        {
            "evidence_id": "ev_main",
            "file_id": "f1",
            "parse_revision_id": "pr_active",
            "content": f"【证据锚点】ea_main_same\n{MAIN_PARAGRAPH_QUOTE}",
        },
        {
            "evidence_id": "ev_si",
            "file_id": "f1",
            "parse_revision_id": "pr_active",
            "content": f"【证据锚点】ea_si_same\n{MAIN_PARAGRAPH_QUOTE}",
        },
    ]
    citations = build_citation_rows(
        rows,
        anchor_index={
            ("pr_active", "ea_main_same"): _anchor("ea_main_same", 3, MAIN_PARAGRAPH_QUOTE),
            ("pr_active", "ea_si_same"): si_anchor,
        },
        si_start_by_file={"f1": 17},
        filename_by_file={"f1": "paper.pdf"},
    )
    assert citations[0]["secondary_of"] is None
    assert citations[1]["secondary_of"] == "E1"
    assert "同句正文见E1" in render_citation_chip(citations[1])


# ---------- 输出门禁（三种历史错误形态都必须被拦截） ----------


def _guard(text):
    return apply_citation_channel(text, _citations())


def test_guard_strips_model_written_bare_page_and_anchor():
    # 事故形态 1：正文 p. 4 + 自写锚点
    guarded, validation = _guard("该句位于正文 p. 4，锚点 ea_8eac914e8f48cd1bd307f9cb6df7f35978e240c1。")
    assert "p. 4" not in guarded
    assert "ea_8eac" not in guarded
    assert NARRATIVE_LOCATOR_MARKER not in guarded  # v3：行内标记不上屏
    assert "未在原文中定位到对应依据" in guarded  # 文末统一提示
    assert validation["changed"] is True
    assert validation["locator"]["status"] == "SANITIZED"


def test_guard_strips_si_page_claim():
    # 事故形态 2：Supporting Information 第 17 页
    guarded, _ = _guard("该句出现在 Supporting Information 第 17 页。")
    assert "第 17 页" not in guarded
    assert "未在原文中定位到对应依据" in guarded


def test_guard_expands_placeholder_to_authoritative_chip():
    guarded, validation = _guard("该句位于论文正文 [E1]，两个 SANT 结构域位于 115–164 与 167–215 aa。")
    assert "第3页" in guarded
    assert "正文·第3页" in guarded
    assert "[E1]" not in guarded
    assert validation["placeholder_expanded"] == 1
    # 数值区间（氨基酸位置）不是页码，不能被误伤
    assert "115–164" in guarded and "167–215" in guarded


def test_placeholder_without_claim_context_fails_closed():
    """合法 ref 但没有可验证 Claim 上下文时也不能产生页码。"""
    guarded, _ = _guard("见 [E1]。")
    assert "正文·第3页" not in guarded
    assert "未在原文中定位到对应依据" in guarded


def test_guard_unknown_placeholder_degrades_to_marker():
    guarded, _ = _guard("参见 [E99]。")
    assert "[E99]" not in guarded
    assert "未在原文中定位到对应依据" in guarded


def test_guard_passes_soft_text_unchanged():
    """软句子（无硬约束：无数字/基因符号/缩写）不触发反向绑定，原样通过。"""
    text = "该蛋白可能参与下游靶基因的转录调控过程。"
    guarded, validation = _guard(text)
    assert guarded == text
    assert validation["changed"] is False
    assert validation["reverse_bound"] == 0


def test_guard_reverse_binds_hard_constraint_sentence():
    """含硬约束（基因符号+缩写）的句子即使模型没写 [E#] 也自动补权威芯片（有理有据）。"""
    text = "OsMYB73 含两个 SANT 结构域。"
    guarded, validation = _guard(text)
    assert validation["reverse_bound"] == 1
    assert "〔证据E1｜正文·第3页｜" in guarded


def test_guard_without_citations_still_strips_bare_pages():
    guarded, validation = apply_citation_channel("该句在第 4 页。", [])
    assert "第 4 页" not in guarded
    assert validation["changed"] is True


def test_guard_never_appends_retrieval_candidate_pages():
    """未验证时不能把所有候选页码倾倒给用户。"""
    guarded, validation = _guard("该句位于 Supporting Information 第 17 页。")
    assert "第 17 页" not in guarded
    assert "证据定位（后端权威渲染）：" not in guarded
    assert "未在原文中定位到对应依据" in guarded
    assert validation["placeholder_expanded"] == 0


def test_guard_does_not_append_chips_for_clean_answer_without_placeholder():
    guarded, validation = _guard("OsMYB73 含两个 SANT 结构域。")
    assert "证据定位" not in guarded
    assert validation["placeholder_expanded"] == 0


def test_guard_does_not_expand_parroted_marker_into_candidate_pages():
    guarded, validation = _guard("该句位置〔页码与锚点以后端引用为准〕，Supporting Information。")
    assert "证据定位（后端权威渲染）：" not in guarded
    assert "第3页" not in guarded
    assert validation["placeholder_expanded"] == 0


def test_guard_does_not_touch_journal_four_digit_pages():
    text = "发表于 Plant Biotechnology Journal, 2025, 23(4): 1021–1038。"
    guarded, _ = _guard(text)
    assert "1021–1038" in guarded


def test_expand_placeholders_counts_only():
    text, count, bindings = expand_placeholders(f"{MAIN_PARAGRAPH_QUOTE} [E1]", _citations())
    assert count == 1
    assert bindings[0]["status"] == "VERIFIED"
    assert "〔证据E1｜正文·第3页｜" in text


def test_wrong_proposed_ref_is_rebound_to_supporting_evidence():
    other = _anchor("ea_other", 1, "Unrelated abstract sentence about rice grain quality.")
    citations = build_citation_rows(
        [
            {
                "evidence_id": "ev_other",
                "file_id": "file_main",
                "parse_revision_id": "pr_active",
                "content": "【证据锚点】ea_other",
            },
            {
                "evidence_id": "ev_true",
                "file_id": "file_main",
                "parse_revision_id": "pr_active",
                "content": f"【证据锚点】{MAIN_ANCHOR_ID}",
            },
        ],
        anchor_index={
            ("pr_active", "ea_other"): other,
            ("pr_active", MAIN_ANCHOR_ID): _anchor(MAIN_ANCHOR_ID, 3, MAIN_PARAGRAPH_QUOTE),
        },
        si_start_by_file={},
        filename_by_file={"file_main": "paper.pdf"},
    )
    guarded, validation = apply_citation_channel(f"{MAIN_PARAGRAPH_QUOTE} [E1]", citations)
    assert "证据E2｜正文·第3页" in guarded
    assert validation["bindings"][0]["corrected_from"] == "E1"


def test_fabricated_valid_ref_with_unrelated_claim_is_removed():
    guarded, validation = _guard("A completely unrelated claim. 〔证据E1｜正文·第3页｜paper.pdf〕")
    assert "第3页" not in guarded
    assert validation["fabrication"]["fabricated_removed"] == 1


def test_history_projection_removes_rendered_citations_without_restoring_ref():
    cleaned = sanitize_history_text("结论。〔证据E1｜正文·第3页｜paper.pdf〕〔页码与锚点以后端引用为准〕")
    assert cleaned == "结论。[citation omitted]"
    assert "[E1]" not in cleaned


def test_strip_bare_locators_collapses_adjacent_markers():
    stripped, validation = strip_bare_locators("见第 3 页、第 4 页与 p. 5")
    assert stripped.count(NARRATIVE_LOCATOR_MARKER) == 1
    assert validation["status"] == "SANITIZED"


# ---------- 小数伪影保护（Figure 3 事故：芯片插进 "1 . 0 cm" 中间） ----------


def test_reverse_bind_never_inserts_chip_inside_decimal_numbers():
    """PDF 伪影小数（点两侧带空格）不是句界：芯片不得插进数字中间。"""
    import re as _re
    import unicodedata as _ud

    def _norm(text: str) -> str:
        value = _ud.normalize("NFKC", text)
        return _re.sub(r"\s+", " ", _re.sub(r"[^0-9a-z一-鿿]+", " ", value.casefold())).strip()

    sentence = "Starch solubility was measured in 1 . 7% KOH solution with scale bars of 1. 0 cm for OsMYB73 panels."
    carrier_quote = (
        "Starch solubility was measured in 1 . 7% KOH solution with scale bars of 1. 0 cm for OsMYB73 panels. "
        "Additional methods context follows here."
    )
    citations = [
        {
            "ref": "E1",
            "evidence_id": "ev-E1",
            "kb_id": "kb-a",
            "file_id": "file-a",
            "filename": "paper.pdf",
            "zone": "MAIN_TEXT",
            "page_numbers": [7],
            "primary_page": 7,
            "quote_head": carrier_quote[:80],
            "anchor_ids": ["ea-E1"],
            "locatable": True,
            "toc_line": False,
            "secondary_of": None,
            "_anchor_id": "ea-E1",
            "_physical_evidence_id": "ev-E1",
            "_quote": carrier_quote,
            "_quote_norm": _norm(carrier_quote),
        }
    ]
    out, bound, _uncovered = reverse_bind_citations(
        sentence + " Unrelated trailing sentence without citations.", citations
    )

    assert "\x00" not in out
    # 伪影小数被切分保护并规范化（"1 . 7" → "1.7"），芯片绝不落在数字内部
    assert "1.7% KOH" in out
    assert "1.0 cm" in out
    decimal_zone = out[out.index("1.7%") : out.index("cm for OsMYB73 panels.") + len("cm for OsMYB73 panels.")]
    assert "〔" not in decimal_zone
    # 绑定成功：芯片在真实句末（panels. 之后），不在句中
    assert bound == 1
    assert "panels. 〔证据E1｜正文·第7页｜paper.pdf〕" in out
