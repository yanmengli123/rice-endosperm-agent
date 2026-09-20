"""知识库发布治理服务（P2）：不可变 Release Manifest + 原子发布指针。

设计第十一/十二节落地::

    - 多来源知识库通过不可变发布清单发布：每文件引用其活跃解析修订与索引修订，
      外加检索策略修订；发布/回滚只原子切换 KnowledgeBase.active_release_id。
    - 生命周期三维度：治理状态（KB 行上 DRAFT/PUBLISHED/ARCHIVED）、
      运行健康（由来源聚合计算，不落列）、任务状态（各修订/导入记录自有状态）。
    - Capability 三层：Base（契约静态）∩ Observed（文件级实际处理质量）
      ∩ Permission/Policy → Effective。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeRelease,
    KnowledgeRetrievalPolicyRevision,
)
from yuxi.utils import logger
from yuxi.utils.datetime_utils import utc_now

if TYPE_CHECKING:
    from yuxi.storage.postgres.models_knowledge import KnowledgeGraphReleaseDecision

CAPABILITY_LEVEL_RANK = {"FULL": 3, "PARTIAL": 2, "UNSUPPORTED": 1, "REJECTED": 0, None: -1}


class ReleaseStateError(ValueError):
    """发布/回滚状态冲突 → HTTP 409 INVALID_STATE_TRANSITION。"""


def _manifest_hash(manifest: dict) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


async def _build_graph_release_section(session, kb) -> tuple[dict[str, Any], list[KnowledgeGraphReleaseDecision]]:
    """冻结图谱治理段（P2）：抽取配置指纹、决策水位与逐条决策清单行。

    manifest.graph 只存水位与哈希（体积可控）；完整决策清单落
    knowledge_graph_release_decisions（append-only），pinned_quote 只存
    sha 前 16 位，原文经 kb_id+target_id+pinned_chunk_id 可回溯。
    """
    from yuxi.knowledge.graphs.milvus_graph_service import GRAPH_CONFIG_KEY
    from yuxi.repositories.knowledge_graph_review_repository import KnowledgeGraphReviewRepository
    from yuxi.storage.postgres.models_knowledge import (
        KnowledgeGraphReleaseDecision,
        KnowledgeGraphReviewDecision,
    )

    review_repo = KnowledgeGraphReviewRepository()
    from yuxi.knowledge.graphs.graph_governance_service import GraphGovernanceService

    try:
        settings = await GraphGovernanceService(kb_repo=None, review_repo=review_repo).get_settings(kb.kb_id)
        review_policy = settings.get("review_policy")
    except ValueError:
        review_policy = None

    decision_rows = (
        (
            await session.execute(
                select(KnowledgeGraphReviewDecision).where(KnowledgeGraphReviewDecision.kb_id == kb.kb_id)
            )
        )
        .scalars()
        .all()
    )
    counts_by_kind_action: dict[str, int] = {}
    max_version = 0
    manifest_lines: list[str] = []
    release_decision_rows: list[KnowledgeGraphReleaseDecision] = []
    for row in sorted(decision_rows, key=lambda r: (r.target_kind, r.target_id)):
        key = f"{row.target_kind}:{row.action}"
        counts_by_kind_action[key] = counts_by_kind_action.get(key, 0) + 1
        max_version = max(max_version, int(row.version or 0))
        manifest_lines.append(f"{row.target_kind}|{row.target_id}|{row.action}|{row.version}")
        release_decision_rows.append(
            KnowledgeGraphReleaseDecision(
                release_id="",  # 由调用方回填 release_id 后统一 flush
                kb_id=kb.kb_id,
                tenant_id=kb.tenant_id,
                target_kind=row.target_kind,
                target_id=row.target_id,
                action=row.action,
                version=int(row.version or 0),
                actor_uid=row.actor_uid,
                pinned_chunk_id=row.pinned_chunk_id,
                pinned_quote_sha=(
                    hashlib.sha256((row.pinned_quote or "").encode("utf-8")).hexdigest()[:16]
                    if row.pinned_quote
                    else None
                ),
            )
        )
    decisions_manifest_sha = (
        hashlib.sha256("\n".join(manifest_lines).encode("utf-8")).hexdigest() if manifest_lines else None
    )

    config = ((kb.additional_params or {}) if hasattr(kb, "additional_params") else {}).get(GRAPH_CONFIG_KEY) or {}
    integrity = await review_repo.integrity_counts(kb.kb_id)
    integrity.pop("rejected_triple_ids", None)
    graph_section = {
        "review_policy": review_policy,
        "extraction": {
            "extractor_type": config.get("extractor_type"),
            "model_spec": (config.get("extractor_options") or {}).get("model_spec"),
            "config_created_at": config.get("created_at"),
            "config_created_by": config.get("created_by"),
        },
        "decisions_watermark": {
            "total": len(decision_rows),
            "counts_by_kind_action": counts_by_kind_action,
            "max_version": max_version,
            "manifest_sha256": decisions_manifest_sha,
        },
        "integrity_snapshot": integrity,
        "frozen_at": utc_now().isoformat(),
    }
    return graph_section, release_decision_rows


async def _ensure_graph_policy_revision(session, kb, policy_json: dict, operator_id: str | None) -> str | None:
    """为图谱策略创建/复用检索策略修订（幂等：同 hash 复用最新修订）。"""
    policy_hash = _manifest_hash(policy_json)
    existing = (
        (
            await session.execute(
                select(KnowledgeRetrievalPolicyRevision)
                .where(
                    KnowledgeRetrievalPolicyRevision.kb_id == kb.kb_id,
                    KnowledgeRetrievalPolicyRevision.policy_hash == policy_hash,
                )
                .order_by(KnowledgeRetrievalPolicyRevision.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing.revision_id
    revision_id = f"rp_{uuid.uuid4().hex[:24]}"
    session.add(
        KnowledgeRetrievalPolicyRevision(
            revision_id=revision_id,
            kb_id=kb.kb_id,
            tenant_id=kb.tenant_id,
            policy_json=policy_json,
            policy_hash=policy_hash,
            created_by=operator_id,
        )
    )
    return revision_id


async def build_release(kb_id: str, operator_id: str | None) -> dict:
    """从各文件的活跃修订构建 STAGED 发布清单（不可变）。"""
    from yuxi.knowledge.source_contracts import SourceContractError, load_kb_contract
    from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository

    kb = await KnowledgeBaseRepository().get_by_kb_id(kb_id)
    if kb is None:
        raise ValueError(f"知识库 {kb_id} 不存在")
    try:
        spec = await load_kb_contract(kb_id)
    except SourceContractError as exc:
        raise ValueError(f"[{exc.error_code}] {exc}") from exc

    async with pg_manager.get_async_session_context() as session:
        file_rows = (
            (
                await session.execute(
                    select(KnowledgeFile)
                    .where(KnowledgeFile.kb_id == kb_id, KnowledgeFile.is_folder.is_(False))
                    .order_by(KnowledgeFile.file_id)
                )
            )
            .scalars()
            .all()
        )
        sources = []
        for record in file_rows:
            status = (record.status or "").lower()
            if status not in {"parsed", "indexed", "done", "error_indexing"}:
                continue
            sources.append(
                {
                    "source_id": record.file_id,
                    "filename": record.filename,
                    "content_hash": record.content_hash,
                    "canonical_revision_id": record.active_parse_revision_id,
                    "projection_revision_id": record.active_index_revision_id,
                    "evidence_status": record.evidence_status,
                }
            )
        if not sources:
            raise ReleaseStateError("知识库没有可发布的已处理来源（需要至少一个已解析/已入库文件）")

        graph_section: dict[str, Any] | None = None
        graph_decision_rows: list = []
        if (kb.kb_type or "").lower() == "milvus":
            graph_section, graph_decision_rows = await _build_graph_release_section(session, kb)

        manifest = {
            "kb_id": kb_id,
            "contract": spec.contract_ref,
            "sources": sources,
            "built_at": utc_now().isoformat(),
        }
        retrieval_policy_revision_id = None
        if graph_section is not None:
            manifest["graph"] = graph_section
            retrieval_policy_revision_id = await _ensure_graph_policy_revision(
                session,
                kb,
                {
                    "graph_review_policy": graph_section.get("review_policy"),
                    "decisions_manifest_sha256": graph_section.get("decisions_watermark", {}).get("manifest_sha256"),
                },
                operator_id,
            )
        release = KnowledgeRelease(
            release_id=f"rel_{uuid.uuid4().hex[:24]}",
            kb_id=kb_id,
            tenant_id=kb.tenant_id,
            contract_ref=spec.contract_ref,
            retrieval_policy_revision_id=retrieval_policy_revision_id,
            manifest_json=manifest,
            manifest_hash=_manifest_hash(manifest),
            status="STAGED",
            source_count=len(sources),
            created_by=operator_id,
        )
        session.add(release)
        await session.flush()  # release_id 已定，回填决策清单行
        for decision_row in graph_decision_rows:
            decision_row.release_id = release.release_id
            session.add(decision_row)
        return {
            "release_id": release.release_id,
            "status": release.status,
            "contract_ref": release.contract_ref,
            "source_count": release.source_count,
            "manifest_hash": release.manifest_hash,
            "graph_decisions_frozen": len(graph_decision_rows),
            "manifest": manifest,
        }


async def publish_release(kb_id: str, release_id: str, operator_id: str | None) -> dict:
    """原子发布：置前一个 ACTIVE 为 SUPERSEDED，切换发布指针，治理状态置 PUBLISHED。"""
    async with pg_manager.get_async_session_context() as session:
        kb = (await session.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))).scalar_one_or_none()
        if kb is None:
            raise ValueError(f"知识库 {kb_id} 不存在")
        release = (
            await session.execute(
                select(KnowledgeRelease).where(
                    KnowledgeRelease.release_id == release_id, KnowledgeRelease.kb_id == kb_id
                )
            )
        ).scalar_one_or_none()
        if release is None:
            raise ValueError(f"发布清单 {release_id} 不存在")
        if release.status not in {"STAGED", "ACTIVE"}:
            raise ReleaseStateError(f"发布清单状态为 {release.status}，只有 STAGED 清单可以发布")

        previous = (
            (
                await session.execute(
                    select(KnowledgeRelease).where(KnowledgeRelease.kb_id == kb_id, KnowledgeRelease.status == "ACTIVE")
                )
            )
            .scalars()
            .all()
        )
        previous_id = None
        for item in previous:
            if item.release_id != release_id:
                item.status = "SUPERSEDED"
                item.superseded_at = utc_now()
                previous_id = previous_id or item.release_id
        release.status = "ACTIVE"
        release.published_at = utc_now()
        release.previous_release_id = previous_id
        kb.active_release_id = release.release_id
        kb.governance_status = "PUBLISHED"
        return {
            "release_id": release.release_id,
            "status": "ACTIVE",
            "previous_release_id": previous_id,
            "governance_status": kb.governance_status,
            "source_count": release.source_count,
        }


async def rollback_release(kb_id: str, operator_id: str | None) -> dict:
    """回滚：把指针切回上一个被替换的 ACTIVE 清单（原子操作 + 审计）。"""
    async with pg_manager.get_async_session_context() as session:
        kb = (await session.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))).scalar_one_or_none()
        if kb is None:
            raise ValueError(f"知识库 {kb_id} 不存在")
        current = (
            (
                await session.execute(
                    select(KnowledgeRelease).where(KnowledgeRelease.kb_id == kb_id, KnowledgeRelease.status == "ACTIVE")
                )
            )
            .scalars()
            .all()
        )
        if not current:
            raise ReleaseStateError("当前没有 ACTIVE 发布清单可回滚")
        current_ids = {item.release_id for item in current}
        # 目标：最近一次被替换的清单（previous_release_id 链优先，否则按发布时间倒序）
        target = None
        for item in current:
            if item.previous_release_id and item.previous_release_id not in current_ids:
                target_row = (
                    await session.execute(
                        select(KnowledgeRelease).where(KnowledgeRelease.release_id == item.previous_release_id)
                    )
                ).scalar_one_or_none()
                if target_row is not None and target_row.status == "SUPERSEDED":
                    target = target_row
                    break
        if target is None:
            target = (
                (
                    await session.execute(
                        select(KnowledgeRelease)
                        .where(KnowledgeRelease.kb_id == kb_id, KnowledgeRelease.status == "SUPERSEDED")
                        .order_by(KnowledgeRelease.superseded_at.desc().nullslast())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        if target is None:
            raise ReleaseStateError("没有可回滚的历史发布清单")

        for item in current:
            item.status = "SUPERSEDED"
            item.superseded_at = utc_now()
        target.status = "ACTIVE"
        target.superseded_at = None
        kb.active_release_id = target.release_id
        return {
            "rolled_back_to": target.release_id,
            "previous_release_id": [item.release_id for item in current],
            "status": "ACTIVE",
        }


async def list_releases(kb_id: str, limit: int = 50) -> dict:
    async with pg_manager.get_async_session_context() as session:
        rows = (
            (
                await session.execute(
                    select(KnowledgeRelease)
                    .where(KnowledgeRelease.kb_id == kb_id)
                    .order_by(KnowledgeRelease.created_at.desc())
                    .limit(min(limit, 200))
                )
            )
            .scalars()
            .all()
        )
        items = [
            {
                "release_id": row.release_id,
                "status": row.status,
                "contract_ref": row.contract_ref,
                "retrieval_policy_revision_id": row.retrieval_policy_revision_id,
                "source_count": row.source_count,
                "manifest_hash": row.manifest_hash,
                "previous_release_id": row.previous_release_id,
                "graph": (row.manifest_json or {}).get("graph") or None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "published_at": row.published_at.isoformat() if row.published_at else None,
            }
            for row in rows
        ]
        return {
            "releases": items,
            "active_release_id": next((item["release_id"] for item in items if item["status"] == "ACTIVE"), None),
        }


async def create_retrieval_policy_revision(kb_id: str, policy: dict, operator_id: str | None) -> dict:
    """查询策略修订（reranker/召回数/融合权重等）；变化不重建索引。"""
    from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository

    kb = await KnowledgeBaseRepository().get_by_kb_id(kb_id)
    if kb is None:
        raise ValueError(f"知识库 {kb_id} 不存在")
    if not isinstance(policy, dict) or not policy:
        raise ValueError("policy 必须是非空对象")
    policy_hash = _manifest_hash(policy)
    revision_id = f"rp_{uuid.uuid4().hex[:24]}"
    async with pg_manager.get_async_session_context() as session:
        session.add(
            KnowledgeRetrievalPolicyRevision(
                revision_id=revision_id,
                kb_id=kb_id,
                tenant_id=kb.tenant_id,
                policy_json=policy,
                policy_hash=policy_hash,
                created_by=operator_id,
            )
        )
    return {"revision_id": revision_id, "policy_hash": policy_hash, "policy": policy}


async def list_retrieval_policy_revisions(kb_id: str, limit: int = 20) -> dict:
    async with pg_manager.get_async_session_context() as session:
        rows = (
            (
                await session.execute(
                    select(KnowledgeRetrievalPolicyRevision)
                    .where(KnowledgeRetrievalPolicyRevision.kb_id == kb_id)
                    .order_by(KnowledgeRetrievalPolicyRevision.created_at.desc())
                    .limit(min(limit, 100))
                )
            )
            .scalars()
            .all()
        )
        return {
            "revisions": [
                {
                    "revision_id": row.revision_id,
                    "policy": row.policy_json,
                    "policy_hash": row.policy_hash,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "created_by": row.created_by,
                }
                for row in rows
            ]
        }


async def capability_report(kb_id: str) -> dict:
    """KB 级能力报告：Base（契约）∩ Observed（文件分布）→ Effective。

    知识库层不显示单一"部分"，而是给出覆盖率分布（完整/部分/不可用篇数）。
    """
    from yuxi.knowledge.source_contracts import SourceContractError, load_kb_contract
    from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository

    kb = await KnowledgeBaseRepository().get_by_kb_id(kb_id)
    if kb is None:
        raise ValueError(f"知识库 {kb_id} 不存在")
    try:
        spec = await load_kb_contract(kb_id)
    except SourceContractError as exc:
        raise ValueError(f"[{exc.error_code}] {exc}") from exc

    async with pg_manager.get_async_session_context() as session:
        rows = (
            (
                await session.execute(
                    select(KnowledgeFile).where(KnowledgeFile.kb_id == kb_id, KnowledgeFile.is_folder.is_(False))
                )
            )
            .scalars()
            .all()
        )
    distribution: dict[str, dict[str, int]] = {}
    files_with_report = 0
    for record in rows:
        capabilities = (record.evidence_capabilities or {}) if record.evidence_capabilities else {}
        file_caps = capabilities.get("capabilities")
        if not isinstance(file_caps, dict):
            file_caps = capabilities
        if not isinstance(file_caps, dict) or not file_caps:
            continue
        files_with_report += 1
        for name, level in file_caps.items():
            if not isinstance(level, str):
                continue
            bucket = distribution.setdefault(name, {"FULL": 0, "PARTIAL": 0, "UNSUPPORTED": 0, "OTHER": 0})
            if level in bucket:
                bucket[level] += 1
            else:
                bucket["OTHER"] += 1

    base_caps = spec.base_capabilities or {}
    capabilities = []
    names = sorted(set(base_caps) | set(distribution))
    for name in names:
        base_level = base_caps.get(name)
        dist = distribution.get(name) or {"FULL": 0, "PARTIAL": 0, "UNSUPPORTED": 0, "OTHER": 0}
        observed_total = sum(dist.values())
        # Effective：契约基线与观测最差值取交（无观测时等于基线）
        observed_levels = [level for level, count in dist.items() if count > 0 and level != "OTHER"]
        worst_observed = (
            min(observed_levels, key=lambda lv: CAPABILITY_LEVEL_RANK.get(lv, -1)) if observed_levels else None
        )
        effective = base_level
        if base_level and worst_observed:
            effective = (
                worst_observed
                if CAPABILITY_LEVEL_RANK.get(worst_observed, -1) < CAPABILITY_LEVEL_RANK.get(base_level, -1)
                else base_level
            )
        capabilities.append(
            {
                "capability": name,
                "base_level": base_level,
                "distribution": dist,
                "files_reported": observed_total,
                "effective_level": effective,
            }
        )
    return {
        "kb_id": kb_id,
        "contract_ref": spec.contract_ref,
        "total_files": len(rows),
        "files_with_capability_report": files_with_report,
        "capabilities": capabilities,
    }


async def archive_kb(kb_id: str, operator_id: str | None) -> dict:
    """治理状态归档：检索入口在路由层由 governance_status 拦截（归档库不再进 Agent 工具刷新）。"""
    from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository

    kb = await KnowledgeBaseRepository().get_by_kb_id(kb_id)
    if kb is None:
        raise ValueError(f"知识库 {kb_id} 不存在")
    if kb.governance_status == "ARCHIVED":
        raise ReleaseStateError("知识库已处于 ARCHIVED 状态")
    previous = kb.governance_status or "DRAFT"
    await KnowledgeBaseRepository().update(kb_id, {"governance_status": "ARCHIVED"})
    logger.info(f"[release] KB {kb_id} 归档：{previous} → ARCHIVED (operator={operator_id})")
    return {"kb_id": kb_id, "previous": previous, "governance_status": "ARCHIVED"}
