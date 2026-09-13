from yuxi.knowledge.rendering.source_output_guard import guard_non_document_source_answer


def test_mcp_only_answer_cannot_emit_document_evidence_affordances():
    guarded, audit = guard_non_document_source_answer(
        "Wx 的结构化记录如下 [E2]，位于第 17 页。"
        "〔证据E2｜补充材料·第17页｜paper.pdf〕\n\n"
        "【证据引用】（后端渲染）\n- E2｜正文·第17页｜paper.pdf｜ea_1234567890abcdef"
    )

    assert "[E2]" not in guarded
    assert "证据E2" not in guarded
    assert "【证据引用】" not in guarded
    assert "ea_123" not in guarded
    assert "第 17 页" not in guarded
    assert "当前数据来源不提供 PDF 物理页码" in guarded
    assert audit["evidence_refs_removed"] == 1
    assert audit["reference_blocks_removed"] == 1
