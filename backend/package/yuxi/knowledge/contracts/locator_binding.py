"""VerifiedLocatorBinding：唯一定位权威对象（Multimodal Locator Authority）。

设计基线（2026-09-13 多模态科研定位权威层）：原句 / 图注 / 图片只是不同的
定位入口，最终必须经同一裁决产生同一个 :class:`VerifiedLocatorBinding`，再由
该绑定同时驱动冻结证据契约、状态投影、引用渲染与 PDF 查看器——Renderer 不再
重新读 Citation dict，状态模块不再重新推断页码，查看器不再重新找 anchor。

硬不变量（出口门禁，违反即 ``ANSWER_VALIDATION_FAILED``，页码不得上屏）：

- ``status == VERIFIED ⇒ physical_evidence_id 非空``；
- ``status == VERIFIED ⇒ physical_evidence_id ∈ 冻结证据契约 contract["evidence"]``；
- 模型（文本或视觉）永远不能决定文献、页码、anchor 或 bbox——绑定只由
  确定性裁决产生；视觉观察契约（VisualObservationEnvelope）物理上没有
  page_number 字段。
"""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

LOCATOR_AUTHORITY_VERSION = "locator_authority_v1"

BINDING_VERIFIED = "VERIFIED"
BINDING_MULTIPLE_MATCHES = "MULTIPLE_MATCHES"
BINDING_NOT_FOUND = "NOT_FOUND"
BINDING_NOT_APPLICABLE = "NOT_APPLICABLE"

# 定位入口（locator_kind）：不同入口，同一裁决对象
LOCATOR_KIND_QUOTE = "QUOTE_LOCATOR"
LOCATOR_KIND_PAGE = "PAGE_LOCATOR"
LOCATOR_KIND_FIGURE_CAPTION = "FIGURE_CAPTION"
LOCATOR_KIND_FIGURE_IMAGE = "FIGURE_IMAGE"

# 出口门禁违例码
VIOLATION_NO_PHYSICAL_EVIDENCE = "VERIFIED_WITHOUT_PHYSICAL_EVIDENCE"
VIOLATION_EVIDENCE_NOT_FROZEN = "VERIFIED_EVIDENCE_NOT_FROZEN"


class VerifiedLocatorBinding(BaseModel):
    """一次定位裁决的完整结论：物理血统 + 页码 + 验证元数据。

    内部权威对象：页码/锚点/血统字段只由确定性裁决写入，永不从模型文本
    重建。渲染器、状态投影与查看器统一消费本对象（或其持久化投影）。
    """

    model_config = ConfigDict(extra="forbid")

    binding_id: str
    status: str
    locator_kind: str = LOCATOR_KIND_QUOTE
    match_tier: str | None = None
    physical_evidence_id: str | None = None
    span_evidence_id: str | None = None
    span_id: str | None = None
    anchor_id: str | None = None
    file_id: str | None = None
    filename: str | None = None
    source_sha256: str | None = None
    parse_revision_id: str | None = None
    index_revision_id: str | None = None
    kb_id: str | None = None
    partition: str | None = None
    page_number: int | None = None
    hard_constraints_passed: list[str] = Field(default_factory=list)
    physical_unique: bool = False
    quote_head: str | None = None
    verification: dict[str, Any] = Field(default_factory=dict)
    backlinks: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.status == BINDING_VERIFIED


def make_binding_id(*, retrieval_id: str, evidence_id: str | None, anchor_id: str | None) -> str:
    digest = hashlib.sha256(f"vlb|{retrieval_id}|{evidence_id or ''}|{anchor_id or ''}".encode()).hexdigest()[:20]
    return f"vlb_{digest}"


def binding_from_locator_resolution(
    resolution: dict[str, Any],
    *,
    retrieval_id: str,
    locator_kind: str = LOCATOR_KIND_QUOTE,
    hard_constraints: list[str] | None = None,
) -> VerifiedLocatorBinding:
    """把 locator_resolution（审计 dict）升格为强类型绑定对象。"""
    resolution = dict(resolution or {})
    status = str(resolution.get("status") or BINDING_NOT_APPLICABLE)
    page = resolution.get("page")
    return VerifiedLocatorBinding(
        binding_id=make_binding_id(
            retrieval_id=retrieval_id,
            evidence_id=resolution.get("evidence_id"),
            anchor_id=resolution.get("anchor_id"),
        ),
        status=status,
        locator_kind=locator_kind,
        match_tier=resolution.get("match_tier"),
        physical_evidence_id=resolution.get("evidence_id"),
        span_evidence_id=resolution.get("span_evidence_id"),
        span_id=resolution.get("span_id"),
        anchor_id=resolution.get("anchor_id"),
        file_id=resolution.get("file_id"),
        filename=resolution.get("filename"),
        source_sha256=resolution.get("source_sha256"),
        parse_revision_id=resolution.get("parse_revision_id"),
        index_revision_id=resolution.get("index_revision_id"),
        kb_id=resolution.get("kb_id"),
        partition=resolution.get("zone"),
        page_number=int(page) if isinstance(page, int) and int(page) >= 1 else None,
        hard_constraints_passed=list(hard_constraints or []),
        physical_unique=status == BINDING_VERIFIED,
        quote_head=resolution.get("quote_head"),
        verification={
            key: value
            for key, value in resolution.items()
            if key
            in {
                "locator_version",
                "reason",
                "match_count",
                "partition_intent",
                "citation_ref",
                "retrieval_evidence_id",
            }
        },
        backlinks=list(resolution.get("backlinks") or []),
    )


def verify_freeze_invariant(binding: VerifiedLocatorBinding, contract: dict[str, Any]) -> list[str]:
    """出口不变量：VERIFIED ⇒ 物理证据已冻结进证据契约。返回违例码列表。"""
    if not binding.verified:
        return []
    if not binding.physical_evidence_id:
        return [VIOLATION_NO_PHYSICAL_EVIDENCE]
    frozen_ids = {
        str(row.get("evidence_id"))
        for row in contract.get("evidence") or []
        if isinstance(row, dict) and row.get("evidence_id")
    }
    if str(binding.physical_evidence_id) not in frozen_ids:
        return [VIOLATION_EVIDENCE_NOT_FROZEN]
    return []


def enforce_locator_authority(
    contract: dict[str, Any],
    *,
    retrieval_id: str,
    locator_kind: str = LOCATOR_KIND_QUOTE,
    hard_constraints: list[str] | None = None,
) -> VerifiedLocatorBinding:
    """Locator Authority 出口门禁（编排器在 locator_resolution 产生后立即调用）。

    通过 → 绑定写回 ``locator_resolution["binding"]``（随审计行持久化，
    状态投影与渲染器随后只消费该对象）；违例 → contract 降级为
    ``ANSWER_VALIDATION_FAILED``，定位结果改为失败关闭（不展示任何页码），
    原始结论保留在 ``_authority_gate_audit`` 供审计，绝不上屏。
    """
    resolution = dict(contract.get("locator_resolution") or {})
    binding = binding_from_locator_resolution(
        resolution,
        retrieval_id=retrieval_id,
        locator_kind=locator_kind,
        hard_constraints=hard_constraints,
    )
    violations = verify_freeze_invariant(binding, contract)
    if not violations:
        resolution["binding"] = binding.model_dump(mode="json")
        contract["locator_resolution"] = resolution
        return binding
    contract["locator_resolution"] = {
        "status": BINDING_NOT_FOUND,
        "locator_version": LOCATOR_AUTHORITY_VERSION,
        "reason": "answer_validation_failed_locator_evidence_not_frozen",
        "binding": binding.model_dump(mode="json"),
        "_authority_gate_audit": {"violations": violations, "original_status": binding.status},
    }
    contract["status"] = "DEGRADED"
    contract["error_code"] = "ANSWER_VALIDATION_FAILED"
    contract["warnings"] = [
        *(contract.get("warnings") or []),
        "定位结论未通过权威门禁（" + "、".join(violations) + "）；系统不展示任何页码。",
    ]
    return binding


__all__ = [
    "BINDING_MULTIPLE_MATCHES",
    "BINDING_NOT_APPLICABLE",
    "BINDING_NOT_FOUND",
    "BINDING_VERIFIED",
    "LOCATOR_AUTHORITY_VERSION",
    "LOCATOR_KIND_FIGURE_CAPTION",
    "LOCATOR_KIND_FIGURE_IMAGE",
    "LOCATOR_KIND_PAGE",
    "LOCATOR_KIND_QUOTE",
    "VerifiedLocatorBinding",
    "binding_from_locator_resolution",
    "enforce_locator_authority",
    "make_binding_id",
    "verify_freeze_invariant",
]
