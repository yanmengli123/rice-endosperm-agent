"""批次 2：文献追问指代消解（确定性、fail-open）的单元测试。"""

from __future__ import annotations

import pytest

from yuxi.literature.followup_resolution import detect_followup_coreference, resolve_retrieval_query

pytestmark = [pytest.mark.unit]


class TestDetectFollowupCoreference:
    def test_pronoun_opening_detected(self) -> None:
        assert detect_followup_coreference("它的方法是什么？")
        assert detect_followup_coreference("那结果呢？")
        assert detect_followup_coreference("该文的结论如何？")

    def test_entity_question_not_flagged(self) -> None:
        assert not detect_followup_coreference("OsWRKY45 如何调控抗病性？")
        assert not detect_followup_coreference("What is the main finding?")

    def test_empty_returns_false(self) -> None:
        assert not detect_followup_coreference("")

    def test_english_pronoun_detected(self) -> None:
        assert detect_followup_coreference("what about its method?")
        assert not detect_followup_coreference("summarize the method")


class TestResolveRetrievalQuery:
    def test_coreference_folds_entity_anchor(self) -> None:
        resolved = resolve_retrieval_query("它的方法是什么？", ["OsWRKY45 如何调控抗病性？"])
        assert "OsWRKY45" in resolved
        assert "它的方法是什么？" in resolved

    def test_non_coreference_returns_original(self) -> None:
        original = "OsWRKY45 如何调控抗病性？"
        assert resolve_retrieval_query(original, ["前一个问题"]) == original

    def test_no_history_returns_original(self) -> None:
        original = "它的方法是什么？"
        assert resolve_retrieval_query(original, []) == original
        assert resolve_retrieval_query(original, None) == original

    def test_no_anchor_returns_original(self) -> None:
        # 历史里没有可抽的拉丁实体（纯中文/停用词）→ 原样
        original = "那结果呢？"
        assert resolve_retrieval_query(original, ["请解释一下"]) == original

    def test_doc_token_and_doi_priority(self) -> None:
        # Multiple selectors are ambiguous; the bound paper already supplies the document scope.
        assert (
            resolve_retrieval_query("它的方法是什么？", ["看 @paper1 和 10.1038/nature12345 那篇"])
            == "它的方法是什么？"
        )

    def test_anchor_cap_respected(self) -> None:
        # 每条历史最多 2 个锚点：历史1→[GeneA,GeneB]、历史2→[GeneD,GeneE]，
        # 总上限 4 个；GeneC（历史1第3）、GeneF（历史2第3）被每条上限截断。
        history = ["GeneA GeneB GeneC", "GeneD GeneE GeneF "]
        assert resolve_retrieval_query("它的机制？", history) == "它的机制？"

    def test_dedup_across_history(self) -> None:
        resolved = resolve_retrieval_query("它的方法？", ["GeneA 研究", "GeneA 与 GeneB"])
        # GeneA 只出现一次
        assert resolved.count("GeneA") == 1

    def test_empty_query_returns_original(self) -> None:
        assert resolve_retrieval_query("", ["历史"]) == ""
