"""闭集词表科研抽取链路：词法层 → 句窗 → 校验门 → 抽取器聚合 → 服务记录。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from yuxi.knowledge.graphs.extraction_gates import (
    G1_SCHEMA,
    G2_MISSING_ENDPOINT,
    G2_VERBATIM_QUOTE,
    G2_VERBATIM_SURFACE,
    G3_IDENTIFIER_PROVENANCE,
    G7_TRIGGER_UNVERIFIED,
    apply_gates,
)
from yuxi.knowledge.graphs.extraction_units import (
    ExtractionWindow,
    build_extraction_windows,
    strip_provenance_prefix,
)
from yuxi.knowledge.graphs.extractors import (
    GraphExtractorFactory,
    LLMScientificGraphExtractor,
    normalize_extraction_result,
)
from yuxi.knowledge.graphs.extractors.llm_scientific import G8_VERIFIER_REJECTED, SCIENTIFIC_RELATION_TYPES
from yuxi.knowledge.graphs.graph_utils import build_graph_payload
from yuxi.knowledge.graphs.lexicon import (
    SCIENTIFIC_ENTITY_TYPES,
    extract_identifiers,
    identifier_kind,
    pre_annotate,
)
from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService, _merge_extraction_stats
from yuxi.knowledge.graphs.predicate_triggers import check_predicate_trigger
from yuxi.repositories.knowledge_graph_repository import (
    EXTRACTED_ALIAS_TYPE,
    build_entity_mention_rows,
    build_extracted_alias_rows,
    build_triple_mention_rows,
    refresh_triple_support_statement,
)

# ── 词法层 ──────────────────────────────────────────────────────


def test_extract_identifiers_recognizes_registry_ids_and_citations():
    text = "LOC_Os03g64210 (Os03g0642100, OsNF-YB1) was cloned; doi:10.1111/tpj.12345 PMID: 12345678."

    kinds = {match.value: match.label for match in extract_identifiers(text)}

    assert kinds["LOC_Os03g64210"] == "Gene"
    assert kinds["Os03g0642100"] == "Gene"
    assert kinds["OsNF-YB1"] == "Gene"
    assert kinds["10.1111/tpj.12345"] is None  # 引文凭证不产实体
    assert kinds["12345678"] is None


def test_pre_annotate_hits_dictionary_terms_with_closed_labels():
    text = "ABA treatment of the endosperm at 10 DAP in Nipponbare was quantified by qRT-PCR."

    labels = {match.surface: match.label for match in pre_annotate(text)}

    assert labels["ABA"] == "Condition"
    assert labels["endosperm"] == "Tissue"
    assert labels["10 DAP"] == "DevelopmentStage"
    assert labels["Nipponbare"] == "Cultivar"
    assert labels["qRT-PCR"] == "Method"


def test_pre_annotate_handles_chinese_terms_and_prefers_longest_match():
    matches = pre_annotate("在灌浆期胚乳中脱落酸含量升高；grain filling stage samples")

    surfaces = {(match.surface, match.label) for match in matches}
    assert ("胚乳", "Tissue") in surfaces
    assert ("脱落酸", "Condition") in surfaces
    assert ("灌浆期", "DevelopmentStage") in surfaces
    # "grain filling stage" 覆盖 "grain"，短词被长词占位淘汰
    assert ("grain filling stage", "DevelopmentStage") in surfaces
    assert all(surface != "grain" for surface, _ in surfaces)


@pytest.mark.parametrize(
    ("surface", "expected"),
    [
        ("LOC_Os03g64210", "msu_id"),
        ("Os03g0642100", "rap_id"),
        ("OsNF-YB1", "os_symbol"),
        ("10.1111/x.1", "doi"),
        ("GIF1", None),
        ("12345678", None),
    ],
)
def test_identifier_kind(surface, expected):
    assert identifier_kind(surface) == expected


# ── 句窗 ────────────────────────────────────────────────────────


def test_strip_provenance_prefix_removes_marker_lines_only():
    text = (
        "【文献】10.1111/tpj.12345\n【章节】Results\nGIF1 encodes an invertase.\n"
        + "【页码】3\nIt is expressed in endosperm."
    )

    assert strip_provenance_prefix(text) == "GIF1 encodes an invertase.\nIt is expressed in endosperm."


def test_build_extraction_windows_uses_same_paragraph_context_only():
    text = (
        "# Results\n\n"
        "GIF1 encodes a cell-wall invertase. Overexpression of GIF1 increased grain weight. "
        "The gif1 mutant showed reduced grain filling.\n\n"
        "Fig. 2\n"
        "OsNF-YB1 is expressed in the aleurone layer."
    )

    windows = build_extraction_windows(text)

    assert [window.main_text for window in windows] == [
        "GIF1 encodes a cell-wall invertase.",
        "Overexpression of GIF1 increased grain weight.",
        "The gif1 mutant showed reduced grain filling.",
        "OsNF-YB1 is expressed in the aleurone layer.",
    ]
    assert windows[0].context_before == ""
    assert windows[0].context_after == "Overexpression of GIF1 increased grain weight."
    assert windows[1].context_before == "GIF1 encodes a cell-wall invertase."
    assert windows[1].context_after == "The gif1 mutant showed reduced grain filling."
    # 第二段的窗口不带第一段语境；题注行 "Fig. 2" 自成一句，过短不能作主句但可作语境
    assert windows[3].context_before == "Fig. 2"
    assert windows[3].paragraph_index == 1
    assert windows[0].full_text == "GIF1 encodes a cell-wall invertase. Overexpression of GIF1 increased grain weight."


# ── 校验门 ──────────────────────────────────────────────────────

_WINDOW = ExtractionWindow(
    index=0,
    main_text="Overexpression of Os03g0642100 (GIF1) increased grain weight in Nipponbare.",
    context_before="GIF1 encodes an invertase.",
)


def _gate(unit_result, preannotations=None):
    return apply_gates(
        unit_result,
        _WINDOW,
        preannotations if preannotations is not None else pre_annotate(_WINDOW.main_text),
        entity_types=SCIENTIFIC_ENTITY_TYPES,
        relation_types=SCIENTIFIC_RELATION_TYPES,
    )


def test_gates_accept_verbatim_entities_and_whitelisted_relation():
    outcome = _gate(
        {
            "entities": [
                {"surface": "Os03g0642100", "type": "Gene", "normalized_name": "GIF1"},
                {"surface": "grain weight", "type": "Phenotype"},
            ],
            "relations": [
                {
                    "subject": "Os03g0642100",
                    "predicate": "overexpression_effect",
                    "object": "grain weight",
                    "evidence_quote": "increased grain weight",
                    "confidence": 1.7,
                    "hedge": "yes",
                    "context": {"cultivar": "Nipponbare", "direction": "null", "tissue": None},
                }
            ],
        }
    )

    assert [entity["identifier_kind"] for entity in outcome.entities] == ["rap_id", None]
    assert outcome.entities[1]["normalized_name"] == "grain weight"
    relation = outcome.relations[0]
    assert relation["predicate"] == "OVEREXPRESSION_EFFECT"
    assert relation["confidence"] == 1.0
    assert relation["hedge"] is True
    assert relation["context"] == {"cultivar": "Nipponbare"}
    assert outcome.stats.hallucination_rate == 0.0
    assert outcome.stats.to_metadata()["identifier_violations"] == 0


def test_gates_reject_schema_violations_paraphrases_and_forged_identifiers():
    outcome = _gate(
        {
            "entities": [
                {"surface": "GIF1", "type": "Person"},  # G1 闭集
                {"surface": "grain yield", "type": "Phenotype"},  # G2 改写
                {"surface": "Os03g0642100", "type": "Gene"},
                {"surface": "grain weight", "type": "Phenotype"},
            ],
            "relations": [
                {"subject": "Os03g0642100", "predicate": "MAGIC", "object": "grain weight", "evidence_quote": "x"},
                {
                    "subject": "Os03g0642100",
                    "predicate": "REGULATES_PHENOTYPE",
                    "object": "missing",
                    "evidence_quote": "x",
                },
                {
                    "subject": "Os03g0642100",
                    "predicate": "REGULATES_PHENOTYPE",
                    "object": "grain weight",
                    "evidence_quote": "GIF1 boosts yield",
                },
                {
                    "subject": "Os03g0642100",
                    "predicate": "REGULATES_PHENOTYPE",
                    "object": "grain weight",
                    "evidence_quote": "increased grain weight",
                },
            ],
        },
        preannotations=[],  # 词法层未命中 → 标识符实体是 LLM 伪造
    )

    rejected = outcome.stats.rejected
    assert rejected[G1_SCHEMA] == 2
    assert rejected[G2_VERBATIM_SURFACE] == 1
    assert rejected[G3_IDENTIFIER_PROVENANCE] == 1
    # 标识符实体被 G3 拒绝后，引用它的关系全部因端点缺失落空
    assert rejected[G2_MISSING_ENDPOINT] == 3
    assert G2_VERBATIM_QUOTE not in rejected
    assert outcome.relations == []
    assert outcome.stats.hallucination_rate == 0.75


# ── 抽取器 ──────────────────────────────────────────────────────


class _FakeModel:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def call(self, prompt: str, stream: bool = False):
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.responses.pop(0))


_TEXT = (
    "【文献】10.1111/tpj.12345\n"
    "GIF1 (GRAIN INCOMPLETE FILLING 1) encodes a cell-wall invertase required for carbon partitioning "
    "during early grain filling. "
    "Overexpression of GIF1 increased grain weight in Nipponbare. "
    "The gif1 mutant showed reduced grain filling at 10 DAP."
)

_RESPONSE = json.dumps(
    {
        "units": [
            {
                "unit": 1,
                "entities": [
                    {"surface": "GIF1", "type": "Gene", "normalized_name": "GIF1"},
                    {"surface": "GRAIN INCOMPLETE FILLING 1", "type": "Gene", "normalized_name": "GIF1"},
                    {"surface": "carbon partitioning", "type": "Process", "normalized_name": "carbon partitioning"},
                ],
                "relations": [
                    {
                        "subject": "GIF1",
                        "predicate": "REQUIRED_FOR",
                        "object": "carbon partitioning",
                        "evidence_quote": "required for carbon partitioning",
                        "confidence": 0.9,
                        "hedge": False,
                        "context": {"stage": "early grain filling", "direction": None},
                    }
                ],
            },
            {
                "unit": 2,
                "entities": [
                    {"surface": "GIF1", "type": "Gene", "normalized_name": "GIF1"},
                    {"surface": "grain weight", "type": "Phenotype", "normalized_name": "grain weight"},
                    {"surface": "Nipponbare", "type": "Cultivar", "normalized_name": "Nipponbare"},
                    {"surface": "Os03g0642100", "type": "Gene", "normalized_name": "GIF1"},
                ],
                "relations": [
                    {
                        "subject": "GIF1",
                        "predicate": "OVEREXPRESSION_EFFECT",
                        "object": "grain weight",
                        "evidence_quote": "Overexpression of GIF1 increased grain weight",
                        "confidence": 1.4,
                        "hedge": False,
                        "context": {"cultivar": "Nipponbare"},
                    },
                    {"subject": "GIF1", "predicate": "MAGIC_RELATION", "object": "grain weight", "evidence_quote": "x"},
                ],
            },
            {
                "unit": 3,
                "entities": [
                    {"surface": "gif1 mutant", "type": "AlleleMutant", "normalized_name": "gif1"},
                    {"surface": "grain filling", "type": "Process", "normalized_name": "grain filling"},
                ],
                "relations": [
                    {
                        "subject": "gif1 mutant",
                        "predicate": "MUTANT_EFFECT",
                        "object": "grain filling",
                        "evidence_quote": "The gif1 mutant showed reduced grain filling",
                        "confidence": 0.8,
                        "hedge": True,
                        "context": {"direction": "inhibits", "stage": "10 DAP"},
                    }
                ],
            },
        ]
    }
)


@pytest.fixture
def fake_model(monkeypatch):
    model = _FakeModel([_RESPONSE])
    captured: dict = {}

    def _select_model(**kwargs):
        captured.update(kwargs)
        return model

    monkeypatch.setattr("yuxi.knowledge.graphs.extractors.llm_scientific.select_model", _select_model)
    return model, captured


@pytest.mark.asyncio
async def test_scientific_extractor_end_to_end(fake_model):
    model, captured = fake_model
    extractor = LLMScientificGraphExtractor({"model_spec": "test/model"})

    result = await extractor.extract(_TEXT, chunk_metadata={"chunk_id": "c1"})

    # 模型参数：温度锁 0；一次批量调用覆盖 3 个句窗；提示词含闭集与预标注
    assert captured["model_spec"] == "test/model"
    assert captured["model_params"] == {"temperature": 0}
    assert len(model.prompts) == 1
    assert "### 单元 3" in model.prompts[0]
    assert "Nipponbare(Cultivar)" in model.prompts[0]
    assert "OVEREXPRESSION_EFFECT" in model.prompts[0]
    assert "10.1111" not in model.prompts[0]  # provenance 前缀已剥离

    entities = {(entity["text"], entity["label"]): entity for entity in result["entities"]}
    assert set(entities) == {
        ("GIF1", "Gene"),
        ("carbon partitioning", "Process"),
        ("grain weight", "Phenotype"),
        ("Nipponbare", "Cultivar"),
        ("gif1", "AlleleMutant"),
        ("grain filling", "Process"),
    }
    assert entities[("GIF1", "Gene")]["aliases"] == ["GRAIN INCOMPLETE FILLING 1"]
    assert entities[("gif1", "AlleleMutant")]["aliases"] == ["gif1 mutant"]

    relations = {relation["label"]: relation for relation in result["relations"]}
    assert set(relations) == {"REQUIRED_FOR", "OVEREXPRESSION_EFFECT", "MUTANT_EFFECT"}
    assert relations["OVEREXPRESSION_EFFECT"]["confidence"] == 1.0
    assert relations["OVEREXPRESSION_EFFECT"]["text"] == "Overexpression of GIF1 increased grain weight"
    assert relations["OVEREXPRESSION_EFFECT"]["source"] is entities[("GIF1", "Gene")]
    assert relations["MUTANT_EFFECT"]["hedge"] is True
    assert relations["MUTANT_EFFECT"]["context"] == {"direction": "inhibits", "stage": "10 DAP"}
    assert relations["REQUIRED_FOR"]["context"] == {"stage": "early grain filling"}

    metadata = result["metadata"]
    assert metadata["extractor_type"] == "llm_scientific"
    assert metadata["windows"] == 3
    assert metadata["llm_calls"] == 1
    gates = metadata["gates"]
    assert gates["entity_candidates"] == 9
    assert gates["accepted_entities"] == 8
    assert gates["relation_candidates"] == 4
    assert gates["accepted_relations"] == 3
    assert gates["rejected"] == {G1_SCHEMA: 1, G2_VERBATIM_SURFACE: 1}
    assert gates["hallucination_rate"] == 0.0


@pytest.mark.asyncio
async def test_scientific_extractor_output_flows_into_service_records(fake_model):
    extractor = LLMScientificGraphExtractor({"model_spec": "test/model"})
    raw = await extractor.extract(_TEXT)

    normalized = normalize_extraction_result(raw, "llm_scientific")
    payload = build_graph_payload(normalized)
    records = MilvusGraphService()._build_entity_records("kb_test", payload["entities"])

    gif1 = next(record for record in records if record["name"] == "GIF1")
    assert gif1["aliases"] == ["GRAIN INCOMPLETE FILLING 1"]
    assert gif1["canonical_identity"] == "name:gif1"
    relation = next(item for item in normalized["relations"] if item["label"] == "MUTANT_EFFECT")
    assert relation["hedge"] is True
    assert relation["confidence"] == 0.8
    assert relation["context"] == {"direction": "inhibits", "stage": "10 DAP"}
    assert normalized["metadata"]["gates"]["accepted_relations"] == 3


@pytest.mark.asyncio
async def test_scientific_extractor_rejects_response_without_units(monkeypatch):
    monkeypatch.setattr(
        "yuxi.knowledge.graphs.extractors.llm_scientific.select_model",
        lambda **kwargs: _FakeModel(['{"relations": []}']),
    )
    extractor = LLMScientificGraphExtractor({"model_spec": "test/model"})

    with pytest.raises(ValueError, match="units"):
        await extractor.extract("GIF1 encodes a cell-wall invertase in rice endosperm.")


@pytest.mark.asyncio
async def test_scientific_extractor_skips_llm_when_no_window(monkeypatch):
    monkeypatch.setattr(
        "yuxi.knowledge.graphs.extractors.llm_scientific.select_model",
        lambda **kwargs: pytest.fail("no window → must not call the model"),
    )
    extractor = LLMScientificGraphExtractor({"model_spec": "test/model"})

    result = await extractor.extract("【章节】Results\n# Title\n")

    assert result == {"entities": [], "relations": [], "metadata": result["metadata"]}
    assert result["metadata"]["windows"] == 0
    assert result["metadata"]["gates"]["hallucination_rate"] is None


@pytest.mark.parametrize(
    "options",
    [
        {"model_spec": "m", "prompt": "custom"},
        {"model_spec": "m", "schema": "custom"},
        {"model_spec": "m", "batch_size": 0},
        {"model_spec": "m", "batch_size": 99},
        {"model_spec": "m", "context_sentences": 5},
        {"model_spec": "", "batch_size": 8},
    ],
)
def test_scientific_extractor_validate_options_rejects(options):
    with pytest.raises(ValueError):
        LLMScientificGraphExtractor(options).validate_options()


def test_factory_registers_scientific_extractor():
    assert GraphExtractorFactory.supported_types() == ["llm", "llm_scientific"]
    extractor = GraphExtractorFactory.create("llm_scientific", {"model_spec": "m", "batch_size": 4})
    assert extractor.batch_size == 4
    assert extractor.context_sentences == 1


# ── 服务 / 仓储 ─────────────────────────────────────────────────


def test_merge_extraction_stats_recomputes_rate_from_totals():
    total: dict = {}
    _merge_extraction_stats(
        total,
        {
            "windows": 3,
            "llm_calls": 1,
            "gates": {"relation_candidates": 4, "accepted_relations": 3, "rejected": {G2_VERBATIM_QUOTE: 1}},
        },
    )
    _merge_extraction_stats(
        total,
        {"windows": 1, "llm_calls": 1, "gates": {"relation_candidates": 4, "accepted_relations": 4, "rejected": {}}},
    )
    _merge_extraction_stats(total, {"extractor_type": "llm"})  # 通用抽取器无 gates → 跳过

    assert total["chunks_with_stats"] == 2
    assert total["windows"] == 4
    assert total["llm_calls"] == 2
    assert total["relation_candidates"] == 8
    assert total["hallucination_rate"] == 0.125
    assert total["identifier_violations"] == 0


def test_build_extracted_alias_rows_skips_self_and_duplicates():
    rows = build_extracted_alias_rows(
        "kb_test",
        [
            {
                "entity_id": "e1",
                "name": "GIF1",
                "aliases": ["gif1", "GRAIN INCOMPLETE FILLING 1", "GRAIN INCOMPLETE FILLING 1"],
            },
            {"entity_id": "e2", "name": "ABA", "aliases": []},
        ],
    )

    assert rows == [
        {
            "kb_id": "kb_test",
            "entity_id": "e1",
            "alias": "GRAIN INCOMPLETE FILLING 1",
            "normalized_alias": "grain incomplete filling 1",
            "alias_type": EXTRACTED_ALIAS_TYPE,
            "source": "chunk_extraction",
            "is_official": False,
        }
    ]


def test_refresh_triple_support_statement_counts_distinct_chunks_and_files():
    from sqlalchemy.dialects import postgresql

    sql = str(refresh_triple_support_statement(["t1"]).compile(dialect=postgresql.dialect()))

    assert "UPDATE knowledge_graph_triples" in sql
    assert sql.count("count(DISTINCT knowledge_graph_triple_mentions.chunk_id)") == 1
    assert sql.count("count(DISTINCT knowledge_graph_triple_mentions.file_id)") == 1
    assert "knowledge_graph_triple_mentions.triple_id = knowledge_graph_triples.triple_id" in sql


# ── G7 谓词触发词门 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("predicate", "sentence", "subject", "obj", "expected_verified"),
    [
        (
            "TRANSCRIPTIONAL_ACTIVATION",
            "OsNF-YB1 directly activates OsSWEET4 in the endosperm.",
            "OsNF-YB1",
            "OsSWEET4",
            True,
        ),
        # 方向颠倒：触发词在 object 之后、subject 之前且无被动标记
        (
            "TRANSCRIPTIONAL_ACTIVATION",
            "OsSWEET4 expression rose while OsNF-YB1 activates it.",
            "OsSWEET4",
            "OsNF-YB1",
            False,
        ),
        # 被动句：object 在前 + by + 触发词
        ("TRANSCRIPTIONAL_REPRESSION", "OsSWEET4 was repressed by OsNF-YB1.", "OsNF-YB1", "OsSWEET4", True),
        # 扰动类：触发词可在 subject 之前
        ("OVEREXPRESSION_EFFECT", "Overexpression of GIF1 increased grain weight.", "GIF1", "grain weight", True),
        # 对称谓词不判方向
        ("DIRECT_BINDING", "GIF1 physically interacts with OsNF-YB1.", "OsNF-YB1", "GIF1", True),
        # 中文主动句
        ("PROMOTES_PROCESS", "GIF1 促进胚乳淀粉合成。", "GIF1", "淀粉合成", True),
        # 无触发词：句子没说这个关系
        ("TRANSCRIPTIONAL_ACTIVATION", "GIF1 and grain weight were measured at 10 DAP.", "GIF1", "grain weight", False),
    ],
)
def test_check_predicate_trigger(predicate, sentence, subject, obj, expected_verified):
    assert check_predicate_trigger(predicate, sentence, subject, obj).verified is expected_verified


def test_gates_mark_triggers_and_attach_entity_mention_quotes():
    outcome = _gate(
        {
            "entities": [
                {"surface": "Os03g0642100", "type": "Gene", "normalized_name": "GIF1"},
                {"surface": "grain weight", "type": "Phenotype"},
            ],
            "relations": [
                {
                    "subject": "Os03g0642100",
                    "predicate": "OVEREXPRESSION_EFFECT",
                    "object": "grain weight",
                    "evidence_quote": "increased grain weight",
                },
                {
                    "subject": "Os03g0642100",
                    "predicate": "TRANSCRIPTIONAL_REPRESSION",
                    "object": "grain weight",
                    "evidence_quote": "grain weight",
                },
            ],
        }
    )

    assert all(entity["mention_quote"] == _WINDOW.main_text for entity in outcome.entities)
    by_predicate = {relation["predicate"]: relation for relation in outcome.relations}
    assert by_predicate["OVEREXPRESSION_EFFECT"]["trigger_verified"] is True
    assert by_predicate["OVEREXPRESSION_EFFECT"]["trigger_term"] == "overexpress"
    # 句中没有任何抑制义触发词：标记未验证但默认不拒绝
    assert by_predicate["TRANSCRIPTIONAL_REPRESSION"]["trigger_verified"] is False
    assert outcome.stats.trigger_verified_relations == 1
    assert outcome.stats.trigger_unverified_relations == 1
    assert G7_TRIGGER_UNVERIFIED not in outcome.stats.rejected


def test_gates_strict_triggers_reject_unverified_relations():
    outcome = apply_gates(
        {
            "entities": [
                {"surface": "Os03g0642100", "type": "Gene"},
                {"surface": "grain weight", "type": "Phenotype"},
            ],
            "relations": [
                {
                    "subject": "Os03g0642100",
                    "predicate": "TRANSCRIPTIONAL_REPRESSION",
                    "object": "grain weight",
                    "evidence_quote": "grain weight",
                }
            ],
        },
        _WINDOW,
        pre_annotate(_WINDOW.main_text),
        entity_types=SCIENTIFIC_ENTITY_TYPES,
        relation_types=SCIENTIFIC_RELATION_TYPES,
        strict_triggers=True,
    )

    assert outcome.relations == []
    assert outcome.stats.rejected == {G7_TRIGGER_UNVERIFIED: 1}
    assert outcome.stats.accepted_relations == 0


# ── 双模型复核 ──────────────────────────────────────────────────

_VERDICTS = json.dumps(
    {
        "verdicts": [
            {"id": 1, "supported": True, "span": "required for carbon partitioning"},
            {"id": 2, "supported": True, "span": "GIF1 boosts yield"},  # span 不在主句 → 拒绝
            {"id": 3, "supported": False, "span": None},
        ]
    }
)


@pytest.mark.asyncio
async def test_scientific_extractor_verifier_rejects_unsupported_relations(monkeypatch):
    models = {"test/model": _FakeModel([_RESPONSE]), "test/verifier": _FakeModel([_VERDICTS])}
    monkeypatch.setattr(
        "yuxi.knowledge.graphs.extractors.llm_scientific.select_model",
        lambda **kwargs: models[kwargs["model_spec"]],
    )
    extractor = LLMScientificGraphExtractor({"model_spec": "test/model", "verifier_model_spec": "test/verifier"})

    result = await extractor.extract(_TEXT)

    assert "候选关系：GIF1 --REQUIRED_FOR" in models["test/verifier"].prompts[0]
    assert [relation["label"] for relation in result["relations"]] == ["REQUIRED_FOR"]
    assert result["relations"][0]["verifier_confirmed"] is True
    assert result["relations"][0]["trigger_verified"] is True
    gates = result["metadata"]["gates"]
    assert gates["accepted_relations"] == 1
    assert gates["rejected"][G8_VERIFIER_REJECTED] == 2
    assert result["metadata"]["verifier_calls"] == 1
    assert result["metadata"]["verifier_model_spec"] == "test/verifier"
    # 实体不受复核影响，且每个实体都带本 chunk 的主句引文
    assert all(entity["mention_quote"] for entity in result["entities"])


@pytest.mark.asyncio
async def test_scientific_extractor_marks_triggers_without_verifier(fake_model):
    extractor = LLMScientificGraphExtractor({"model_spec": "test/model"})

    result = await extractor.extract(_TEXT)

    assert all(relation["trigger_verified"] for relation in result["relations"])
    assert all(relation["verifier_confirmed"] is None for relation in result["relations"])
    assert result["metadata"]["gates"]["trigger_verified_relations"] == 3
    assert result["metadata"]["verifier_model_spec"] is None


# ── 服务写入：引文偏移绑定与不变式断言 ──────────────────────────


def _neo4j_stub_service():
    from unittest.mock import MagicMock

    tx = MagicMock()
    session = MagicMock()
    session.__enter__.return_value = session
    session.execute_write.side_effect = lambda func: func(tx)
    driver = MagicMock()
    driver.session.return_value = session
    return MilvusGraphService(neo4j_connection=SimpleNamespace(driver=driver))


@pytest.mark.asyncio
async def test_write_chunk_graph_binds_quote_offsets_for_scientific_track(fake_model):
    extractor = LLMScientificGraphExtractor({"model_spec": "test/model"})
    normalized = normalize_extraction_result(await extractor.extract(_TEXT), "llm_scientific")
    chunk = SimpleNamespace(
        chunk_id="chunk_1", file_id="file_1", chunk_index=0, content=_TEXT, start_char_pos=0, end_char_pos=len(_TEXT)
    )

    entity_records, triple_records = _neo4j_stub_service().write_chunk_graph("kb_test", chunk, normalized)

    gif1 = next(record for record in entity_records if record["name"] == "GIF1")
    assert gif1["mention_quote"].startswith("GIF1 (GRAIN INCOMPLETE FILLING 1) encodes")
    assert _TEXT[gif1["mention_quote_start"] :].startswith(gif1["mention_quote"])
    overexpression = next(record for record in triple_records if record["relation_type"] == "OVEREXPRESSION_EFFECT")
    assert overexpression["text"] == "Overexpression of GIF1 increased grain weight"
    assert _TEXT[overexpression["quote_start_char"] :].startswith(overexpression["text"])
    assert overexpression["confidence"] == 1.0
    assert overexpression["trigger_verified"] is True
    assert overexpression["context"] == {"cultivar": "Nipponbare"}


def test_write_chunk_graph_rejects_scientific_entities_without_quote():
    normalized = normalize_extraction_result(
        {
            "entities": [{"text": "GIF1", "label": "Gene"}],
            "relations": [],
            "metadata": {"extractor_type": "llm_scientific"},
        },
        "llm_scientific",
    )
    chunk = SimpleNamespace(chunk_id="c", file_id="f", chunk_index=0, content="GIF1.", start_char_pos=0, end_char_pos=5)

    with pytest.raises(ValueError, match="缺少原文引文"):
        _neo4j_stub_service().write_chunk_graph("kb_test", chunk, normalized)


def test_mention_row_builders_emit_uniform_columns():
    entity_rows = build_entity_mention_rows(
        "kb", "f", "c", [{"entity_id": "e1", "mention_quote": "GIF1 encodes an invertase.", "mention_quote_start": 3}]
    )
    triple_rows = build_triple_mention_rows(
        "kb",
        "f",
        "c",
        [
            {
                "triple_id": "t1",
                "text": "quote",
                "extractor_type": "llm_scientific",
                "confidence": 0.9,
                "hedge": False,
                "context": {"stage": "10 DAP"},
                "trigger_verified": True,
                "trigger_term": "promot",
                "verifier_confirmed": None,
            },
            {"triple_id": "t2", "text": "任职于", "extractor_type": "llm"},
        ],
    )

    assert entity_rows == [
        {
            "entity_id": "e1",
            "kb_id": "kb",
            "file_id": "f",
            "chunk_id": "c",
            "text": "GIF1 encodes an invertase.",
            "quote_start_char": 3,
        }
    ]
    assert set(triple_rows[0]) == set(triple_rows[1])
    assert triple_rows[0]["context_json"] == {"stage": "10 DAP"}
    assert triple_rows[1]["confidence"] is None and triple_rows[1]["context_json"] is None
