"""Contract Command Gate：所有修改型入口的统一契约校验。

调用顺序（设计第十四节）::

    PrincipalContext / Access Check
    → Contract Command Gate（本模块）
    → Media/Schema Validation
    → Domain Service

未知契约 fail closed；命令默认拒绝（只有 legacy 契约显式放开全部命令）。
路由层负责把 SourceContractError 映射为 415/422 响应。
"""

from __future__ import annotations

import os

from yuxi.knowledge.source_contracts.registry import (
    SourceContractError,
    UnknownSourceContractError,
    resolve_contract,
)
from yuxi.knowledge.source_contracts.specs import SourceContractSpec, contract_digest
from yuxi.utils import logger

# 迁移回填兜底：行上没有契约字段的旧库（正常应已被 0029 回填）
_FALLBACK_CONTRACT = ("legacy_generic", "0")


class ContractCommandForbidden(SourceContractError):
    """契约不接受该命令。"""

    http_status = 422
    error_code = "SOURCE_CONTRACT_VIOLATION"


class ContractDigestDriftError(SourceContractError):
    """KB 行上冻结的契约 digest 与当前代码不一致（strict 模式 fail closed）。"""

    http_status = 422
    error_code = "SOURCE_CONTRACT_DIGEST_DRIFT"


def _digest_enforcement_mode() -> str:
    """digest 漂移处置：warn（默认，仅告警）/ strict（fail closed）。

    strict 是灰度开关：必须先运行 digest 刷新迁移（把所有 KB 行的
    contract_digest 对齐到当前代码），再切换，否则存量行会全线被拒。
    """
    return os.getenv("YUXI_CONTRACT_DIGEST_ENFORCE", "warn").strip().lower()


class ContractMediaRejected(SourceContractError):
    """文件媒体类型不被契约接受。"""

    http_status = 415
    error_code = "SOURCE_CONTRACT_MEDIA_REJECTED"


async def load_kb_contract(kb_id: str) -> SourceContractSpec:
    """加载知识库行上冻结的契约；行缺失契约字段时回落 legacy_generic@0 并告警。"""
    from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository

    kb = await KnowledgeBaseRepository().get_by_kb_id(kb_id)
    if kb is None:
        raise UnknownSourceContractError(f"知识库 {kb_id} 不存在")
    contract_key = (kb.contract_key or "").strip()
    contract_version = (kb.contract_version or "").strip()
    if not contract_key:
        logger.warning(f"[ContractGate] KB {kb_id} 缺少契约字段，回落 {_FALLBACK_CONTRACT[0]}@0（应已被迁移回填）")
        contract_key, contract_version = _FALLBACK_CONTRACT
    spec = resolve_contract(contract_key, contract_version or None)
    stored_digest = (kb.contract_digest or "").strip()
    if stored_digest and stored_digest != contract_digest(spec):
        drift_message = (
            f"[ContractGate] KB {kb_id} 冻结契约 digest={stored_digest} 与当前代码 "
            f"{spec.contract_ref} digest={contract_digest(spec)} 不一致"
        )
        if _digest_enforcement_mode() == "strict":
            # 冻结契约的语义漂移必须 fail closed：先跑 digest 刷新迁移再开 strict
            raise ContractDigestDriftError(f"{drift_message}；strict 模式拒绝按漂移语义执行")
        logger.warning(f"{drift_message}，按当前代码语义执行")
    return spec


async def require_contract_command(kb_id: str, command: str) -> SourceContractSpec:
    """校验契约是否允许该命令；不允许则抛 ContractCommandForbidden。"""
    spec = await load_kb_contract(kb_id)
    if command in spec.allowed_commands:
        return spec
    raise ContractCommandForbidden(
        f"知识源契约 {spec.contract_ref} 不接受命令 {command}；"
        f"允许的命令：{', '.join(sorted(spec.allowed_commands)) or '（无）'}"
    )


def validate_contract_media(
    spec: SourceContractSpec,
    filename: str | None,
    content_type: str | None = None,
    *,
    role: str = "document",
) -> None:
    """按契约校验文件媒体类型；不接受则抛 ContractMediaRejected(415)。

    媒体规则为空的契约（legacy）不做限制。
    """
    if not spec.accepted_media:
        return
    rules = [rule for rule in spec.accepted_media if rule.role == role]
    if not rules:
        rules = list(spec.accepted_media)
    extension = os.path.splitext(str(filename or "").strip().lower())[1]
    normalized_type = str(content_type or "").split(";", 1)[0].strip().lower()
    for rule in rules:
        if rule.extensions and extension in rule.extensions:
            return
        if normalized_type and rule.content_types and normalized_type in rule.content_types:
            return
    allowed_ext = sorted({ext for rule in rules for ext in rule.extensions})
    raise ContractMediaRejected(
        f"知识源契约 {spec.contract_ref} 只接受 {'/'.join(allowed_ext) or '受限媒体'} 文件: "
        f"{filename or '(未提供文件名)'}"
    )


def classify_legacy_kb(
    *,
    format_template: str | None,
    pdf_evidence_pipeline: bool,
    has_documents: bool,
    has_llm_extraction: bool,
    kb_type: str | None = "milvus",
) -> tuple[str, str, str]:
    """存量库契约回填分类（迁移 0029 与审计报告共用）。

    Returns:
        (contract_key, contract_version, reason)

    规则（设计第十一节）：
    - pdf_literature 模板或显式 pdf_evidence_pipeline → pdf_evidence@1.0.0
    - 纯 graph_csv（无普通文档、无 LLM 抽图）→ managed_graph@1.0.0
    - graph_csv 混合内容 → legacy_mixed@0（管理员拆分后再升级）
    - 其余（含 csv_dataset 旧模板，P1 前无 CSV 数据产品可升）→ legacy_generic@0
    """
    template = str(format_template or "").strip()
    kb_type = str(kb_type or "milvus").strip().lower()

    if template == "pdf_literature" or bool(pdf_evidence_pipeline):
        return ("pdf_evidence", "1.0.0", "pdf_literature 模板或显式 pdf_evidence_pipeline")
    if template == "graph_csv":
        if not has_documents and not has_llm_extraction:
            return ("managed_graph", "1.0.0", "纯 graph_csv 模板，无普通文档与 LLM 抽图")
        return (
            "legacy_mixed",
            "0",
            f"graph_csv 模板下存在混合内容（documents={has_documents}, llm_extraction={has_llm_extraction}）",
        )
    if template == "csv_dataset":
        return ("legacy_generic", "0", "旧 csv_dataset 模板：待 CSV 数据产品上线后显式迁移")
    if kb_type not in {"milvus"}:
        return ("legacy_generic", "0", f"外部/只读适配器 kb_type={kb_type}：连接器契约规划中")
    return ("legacy_generic", "0", "未选择模板的旧入口创建")
