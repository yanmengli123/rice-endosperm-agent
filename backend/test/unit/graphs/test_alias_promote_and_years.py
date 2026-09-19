"""R4：ALIAS_PROMOTE 词典晋升、KB 作用域词典装载回滚、发表年提取。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuxi.knowledge.graphs.graph_review_service import GraphReviewService
from yuxi.knowledge.graphs.lexicon import (
    DICTIONARY_LEXICONS,
    lexicon_content_digest,
    merge_lexicon_terms,
    remove_lexicon_terms,
)
from yuxi.repositories.knowledge_graph_repository import _extract_publish_year


class _FakeRepo:
    def __init__(self):
        self.existing_decision = None
        self.saved_decisions: list[dict] = []
        self.added_aliases: list[tuple] = []

    async def get_decision(self, kb_id, target_kind, target_id):
        return self.existing_decision

    async def save_decision(self, **kwargs):
        self.saved_decisions.append(kwargs)
        return {"decision_id": "d1", "version": 1}

    async def add_entity_aliases(self, kb_id, entity_id, entity_name, aliases, *, alias_type="HUMAN"):
        self.added_aliases.append((kb_id, entity_id, aliases, alias_type))
        return len(aliases)


def _service(repo: _FakeRepo) -> GraphReviewService:
    service = GraphReviewService(
        review_repo=repo,
        graph_repo=SimpleNamespace(upsert_chunk_graph=AsyncMock()),
        chunk_repo=SimpleNamespace(get_by_chunk_id=AsyncMock()),
        kb_repo=SimpleNamespace(get_by_kb_id=AsyncMock(return_value=SimpleNamespace(kb_type="milvus"))),
    )
    service._tenant_id = AsyncMock(return_value=1)
    return service


@pytest.mark.asyncio
async def test_promote_alias_writes_decision_and_alias_table():
    repo = _FakeRepo()
    service = _service(repo)
    service._entity_by_name = AsyncMock(
        return_value={
            "entity_id": "e1",
            "normalized_name": "秋田小町",
            "label": "Cultivar",
            "name": "秋田小町",
            "attributes": [],
        }
    )
    result = await service.promote_alias(
        "kb1", surface="Akitakomachi", resolved_name="秋田小町", resolved_label="Cultivar", actor_uid="u1"
    )
    assert result["aliases_added"] == 1
    assert repo.added_aliases[0][3] == "DOCLEX_PROMOTED"
    assert repo.saved_decisions and repo.saved_decisions[0]["action"] == "ALIAS_PROMOTE"
    assert repo.saved_decisions[0]["target_kind"] == "ALIAS"


@pytest.mark.asyncio
async def test_promote_alias_is_idempotent_on_same_decision():
    repo = _FakeRepo()
    repo.existing_decision = {"action": "ALIAS_PROMOTE", "version": 1}
    service = _service(repo)
    result = await service.promote_alias(
        "kb1", surface="Akitakomachi", resolved_name="秋田小町", resolved_label="Cultivar", actor_uid="u2"
    )
    assert result["unchanged"] is True
    assert repo.added_aliases == []  # 幂等：不重复写别名表
    assert repo.saved_decisions == []


@pytest.mark.asyncio
async def test_promote_alias_requires_existing_entity():
    service = _service(_FakeRepo())
    service._entity_by_name = AsyncMock(return_value=None)
    with pytest.raises(ValueError, match="不存在"):
        await service.promote_alias(
            "kb1", surface="X", resolved_name="秋田小町", resolved_label="Cultivar", actor_uid="u1"
        )


# ── 词典装载回滚（KB 作用域）────────────────────────────────────


def test_merge_then_remove_restores_digest():
    digest_before = lexicon_content_digest()
    added = merge_lexicon_terms("Cultivar", {"__scope_test__": ("zz-scope-test",)})
    assert added == {"__scope_test__"}
    assert lexicon_content_digest() != digest_before
    remove_lexicon_terms("Cultivar", added)
    assert lexicon_content_digest() == digest_before
    assert "__scope_test__" not in DICTIONARY_LEXICONS["Cultivar"]


def test_remove_is_noop_for_unknown_keys():
    remove_lexicon_terms("Cultivar", {"__never_merged__"})
    remove_lexicon_terms("__missing_label__", {"__x__"})


# ── 发表年提取（R4b 时间维）─────────────────────────────────────


def test_extract_publish_year_supports_common_shapes():
    assert _extract_publish_year({"year": 2019}) == 2019
    assert _extract_publish_year({"published": "2022-05-01"}) == 2022
    assert _extract_publish_year({"publication_date": "Published online 2015 Apr"}) == 2015
    assert _extract_publish_year({"title": "no year here"}) is None
    assert _extract_publish_year(None) is None
    assert _extract_publish_year("not a dict") is None
