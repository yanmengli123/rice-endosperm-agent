from yuxi.knowledge.rendering.answer_draft import render_answer_draft


def test_structured_answer_draft_is_rendered_by_backend():
    rendered, validation = render_answer_draft(
        '<YUXI_ANSWER_DRAFT>{"schema_version":"answer-draft.v1","blocks":['
        '{"type":"heading","text":"结论","evidence_refs":[]},'
        '{"type":"paragraph","text":"Figure S8 比较野生型和突变体。","evidence_refs":["E2"]}'
        ']}</YUXI_ANSWER_DRAFT>'
    )

    assert rendered == "## 结论\n\nFigure S8 比较野生型和突变体。 [E2]"
    assert validation["status"] == "RENDERED"


def test_invalid_answer_draft_falls_back_without_partial_rewrite():
    source = '<YUXI_ANSWER_DRAFT>{"blocks":"broken"}</YUXI_ANSWER_DRAFT>'
    rendered, validation = render_answer_draft(source)

    assert rendered == source
    assert validation["status"] == "INVALID_DRAFT_FALLBACK"


def test_renders_draft_inside_code_fence_after_locator_line():
    """2026-09 事故回归：模型把草案 JSON 包进 ```json 围栏且前面有后端定位行。"""
    from yuxi.knowledge.rendering.answer_draft import render_answer_draft

    text = (
        "已可靠定位到原文：〔引文定位｜补充材料·第17页｜paper.pdf〕\n\n"
        "```json\n"
        '{"schema_version":"answer-draft.v1","blocks":[{"type":"heading","text":"定位","evidence_refs":["E1"]}]}'
        "\n```\n"
    )
    out, meta = render_answer_draft(text)
    assert meta["status"] == "RENDERED"
    assert out.startswith("## 定位")
    assert "```" not in out and "schema_version" not in out


def test_bare_json_anywhere_with_signature_is_extracted():
    from yuxi.knowledge.rendering.answer_draft import render_answer_draft

    text = (
        "前置说明\n"
        '{"schema_version":"answer-draft.v1","blocks":[{"type":"paragraph","text":"内容","evidence_refs":[]}]}\n'
        "后置文本"
    )
    out, meta = render_answer_draft(text)
    assert meta["status"] == "RENDERED"
    assert "内容" in out


def test_plain_json_code_block_without_signature_not_touched():
    from yuxi.knowledge.rendering.answer_draft import render_answer_draft

    text = "```json\n" + '{"other": 1}' + "\n```"
    out, meta = render_answer_draft(text)
    assert meta["status"] == "LEGACY_MARKDOWN"
    assert out == text
