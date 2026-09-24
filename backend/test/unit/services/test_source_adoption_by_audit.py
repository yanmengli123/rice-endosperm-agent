"""F1 adoption 以 MCPCallAudit 为权威的行为契约。

背景（golden 实测 run 09452049）：计划未要求 MCP 的轮次里 ricekb_sequence
成功调用，但旧逻辑 ``adopted = audit.id in matched_ids`` 依赖 plan 匹配 →
source_uses 全部 adopted=False → 事实账本不挂载 → 1830bp 序列零标记放行。
修复后：受信注册表、answer-eligible、非 discovery 的成功调用一律 adopted。
"""

from __future__ import annotations

from types import SimpleNamespace

from yuxi.knowledge.planning.turn_execution_plan import plan_turn
from yuxi.knowledge.rendering.source_output_guard import fact_catalog_summary
from yuxi.services.chat_service import _append_mcp_source_uses, _initial_source_manifest


def _audit(
    audit_id: int, *, tool: str = "ricekb_sequence", status: str = "success", provider_status: str | None = None
):
    provenance = {"provider_status": provider_status} if provider_status else {}
    return SimpleNamespace(
        id=audit_id,
        server_slug="ricekb",
        capability_name=tool,
        status=status,
        provenance=provenance,
        arguments_digest="sha256:args",
        result_digest="sha256:result",
    )


def _manifest():
    return _initial_source_manifest(plan_turn("Wx 的 CDS 序列给我", has_knowledge_scope=True))


def test_successful_registry_tool_adopted_without_plan_match():
    """计划未匹配（matched_ids 空）时，成功受信调用仍 adopted——账本必须挂上。"""
    manifest = _manifest()
    _append_mcp_source_uses(manifest, audits=[_audit(101)], matched_ids=set())
    assert len(manifest.source_uses) == 1
    assert manifest.source_uses[0].adopted is True
    assert manifest.source_uses[0].evidence_ids == ["mcp:101"]
    assert manifest.source_uses[0].execution_status == "SUCCESS"


def test_found_provider_status_does_not_replace_execution_success():
    manifest = _manifest()
    _append_mcp_source_uses(manifest, audits=[_audit(107, provider_status="FOUND")], matched_ids=set())
    source_use = manifest.source_uses[0]
    assert source_use.status == "SUCCESS"
    assert source_use.execution_status == "SUCCESS"
    assert source_use.provider_status == "FOUND"
    assert source_use.adopted is True


def test_found_fact_manifest_reaches_the_publish_catalog():
    audit = _audit(108, tool="ricekb_gene_profile")
    audit.provenance = {
        "fact_manifest": {
            "facts": [
                {"id": "f_0000000000000108", "path": "/status", "string_value": "FOUND"},
                {
                    "id": "f_0000000000000109",
                    "path": "/data/identity/canonical_rap_id",
                    "string_value": "Os06g0133000",
                },
            ]
        }
    }
    manifest = _manifest()
    _append_mcp_source_uses(manifest, audits=[audit], matched_ids={108})

    assert manifest.source_uses[0].adopted is True
    assert manifest.source_uses[0].provider_status == "FOUND"
    assert [fact["string_value"] for fact in fact_catalog_summary(manifest.source_uses)] == [
        "FOUND",
        "Os06g0133000",
    ]


def test_negative_provider_status_not_adopted():
    """科学负状态（NOT_FOUND 等）不算成功调用：不 adopted，账本不挂。"""
    manifest = _manifest()
    _append_mcp_source_uses(manifest, audits=[_audit(102, provider_status="NOT_FOUND")], matched_ids=set())
    assert manifest.source_uses[0].adopted is False


def test_negative_status_from_fact_manifest_not_adopted():
    """Legacy adapter 无 structured_content 时，/status 事实仍是负状态权威。"""
    audit = _audit(106)
    audit.provenance = {
        "fact_manifest": {
            "facts": [
                {
                    "id": "f_0000000000000106",
                    "path": "/status",
                    "string_value": "NOT_FOUND",
                }
            ]
        }
    }
    manifest = _manifest()
    _append_mcp_source_uses(manifest, audits=[audit], matched_ids={106})
    assert manifest.source_uses[0].adopted is False
    assert manifest.source_uses[0].status == "SUCCESS"
    assert manifest.source_uses[0].provider_status == "NOT_FOUND"


def test_failed_audit_not_adopted():
    manifest = _manifest()
    _append_mcp_source_uses(manifest, audits=[_audit(103, status="error")], matched_ids={103})
    assert manifest.source_uses[0].adopted is False


def test_discovery_tool_not_adopted_even_if_matched():
    manifest = _manifest()
    _append_mcp_source_uses(manifest, audits=[_audit(104, tool="ncbi_eutils_search_rest")], matched_ids={104})
    assert manifest.source_uses[0].source_class.value == "DISCOVERY"
    assert manifest.source_uses[0].adopted is False


def test_unregistered_tool_still_requires_plan_match():
    """非注册表工具（profile None）维持旧语义：只有 plan 匹配才 adopted。"""
    manifest = _manifest()
    _append_mcp_source_uses(manifest, audits=[_audit(105, tool="totally_custom_tool")], matched_ids=set())
    assert manifest.source_uses[0].adopted is False
