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


# ---- v2：locator block 只引用 binding_id，页码由后端从绑定渲染 ----


def _verified_binding() -> dict:
    return {
        "status": "VERIFIED",
        "page": 4,
        "zone": "MAIN_TEXT",
        "filename": "osmyb73-paper.pdf",
        "binding": {"binding_id": "vlb_1", "page_number": 4, "status": "VERIFIED"},
    }


def test_v2_locator_block_renders_authoritative_chip_from_binding():
    rendered, validation = render_answer_draft(
        '<YUXI_ANSWER_DRAFT>{"schema_version":"answer-draft.v2","blocks":['
        '{"type":"locator","text":"定位行","binding_id":"vlb_1"},'
        '{"type":"paragraph","text":"该图展示 OsMYB73 表达谱。","evidence_refs":["E1"]}'
        ']}</YUXI_ANSWER_DRAFT>',
        locator_bindings={"vlb_1": _verified_binding()},
    )
    assert validation["status"] == "RENDERED_V2"
    assert validation["locator_blocks"] == "1"
    assert "已可靠定位到原文：〔引文定位｜正文·第4页｜osmyb73-paper.pdf〕" in rendered
    assert "该图展示 OsMYB73 表达谱。 [E1]" in rendered
    # 模型文本里不存在任何能变成页码的自由文本
    assert "4" not in rendered.replace("第4页", "").replace("[E1]", "")


def test_v2_locator_block_without_binding_renders_fail_closed():
    """没有 Binding 就没有页码：缺失绑定 → 失败关闭文案，绝不猜测。"""
    rendered, validation = render_answer_draft(
        '<YUXI_ANSWER_DRAFT>{"schema_version":"answer-draft.v2","blocks":['
        '{"type":"locator","text":"定位行","binding_id":"vlb_missing"}'
        ']}</YUXI_ANSWER_DRAFT>',
        locator_bindings={"vlb_1": _verified_binding()},
    )
    assert validation["status"] == "RENDERED_V2"
    assert "〔当前无法可靠定位原文页码〕" in rendered
    assert "第4页" not in rendered


def test_v2_locator_block_with_unverified_binding_renders_fail_closed():
    binding = {**_verified_binding(), "status": "NOT_FOUND"}
    binding.pop("page")
    rendered, _ = render_answer_draft(
        '{"schema_version":"answer-draft.v2","blocks":[{"type":"locator","text":"定位","binding_id":"vlb_1"}]}',
        locator_bindings={"vlb_1": binding},
    )
    assert "〔当前无法可靠定位原文页码〕" in rendered


def test_v2_draft_rejects_extra_fields_on_block():
    source = (
        '<YUXI_ANSWER_DRAFT>{"schema_version":"answer-draft.v2","blocks":['
        '{"type":"locator","text":"定位","binding_id":"vlb_1","page_number":4}'
        "]}</YUXI_ANSWER_DRAFT>"
    )
    rendered, validation = render_answer_draft(source, locator_bindings={"vlb_1": _verified_binding()})
    # 块级 extra=forbid：模型夹带 page_number → 整份草案回退，不部分采信
    assert validation["status"] == "INVALID_DRAFT_FALLBACK"
    assert rendered == source


def test_v1_drafts_remain_compatible():
    rendered, validation = render_answer_draft(
        '{"schema_version":"answer-draft.v1","blocks":[{"type":"paragraph","text":"v1 兼容","evidence_refs":[]}]}'
    )
    assert validation["status"] == "RENDERED"
    assert rendered == "v1 兼容"
