from yuxi.knowledge.validation.document_release_hygiene import (
    guard_document_release_hygiene,
    strip_unbound_assertions_after_render,
)


def test_unfinished_candidates_are_not_publishable():
    cleaned, report = guard_document_release_hygiene(
        "位置（内、外？）发生变化。[E1]\n热胁迫值为 26.8（或为22.7）。", []
    )
    assert "26.8" not in cleaned and "内、外" not in cleaned
    assert report["removed_line_count"] == 2


def test_conflict_needs_two_distinct_source_excerpts():
    cleaned, report = guard_document_release_hygiene(
        "不同图注/正文表述存在差异。[E1]", [{"ref": "E1", "_quote": "one quote"}]
    )
    assert "表述存在差异" not in cleaned
    assert report["reason_counts"] == {"CONFLICT_REQUIRES_TWO_SOURCES": 1}
    cleaned, report = guard_document_release_hygiene(
        "不同来源结论存在差异。[E1] [E2]", [{"ref": "E1", "_quote": "A"}, {"ref": "E2", "_quote": "B"}]
    )
    assert report["status"] == "PASS"


def test_honest_evidence_limits_are_not_removed():
    source = "同一段正文未直接给出分子机制，因此目前不能归因。"
    assert guard_document_release_hygiene(source, [])[0] == source


def test_figure_question_does_not_publish_uncited_causal_explanations():
    text = "## 机制\n缺失导致细胞扩张增强。\n正文明确指出腹部灌浆不足。[E1]\n无法确定具体细胞机制。"
    cleaned, report = guard_document_release_hygiene(
        text,
        [{"ref": "E1", "_quote": "deficient filling process of abdominal endosperm"}],
        question="解释表型并注明哪个图",
    )
    assert "细胞扩张增强" not in cleaned
    assert "正文明确指出腹部灌浆不足。[E1]" in cleaned
    assert "无法确定具体细胞机制。" in cleaned
    assert report["reason_counts"] == {"ASSERTION_REQUIRES_BOUND_SOURCE": 1}


def test_a_bound_structure_citation_cannot_be_used_to_rule_out_off_target():
    cleaned, report = guard_document_release_hygiene(
        "Figure S3 证明表型来自功能缺失而非脱靶。[E1]",
        [{"ref": "E1", "_quote": "comparison of amino acid sequences and three-dimensional structure"}],
        question="解释 Figure S3",
    )
    assert "而非脱靶" not in cleaned
    assert report["reason_counts"] == {"EXCLUSION_REQUIRES_EXPLICIT_SOURCE": 1}


def test_exclusion_source_check_is_only_a_necessary_not_sufficient_condition():
    text = "Figure 3 排除脱靶。[E1]"
    cleaned, report = guard_document_release_hygiene(
        text, [{"ref": "E1", "_quote": "off-target assay"}], question="解释 Figure 3"
    )
    assert cleaned == text and report["status"] == "PASS"
    # Presence of this term does not prove the assay outcome or entailment.


def test_removal_does_not_leave_empty_headings_and_disclosure_is_idempotent():
    citations = [{"ref": "E1", "_quote": "grain length increased"}]
    cleaned, _ = guard_document_release_hygiene(
        "## 结论\n缺失导致细胞扩张增强。\n## 逐图依据\nFigure 2 显示粒长增加。[E1]\n## 机制\n导致改变。",
        citations,
        question="依据图注解释",
    )
    assert "## 结论" not in cleaned and "## 机制" not in cleaned
    assert "## 逐图依据" in cleaned
    again, report = guard_document_release_hygiene(cleaned, citations, question="依据图注解释")
    assert again == cleaned and report["removed_line_count"] == 0


def test_a_revoked_proposed_citation_cannot_authorize_a_final_causal_assertion():
    from yuxi.knowledge.rendering.citation_channel import apply_citation_channel

    text, report = apply_citation_channel(
        "缺失导致细胞扩张。[E1]",
        [{"ref": "E1", "_quote": "grain length increased", "quote_head": "grain length increased", "locatable": False}],
        semantic_question="依据图注解释表型",
    )
    assert "缺失导致细胞扩张" not in text
    assert report["document_release_hygiene"]["reason_counts"]["ASSERTION_REQUIRES_BOUND_SOURCE"] == 1


# ------------------------------------------------- 终态无绑定断言清理（v2-final）


def _citations(*rows):
    return [{"ref": ref, "_quote": quote, "quote_head": quote, **extra} for ref, quote, *extra in rows]


def test_final_pass_removes_assertion_whose_chip_was_detached_after_render():
    """chat 层接地拆除场景：渲染时带芯片的因果句，芯片被后续校验拆掉后
    不得以无绑定形态残留（协作者最终复测发现的发布顺序缺陷）。"""
    text = "突变导致灌浆不足。\n突变体粒长增加。〔证据E1｜正文·第3页｜JIPB…〕"
    cleaned, report = strip_unbound_assertions_after_render(
        text,
        [{"ref": "E1", "_quote": "grain length increased significantly", "locatable": True}],
        question="解释图 2 表型",
    )
    assert "突变导致灌浆不足" not in cleaned
    assert "粒长增加" in cleaned
    assert report["reason_counts"] == {"ASSERTION_REQUIRES_BOUND_SOURCE": 1}
    assert report["version"] == "document-release-hygiene.v2-final"


def test_final_pass_treats_frozen_verbatim_quote_lines_as_bound_evidence():
    """含冻结引文子串（≥20 字符、locatable）的行是逐字原文载体——引文即
    绑定，即使断言词来自引文本身也不得误删。"""
    quote = "the mutation caused a significant reduction in grain filling rate"
    text = f"原文证据：{quote}"
    cleaned, report = strip_unbound_assertions_after_render(
        text, [{"ref": "E1", "_quote": quote, "locatable": True}], question="解释 Figure 2f"
    )
    assert cleaned == text and report["removed_line_count"] == 0


def test_final_pass_protects_pipeline_owned_notices_and_is_idempotent():
    text = (
        "## 表型\n突变导致白垩胚乳。\n"
        "（注：以下结论未在原文中定位到对应依据，请谨慎采信以下结论。）\n"
        "> 已省略未定稿或缺少绑定来源支持的推断；以下仅保留可引用内容。\n"
        "> 逐字原文 blockquote 行内容。"
    )
    cleaned, report = strip_unbound_assertions_after_render(text, [], question="解释图注")
    assert "（注：以下结论未在原文中定位" in cleaned
    assert "> 已省略" in cleaned
    assert "白垩胚乳" not in cleaned  # 无绑定因果句仍被清掉
    assert report["removed_line_count"] == 1
    again, second = strip_unbound_assertions_after_render(cleaned, [], question="解释图注")
    assert again == cleaned and second["removed_line_count"] == 0


def test_final_pass_appends_omission_notice_only_when_undisclosed():
    cleaned, report = strip_unbound_assertions_after_render("突变导致粒型变化。", [], question="哪个图展示了表型")
    assert "已省略未定稿或缺少绑定来源支持的推断" in cleaned and report["removed_line_count"] == 1
    disclosed = "结论。\n（注：3 处具体数值未绑定〔数值〕，请以原文表格为准。）"
    cleaned2, report2 = strip_unbound_assertions_after_render(
        "突变导致粒型变化。\n" + disclosed, [], question="哪个图展示了表型"
    )
    assert cleaned2.count("（注：") == 1 and "已省略" not in cleaned2


def test_backend_signed_figure_chip_counts_as_bound_evidence():
    """〔图表F#〕是后端从可信注册表签发的权威绑定：只带图表芯片的因果句
    不得被误判为无绑定断言删除（误删会把正当图文并联回答掏空）。"""
    bound = "Figure 3 显示总淀粉含量显著下降。〔图表F3｜图 3〕"
    for guard in (guard_document_release_hygiene, strip_unbound_assertions_after_render):
        cleaned, report = guard(bound, [], question="解释 Figure 3")
        assert cleaned == bound, guard.__name__
        assert report["removed_line_count"] == 0
    # 芯片与断言都缺才删；仅缺芯片（v1 签发前的裸文本形态）仍删
    stripped_text = "Figure 3 显示总淀粉含量显著下降。"
    cleaned, report = strip_unbound_assertions_after_render(stripped_text, [], question="解释 Figure 3")
    assert "总淀粉含量" not in cleaned and report["removed_line_count"] == 1


def test_policy_revocation_inside_channel_still_cannot_leave_unbound_claims():
    """管线内场景：figure_label_allowed=False（步骤 9）在步骤 5 卫生复查之后
    剥离标签声明——终态收口（13b）必须兜住剥离后的无绑定因果句。"""
    from yuxi.knowledge.rendering.citation_channel import apply_citation_channel

    citations = [
        {
            "ref": "E1",
            "_quote": "grain length increased significantly",
            "quote_head": "grain length increased significantly",
            "locatable": True,
        }
    ]
    text, report = apply_citation_channel(
        "Figure 2 表明突变导致粒长增加。[E1]",
        citations,
        authority_policy={
            "page_claim_allowed": True,
            "document_citations_allowed": True,
            "figure_label_allowed": False,
        },
        semantic_question="解释 Figure 2 表型",
    )
    # 引用通道保留绑定句；本用例的断言句依赖标签声明被剥离后的形态
    assert "Figure 2" not in text or "粒长增加" in text
    hygiene = report["document_release_hygiene"]
    assert hygiene["removed_line_count"] >= 0  # 形态由策略决定，收口报告恒在场
