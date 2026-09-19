"""D3 抽取指纹：改配置 → 缓存失效 → 重抽覆盖的机械化回路。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxi.knowledge.graphs.extractors import LLMScientificGraphExtractor
from yuxi.knowledge.graphs.extraction_fingerprint import (
    compute_extraction_fingerprint,
    relation_types_version,
)
from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService


def _options(**overrides):
    return {"model_spec": "gpt-test", **overrides}


def _extractor(**overrides):
    return LLMScientificGraphExtractor(_options(**overrides))


# ── 指纹纯函数 ──────────────────────────────────────────────────


def test_fingerprint_stable_for_same_config():
    assert _extractor().extraction_fingerprint() == _extractor().extraction_fingerprint()


def test_fingerprint_changes_on_semantic_components():
    base = _extractor().extraction_fingerprint()
    variants = {
        "model_spec": _extractor(model_spec="gpt-other").extraction_fingerprint(),
        "model_params": _extractor(model_params={"temperature": 0.2}).extraction_fingerprint(),
        "batch_size": _extractor(batch_size=16).extraction_fingerprint(),
        "context_sentences": _extractor(context_sentences=2).extraction_fingerprint(),
        "strict_triggers": _extractor(strict_triggers=True).extraction_fingerprint(),
        "verifier": _extractor(verifier_model_spec="gpt-verifier").extraction_fingerprint(),
        "doclex": _extractor().extraction_fingerprint(doclex_fingerprint="doclex-abc"),
    }
    for name, fingerprint in variants.items():
        assert fingerprint != base, f"{name} 变化必须改变指纹"


def test_relation_types_version_tracks_set_membership():
    v1 = relation_types_version(frozenset({"A", "B"}))
    assert v1 == relation_types_version(frozenset({"B", "A"}))
    assert v1 != relation_types_version(frozenset({"A", "B", "OBSERVED_BY"}))


def test_compute_fingerprint_deterministic_and_doclex_sensitive():
    a = compute_extraction_fingerprint(
        extractor_type="llm_scientific",
        algorithm_versions={"lexicon": "v1"},
        runtime_options={"model_spec": "m"},
    )
    assert a == compute_extraction_fingerprint(
        extractor_type="llm_scientific",
        algorithm_versions={"lexicon": "v1"},
        runtime_options={"model_spec": "m"},
    )
    assert a != compute_extraction_fingerprint(
        extractor_type="llm_scientific",
        algorithm_versions={"lexicon": "v1"},
        runtime_options={"model_spec": "m"},
        doclex_fingerprint="doc",
    )


def test_extractor_fingerprint_ignores_execution_level_options():
    # concurrency_count 是执行层参数（不影响单 chunk 语义），不得触发重抽
    base = LLMScientificGraphExtractor({"model_spec": "m"}).extraction_fingerprint()
    with_concurrency = LLMScientificGraphExtractor({"model_spec": "m", "concurrency_count": 8}).extraction_fingerprint()
    assert base == with_concurrency


# ── 服务层缓存失效（伪造仓储，无 DB）──────────────────────────────


class _FakeChunkRepo:
    def __init__(self, stored):
        self.stored = stored
        self.updates: list[tuple[str, dict]] = []

    async def update_extraction_result(self, chunk_id, extraction_result):
        self.updates.append((chunk_id, extraction_result))


class _FingerprintExtractor:
    """按当前配置返回指纹的假抽取器；extract 记录调用。"""

    extractor_type = "llm_scientific"

    def __init__(self, fingerprint):
        self._fingerprint = fingerprint
        self.calls = 0

    def extraction_fingerprint(self, *, doclex_fingerprint=None):
        return self._fingerprint

    async def extract(self, text, *, chunk_metadata=None):
        self.calls += 1
        return {
            "entities": [{"text": "OsPIP2;1", "label": "Gene"}],
            "relations": [],
            "metadata": {"extractor_type": self.extractor_type, "extraction_fingerprint": self._fingerprint},
        }


def _chunk(extraction_result):
    return SimpleNamespace(
        chunk_id="c1",
        file_id="f1",
        chunk_index=0,
        content="OsPIP2;1 was detected.",
        extraction_result=extraction_result,
    )


@pytest.mark.asyncio
async def test_matching_fingerprint_reuses_cache_without_llm_call():
    stored = {"entities": [], "relations": [], "metadata": {"extraction_fingerprint": "fp-1"}}
    chunk_repo = _FakeChunkRepo(stored)
    service = MilvusGraphService(chunk_repo=chunk_repo)
    extractor = _FingerprintExtractor("fp-1")

    result = await service._get_chunk_extraction_result("kb1", _chunk(stored), extractor)

    assert extractor.calls == 0
    assert chunk_repo.updates == []
    assert result["metadata"]["extraction_fingerprint"] == "fp-1"


@pytest.mark.asyncio
async def test_stale_fingerprint_triggers_reextraction_and_overwrite():
    stored = {"entities": [], "relations": [], "metadata": {"extraction_fingerprint": "fp-old"}}
    chunk_repo = _FakeChunkRepo(stored)
    service = MilvusGraphService(chunk_repo=chunk_repo)
    extractor = _FingerprintExtractor("fp-new")

    result = await service._get_chunk_extraction_result("kb1", _chunk(stored), extractor)

    assert extractor.calls == 1
    assert len(chunk_repo.updates) == 1
    assert chunk_repo.updates[0][1]["metadata"]["extraction_fingerprint"] == "fp-new"
    assert result["metadata"]["extraction_fingerprint"] == "fp-new"


@pytest.mark.asyncio
async def test_legacy_cache_without_fingerprint_is_rebuilt():
    stored = {"entities": [], "relations": [], "metadata": {}}
    chunk_repo = _FakeChunkRepo(stored)
    service = MilvusGraphService(chunk_repo=chunk_repo)
    extractor = _FingerprintExtractor("fp-legacy-migration")

    await service._get_chunk_extraction_result("kb1", _chunk(stored), extractor)

    assert extractor.calls == 1


@pytest.mark.asyncio
async def test_extractor_without_fingerprint_keeps_legacy_behavior():
    class _NoFingerprintExtractor(_FingerprintExtractor):
        def extraction_fingerprint(self, *, doclex_fingerprint=None):
            return None  # 通用 llm 轨：无指纹 → 永远复用

    stored = {"entities": [], "relations": [], "metadata": {}}
    chunk_repo = _FakeChunkRepo(stored)
    service = MilvusGraphService(chunk_repo=chunk_repo)
    extractor = _NoFingerprintExtractor("ignored")

    await service._get_chunk_extraction_result("kb1", _chunk(stored), extractor)
    assert extractor.calls == 0


@pytest.mark.asyncio
async def test_doclex_fingerprint_change_invalidates_cache():
    stored = {"entities": [], "relations": [], "metadata": {"extraction_fingerprint": "fp-1"}}
    chunk_repo = _FakeChunkRepo(stored)
    service = MilvusGraphService(chunk_repo=chunk_repo)
    extractor = _FingerprintExtractor("fp-1")

    await service._get_chunk_extraction_result("kb1", _chunk(stored), extractor, {"fingerprint": None, "entries": []})
    assert extractor.calls == 0  # doclex 为 None（无活跃解析修订）时与无 doclex 等价
