"""Source Contract 注册中心与命令门禁的确定性单测。"""

from __future__ import annotations

import pytest

from yuxi.knowledge.source_contracts import (
    COMMAND_DATASET_IMPORT,
    COMMAND_DATASET_PREVIEW,
    COMMAND_DATASET_DELETE,
    COMMAND_DATASET_SAMPLE_QUESTIONS,
    COMMAND_DOCUMENT_UPLOAD,
    COMMAND_FETCH_URL,
    COMMAND_GRAPH_IMPORT_EXECUTE,
    COMMAND_LLM_GRAPH_BUILD,
    COMMAND_LLM_GRAPH_CONFIG,
    COMMAND_LLM_GRAPH_RESET,
    COMMAND_MINDMAP_GENERATE,
    ContractCommandForbidden,
    ContractMediaRejected,
    SourceContractError,
    UnknownSourceContractError,
    classify_legacy_kb,
    contract_digest,
    contract_registry_snapshot,
    resolve_contract,
    validate_contract_media,
)
from yuxi.knowledge.source_contracts.specs import (
    SourceContractDisplay,
    SourceContractSpec,
)


class TestFailClosed:
    def test_unknown_contract_key_rejected(self):
        with pytest.raises(UnknownSourceContractError):
            resolve_contract("no_such_contract")

    def test_unknown_version_rejected(self):
        with pytest.raises(UnknownSourceContractError):
            resolve_contract("pdf_evidence", "9.9.9")

    def test_empty_key_rejected(self):
        with pytest.raises(UnknownSourceContractError):
            resolve_contract("")
        with pytest.raises(UnknownSourceContractError):
            resolve_contract(None)

    def test_resolve_latest_without_version(self):
        spec = resolve_contract("pdf_evidence")
        assert spec.version == "1.0.0"

    def test_legacy_contract_resolvable(self):
        assert resolve_contract("legacy_generic", "0").contract_key == "legacy_generic"
        assert resolve_contract("legacy_mixed", "0").contract_key == "legacy_mixed"


class TestRegistry:
    def test_snapshot_excludes_hidden_by_default(self):
        snapshot = contract_registry_snapshot()
        keys = {item["contract_key"] for item in snapshot}
        assert {
            "pdf_evidence",
            "csv_record",
            "csv_qa",
            "managed_graph",
            "generic_document",
        } <= keys
        assert "legacy_generic" not in keys
        assert "glossary" in keys

    def test_snapshot_includes_hidden_when_requested(self):
        snapshot = contract_registry_snapshot(include_hidden=True)
        keys = {item["contract_key"] for item in snapshot}
        assert {"legacy_generic", "legacy_mixed"} <= keys

    def test_managed_graph_forbids_llm_and_documents(self):
        spec = resolve_contract("managed_graph")
        assert COMMAND_LLM_GRAPH_BUILD not in spec.allowed_commands
        assert COMMAND_LLM_GRAPH_CONFIG not in spec.allowed_commands
        assert COMMAND_LLM_GRAPH_RESET not in spec.allowed_commands
        assert COMMAND_MINDMAP_GENERATE not in spec.allowed_commands
        assert COMMAND_DOCUMENT_UPLOAD not in spec.allowed_commands
        assert COMMAND_GRAPH_IMPORT_EXECUTE in spec.allowed_commands

    def test_pdf_evidence_authority_policy(self):
        spec = resolve_contract("pdf_evidence")
        assert spec.authority_policy["canonical_store"] == "postgresql_parse_revisions"
        assert spec.authority_policy["model_summary"] == "non_authoritative_citation_only"
        assert spec.authority_policy["llm_graph"] == "navigation_projection_non_authoritative"
        assert spec.authority_policy["mindmap"] == "navigation_projection_non_authoritative"

    def test_csv_qa_requires_mapping_semantics(self):
        spec = resolve_contract("csv_qa")
        assert "上传" in spec.processing_policy["ingest"]
        assert "必须确认" in spec.processing_policy["ingest"]

    def test_glossary_is_closed_world_canonical_authority(self):
        spec = resolve_contract("glossary")
        assert spec.authority_policy["glossary_authority"] is True
        assert spec.authority_policy["coverage_semantics"] == "CLOSED_WORLD_ACTIVE_REVISION"
        assert spec.base_capabilities["glossary_lookup"] == "FULL"
        assert COMMAND_DATASET_IMPORT in spec.allowed_commands
        assert COMMAND_DATASET_DELETE in spec.allowed_commands
        assert COMMAND_DATASET_SAMPLE_QUESTIONS in spec.allowed_commands

    def test_generic_document_is_explicit_advanced_contract(self):
        spec = resolve_contract("generic_document")
        assert spec.display.entry_mode == "advanced"
        assert COMMAND_DOCUMENT_UPLOAD in spec.allowed_commands
        assert COMMAND_FETCH_URL in spec.allowed_commands
        assert COMMAND_LLM_GRAPH_BUILD in spec.allowed_commands
        assert COMMAND_GRAPH_IMPORT_EXECUTE not in spec.allowed_commands


class TestNavigationProductCommands:
    """LLM 图谱与思维导图是「派生导航产品」：csv/pdf 允许生成，managed_graph 保持禁止。

    回归背景：csv_record 库（稻胚乳缩写词典 kb_g7g7wr8dei）在 `/graph-build/index`
    与 `/mindmap/generate` 处 422 `SOURCE_CONTRACT_VIOLATION`——严格契约把导航产品
    命令误列入 `forbidden_commands`，禁令盖过了真实工作流（见 ADR-0001 四平面哲学）。
    """

    NAVIGATION_COMMANDS = (
        COMMAND_MINDMAP_GENERATE,
        COMMAND_LLM_GRAPH_BUILD,
        COMMAND_LLM_GRAPH_CONFIG,
        COMMAND_LLM_GRAPH_RESET,
    )

    def test_csv_record_allows_navigation_products(self):
        spec = resolve_contract("csv_record", "1.0.0")
        for command in self.NAVIGATION_COMMANDS:
            assert command in spec.allowed_commands
            assert command not in spec.forbidden_commands

    def test_csv_record_still_forbids_document_lifecycle_and_graph_import(self):
        spec = resolve_contract("csv_record", "1.0.0")
        assert COMMAND_DOCUMENT_UPLOAD not in spec.allowed_commands
        assert COMMAND_FETCH_URL not in spec.allowed_commands
        assert COMMAND_GRAPH_IMPORT_EXECUTE not in spec.allowed_commands
        assert COMMAND_DATASET_IMPORT in spec.allowed_commands
        assert COMMAND_DATASET_PREVIEW in spec.allowed_commands

    def test_csv_qa_inherits_csv_record_navigation_products(self):
        spec = resolve_contract("csv_qa", "1.0.0")
        for command in self.NAVIGATION_COMMANDS:
            assert command in spec.allowed_commands

    def test_pdf_evidence_allows_navigation_products(self):
        spec = resolve_contract("pdf_evidence", "1.0.0")
        for command in self.NAVIGATION_COMMANDS:
            assert command in spec.allowed_commands
            assert command not in spec.forbidden_commands
        assert COMMAND_DOCUMENT_UPLOAD in spec.allowed_commands
        assert COMMAND_DATASET_IMPORT not in spec.allowed_commands
        assert COMMAND_GRAPH_IMPORT_EXECUTE not in spec.allowed_commands

    def test_managed_graph_still_forbids_navigation_products(self):
        spec = resolve_contract("managed_graph", "1.0.0")
        for command in self.NAVIGATION_COMMANDS:
            assert command not in spec.allowed_commands
            assert command in spec.forbidden_commands
        assert spec.authority_policy["llm_extraction"] == "forbidden"

    def test_authority_policy_marks_navigation_products_non_authoritative(self):
        for key in ("csv_record", "csv_qa", "pdf_evidence", "generic_document"):
            policy = resolve_contract(key, "1.0.0").authority_policy
            assert policy["llm_graph"] == "navigation_projection_non_authoritative"
        for key in ("csv_record", "csv_qa", "pdf_evidence"):
            policy = resolve_contract(key, "1.0.0").authority_policy
            assert policy["mindmap"] == "navigation_projection_non_authoritative"

    def test_navigation_products_documented_in_processing_policy(self):
        for key in ("csv_record", "pdf_evidence"):
            policy = resolve_contract(key, "1.0.0").processing_policy
            assert "navigation_products" in policy


class TestDigest:
    def test_digest_stable(self):
        spec = resolve_contract("csv_record")
        assert contract_digest(spec) == contract_digest(resolve_contract("csv_record"))

    def test_digest_changes_with_semantics(self):
        base = resolve_contract("csv_record")
        changed = SourceContractSpec(
            contract_key=base.contract_key,
            version=base.version,
            product_category=base.product_category,
            display=base.display,
            allowed_commands=tuple(base.allowed_commands) + ("some_new_command",),
        )
        assert contract_digest(base) != contract_digest(changed)

    def test_digest_format(self):
        assert contract_digest(resolve_contract("managed_graph")).startswith("sha256:")


class TestMediaValidation:
    def test_pdf_contract_rejects_csv(self):
        spec = resolve_contract("pdf_evidence")
        with pytest.raises(ContractMediaRejected) as exc_info:
            validate_contract_media(spec, "dataset.csv")
        assert exc_info.value.http_status == 415

    def test_pdf_contract_accepts_pdf(self):
        spec = resolve_contract("pdf_evidence")
        validate_contract_media(spec, "paper.PDF")
        validate_contract_media(spec, "paper.pdf", "application/pdf")

    def test_csv_contract_rejects_pdf(self):
        spec = resolve_contract("csv_record")
        with pytest.raises(ContractMediaRejected):
            validate_contract_media(spec, "paper.pdf")

    def test_csv_contract_accepts_tsv(self):
        spec = resolve_contract("csv_qa")
        validate_contract_media(spec, "data.tsv")

    def test_legacy_unrestricted(self):
        spec = resolve_contract("legacy_generic")
        validate_contract_media(spec, "anything.bin")

    @pytest.mark.parametrize(
        "filename",
        ["notes.md", "report.docx", "slides.pptx", "table.xlsx", "scan.png", "paper.pdf"],
    )
    def test_generic_document_accepts_supported_documents(self, filename):
        validate_contract_media(resolve_contract("generic_document"), filename)

    def test_generic_document_rejects_unsupported_media(self):
        with pytest.raises(ContractMediaRejected):
            validate_contract_media(resolve_contract("generic_document"), "payload.exe")


class TestClassifyLegacyKb:
    def test_pdf_template(self):
        result = classify_legacy_kb(
            format_template="pdf_literature",
            pdf_evidence_pipeline=False,
            has_documents=True,
            has_llm_extraction=False,
        )
        assert result[:2] == ("pdf_evidence", "1.0.0")

    def test_explicit_pipeline_flag(self):
        result = classify_legacy_kb(
            format_template=None,
            pdf_evidence_pipeline=True,
            has_documents=True,
            has_llm_extraction=False,
        )
        assert result[0] == "pdf_evidence"

    def test_pure_graph(self):
        result = classify_legacy_kb(
            format_template="graph_csv",
            pdf_evidence_pipeline=False,
            has_documents=False,
            has_llm_extraction=False,
        )
        assert result[:2] == ("managed_graph", "1.0.0")

    def test_mixed_graph(self):
        result = classify_legacy_kb(
            format_template="graph_csv",
            pdf_evidence_pipeline=False,
            has_documents=True,
            has_llm_extraction=False,
        )
        assert result[0] == "legacy_mixed"
        result2 = classify_legacy_kb(
            format_template="graph_csv",
            pdf_evidence_pipeline=False,
            has_documents=False,
            has_llm_extraction=True,
        )
        assert result2[0] == "legacy_mixed"

    def test_old_csv_template_stays_legacy(self):
        result = classify_legacy_kb(
            format_template="csv_dataset",
            pdf_evidence_pipeline=False,
            has_documents=True,
            has_llm_extraction=False,
        )
        assert result[:2] == ("legacy_generic", "0")

    def test_unknown_template(self):
        result = classify_legacy_kb(
            format_template=None,
            pdf_evidence_pipeline=False,
            has_documents=True,
            has_llm_extraction=False,
        )
        assert result[:2] == ("legacy_generic", "0")


@pytest.mark.asyncio
async def test_command_gate_allows_and_forbids(monkeypatch):
    from yuxi.knowledge.source_contracts import gate

    spec = resolve_contract("managed_graph")

    async def fake_load(kb_id):
        return spec

    monkeypatch.setattr(gate, "load_kb_contract", fake_load)
    assert (await gate.require_contract_command("kb_1", COMMAND_GRAPH_IMPORT_EXECUTE)) is spec
    with pytest.raises(ContractCommandForbidden) as exc_info:
        await gate.require_contract_command("kb_1", COMMAND_DOCUMENT_UPLOAD)
    assert exc_info.value.error_code == "SOURCE_CONTRACT_VIOLATION"
    assert exc_info.value.http_status == 422


@pytest.mark.asyncio
async def test_command_gate_allows_csv_record_navigation_products(monkeypatch):
    """回归：csv_record 库（稻胚乳缩写词典）的图谱构建/思维导图生成不再 422。"""
    from yuxi.knowledge.source_contracts import gate

    spec = resolve_contract("csv_record", "1.0.0")

    async def fake_load(kb_id):
        return spec

    monkeypatch.setattr(gate, "load_kb_contract", fake_load)
    for command in (COMMAND_LLM_GRAPH_BUILD, COMMAND_MINDMAP_GENERATE, COMMAND_LLM_GRAPH_RESET):
        assert (await gate.require_contract_command("kb_g7g7wr8dei", command)) is spec
    # 数据集契约仍然禁止文档生命周期命令（权威纯度不回退）
    with pytest.raises(ContractCommandForbidden) as exc_info:
        await gate.require_contract_command("kb_g7g7wr8dei", COMMAND_DOCUMENT_UPLOAD)
    assert exc_info.value.error_code == "SOURCE_CONTRACT_VIOLATION"
    assert exc_info.value.http_status == 422


@pytest.mark.asyncio
async def test_command_gate_managed_graph_still_blocks_navigation_products(monkeypatch):
    """managed_graph 契约保持严格：图谱只应来自 Canonical 导入。"""
    from yuxi.knowledge.source_contracts import gate

    spec = resolve_contract("managed_graph", "1.0.0")

    async def fake_load(kb_id):
        return spec

    monkeypatch.setattr(gate, "load_kb_contract", fake_load)
    for command in (COMMAND_LLM_GRAPH_BUILD, COMMAND_MINDMAP_GENERATE):
        with pytest.raises(ContractCommandForbidden):
            await gate.require_contract_command("kb_managed", command)


@pytest.mark.asyncio
async def test_command_gate_legacy_allows_all(monkeypatch):
    from yuxi.knowledge.source_contracts import gate

    spec = resolve_contract("legacy_generic")

    async def fake_load(kb_id):
        return spec

    monkeypatch.setattr(gate, "load_kb_contract", fake_load)
    for command in (
        COMMAND_DOCUMENT_UPLOAD,
        COMMAND_LLM_GRAPH_BUILD,
        COMMAND_GRAPH_IMPORT_EXECUTE,
    ):
        await gate.require_contract_command("kb_legacy", command)


def test_error_hierarchy():
    assert issubclass(UnknownSourceContractError, SourceContractError)
    assert issubclass(ContractCommandForbidden, SourceContractError)
    assert issubclass(ContractMediaRejected, SourceContractError)


def test_display_spec_defaults():
    spec = SourceContractSpec(
        contract_key="x",
        version="1.0.0",
        product_category="authority_source",
        display=SourceContractDisplay(label="X"),
    )
    assert not spec.hidden
    assert spec.contract_ref == "x@1.0.0"
