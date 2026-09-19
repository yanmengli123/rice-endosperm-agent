"""R2：词典内容 digest 进指纹 + 图谱构建 chunk 死信落库。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from yuxi.knowledge.graphs.extractors import LLMScientificGraphExtractor
from yuxi.knowledge.graphs.lexicon import DICTIONARY_LEXICONS, lexicon_content_digest, merge_lexicon_terms
from yuxi.knowledge.graphs.milvus_graph_service import (
    GRAPH_DEAD_ATTEMPT_THRESHOLD,
    GRAPH_INDEX_MAX_ATTEMPTS,
    GraphBuildIncompleteError,
    MilvusGraphService,
)


@pytest.fixture(autouse=True)
def _legacy_kb_contract(monkeypatch):
    """契约门禁桩：单测默认 legacy_generic（与 test_milvus_graph_build 同款）。"""

    class _LegacySpec(SimpleNamespace):
        contract_key = "legacy_generic"
        contract_version = "0"
        contract_ref = "legacy_generic@0"
        allowed_commands = ("llm_graph_build",)

    async def _fake_load(kb_id):
        return _LegacySpec()

    import yuxi.knowledge.source_contracts as _contracts

    monkeypatch.setattr(_contracts, "load_kb_contract", _fake_load)


# ── R2a 词典内容 digest ─────────────────────────────────────────


def test_lexicon_digest_stable_and_order_insensitive():
    assert lexicon_content_digest() == lexicon_content_digest()
    # 同组词条顺序换掉（重建 dict）digest 不变
    label_group = DICTIONARY_LEXICONS["Condition"]
    key = next(iter(label_group))
    original = label_group[key]
    try:
        label_group[key] = tuple(reversed(original))
        assert lexicon_content_digest() == lexicon_content_digest()
    finally:
        label_group[key] = original


def test_lexicon_digest_changes_on_merge_lexicon_terms():
    before = lexicon_content_digest()
    try:
        merge_lexicon_terms("Condition", {"__test_term__": ("zz-test-term",)})
        assert lexicon_content_digest() != before
    finally:
        DICTIONARY_LEXICONS["Condition"].pop("__test_term__", None)


def test_extractor_fingerprint_tracks_lexicon_digest():
    extractor = LLMScientificGraphExtractor({"model_spec": "m"})
    before = extractor.extraction_fingerprint()
    try:
        merge_lexicon_terms("Condition", {"__fp_term__": ("zz-fp-term",)})
        assert extractor.extraction_fingerprint() != before  # 运行时扩词 → 缓存失效
    finally:
        DICTIONARY_LEXICONS["Condition"].pop("__fp_term__", None)
    assert extractor.extraction_fingerprint() == before  # 移除后回到原指纹


# ── R2b chunk 死信落库 ──────────────────────────────────────────


class _DeadLetterChunkRepo:
    """带死信记账的假仓储：失败累计落库、死信排除出 pending。"""

    def __init__(self, fail_chunk_ids: set[str], count: int = 3):
        self.pending = {
            f"chunk_{index}": SimpleNamespace(
                id=index,
                chunk_id=f"chunk_{index}",
                file_id="file_1",
                chunk_index=index,
                content=f"content {index}",
                extraction_result=None,
            )
            for index in range(1, count + 1)
        }
        self.fail_chunk_ids = set(fail_chunk_ids)
        self.indexed: list[str] = []
        self.recorded: list[str] = []
        self.attempts: dict[str, int] = {}
        self.revived = 0

    async def count_graph_pending_by_kb_id(self, kb_id):
        return len(self.pending)

    async def count_graph_dead_by_kb_id(self, kb_id):
        return sum(1 for attempts in self.attempts.values() if attempts >= GRAPH_DEAD_ATTEMPT_THRESHOLD)

    async def list_graph_pending_by_kb_id(self, kb_id, limit, *, after_id=None):
        chunks = sorted(self.pending.values(), key=lambda chunk: chunk.id)
        if after_id is not None:
            chunks = [chunk for chunk in chunks if chunk.id > after_id]
        return chunks[:limit]

    async def record_graph_attempt(self, chunk_id, error, *, dead_threshold):
        self.recorded.append(chunk_id)
        self.attempts[chunk_id] = self.attempts.get(chunk_id, 0) + 1
        return self.attempts[chunk_id]

    async def revive_graph_chunks(self, kb_id):
        self.revived += 1
        revived = [chunk_id for chunk_id, attempts in self.attempts.items() if attempts >= GRAPH_DEAD_ATTEMPT_THRESHOLD]
        for chunk_id in revived:
            self.attempts[chunk_id] = 0
        return len(revived)

    async def mark_graph_indexed(self, chunk_id, ent_ids=None):
        self.pending.pop(chunk_id)
        self.indexed.append(chunk_id)


def _dead_letter_build_service(chunk_repo):
    kb = SimpleNamespace(
        kb_type="milvus",
        embedding_model_spec="siliconflow-cn:BAAI/bge-m3",
        additional_params={
            "graph_build_config": {
                "locked": True,
                "extractor_type": "llm",
                "extractor_options": {"model_spec": "minimax-cn:MiniMax-M3", "concurrency_count": 1},
            }
        },
    )
    service = MilvusGraphService(
        kb_repo=SimpleNamespace(get_by_kb_id=AsyncMock(return_value=kb)),
        chunk_repo=chunk_repo,
        graph_repo=SimpleNamespace(upsert_chunk_graph=AsyncMock()),
        review_repo=SimpleNamespace(list_decisions=AsyncMock(return_value=[])),
        graph_vector_store=SimpleNamespace(insert_missing_graph_records=AsyncMock(return_value=None)),
        doclex_service=SimpleNamespace(prepare_file=AsyncMock(return_value={"fingerprint": None, "entries": []})),
    )
    service._get_chunk_extraction_result = AsyncMock(return_value={"entities": [], "relations": [], "metadata": {}})

    def write_chunk_graph(kb_id, chunk, extraction):
        if chunk.chunk_id in chunk_repo.fail_chunk_ids:
            raise RuntimeError("embedding unavailable")
        return ([{"entity_id": chunk.chunk_id}], [])

    service.write_chunk_graph = MagicMock(side_effect=write_chunk_graph)
    return service


@pytest.mark.asyncio
async def test_failed_attempts_are_recorded_per_try():
    repo = _DeadLetterChunkRepo({"chunk_1"})
    service = _dead_letter_build_service(repo)
    with pytest.raises(GraphBuildIncompleteError):
        await service.build_pending_chunks("kb_test", batch_size=2)
    # 单 job 内重试 3 次，每次失败都落库一笔
    assert repo.recorded.count("chunk_1") == GRAPH_INDEX_MAX_ATTEMPTS
    assert repo.attempts["chunk_1"] == GRAPH_INDEX_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_build_result_carries_dead_count_and_revive_clears():
    repo = _DeadLetterChunkRepo({"chunk_1"})
    # 预置：chunk_1 已累计 4 次失败（前一个 job 遗留），本 job 再 3 次达到 7 ≥ 6 → 死信
    repo.attempts["chunk_1"] = GRAPH_DEAD_ATTEMPT_THRESHOLD - 2
    service = _dead_letter_build_service(repo)
    with pytest.raises(GraphBuildIncompleteError) as exc_info:
        await service.build_pending_chunks("kb_test", batch_size=2)
    result = exc_info.value.result
    assert result["success"] == 2
    assert repo.attempts["chunk_1"] >= GRAPH_DEAD_ATTEMPT_THRESHOLD
    assert result["dead"] == 1

    revived = await service.revive_dead_chunks("kb_test")
    assert revived == 1
    assert repo.attempts["chunk_1"] == 0
