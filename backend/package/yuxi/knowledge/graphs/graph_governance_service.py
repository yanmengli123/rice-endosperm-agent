"""知识图谱治理服务：治理设置（审计化）、聚合总览（TTL 缓存）、批量批准准入、发布 go/no-go。

与纯展示的 graph_view_settings 分离：review_policy / batch_admission / maker_checker /
review_sla_hours 等生产治理参数的权威存储是 knowledge_bases.graph_governance_settings，
变更必须经 :meth:`GraphGovernanceService.update_settings`（写 append-only 审计）。

缓存说明：summary 是治理头的高频轮询入口，进程内 TTL 缓存（默认 15s，可配 5-300s）；
写操作（决策/门禁裁决/冲突处置/设置变更）尽力失效本进程缓存。当前 api 服务为单
进程部署，跨进程一致性依赖 TTL 自然过期。
"""

from __future__ import annotations

import time
from typing import Any

from yuxi.knowledge.graphs.graph_evidence_service import (
    TRUST_VERIFIED_CORROBORATED,
    TRUST_VERIFIED_SINGLE,
    VERIFICATION_OK,
    GraphEvidenceService,
)
from yuxi.knowledge.graphs.milvus_graph_service import (
    REVIEW_POLICIES,
    REVIEW_POLICY_CANDIDATES_VISIBLE,
    MilvusGraphService,
)
from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository
from yuxi.repositories.knowledge_graph_review_repository import BATCH_ADMISSION_LEVELS, KnowledgeGraphReviewRepository
from yuxi.services.principal import resolve_tenant_id
from yuxi.storage.postgres.manager import pg_manager
from yuxi.utils.datetime_utils import utc_now

AUDIT_GOVERNANCE_SETTINGS_UPDATE = "GOVERNANCE_SETTINGS_UPDATE"
BATCH_ADMISSION_DEFAULT = "strict"
SUMMARY_CACHE_TTL_DEFAULT = 15
_SUMMARY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def invalidate_governance_summary(kb_id: str) -> None:
    """写路径尽力失效 summary 缓存（同进程立即生效，跨进程等 TTL）。"""
    _SUMMARY_CACHE.pop(kb_id, None)


def normalize_governance_settings(value: object) -> dict[str, Any]:
    """治理设置规范化：始终返回完整键集，非法值回落默认（不抛错，防脏数据堵死读路径）。"""
    raw = value if isinstance(value, dict) else {}
    review_policy = raw.get("review_policy")
    batch_admission = raw.get("batch_admission")
    sla = raw.get("review_sla_hours")
    ttl = raw.get("summary_cache_ttl_seconds")
    return {
        "review_policy": review_policy if review_policy in REVIEW_POLICIES else REVIEW_POLICY_CANDIDATES_VISIBLE,
        "batch_admission": batch_admission if batch_admission in BATCH_ADMISSION_LEVELS else BATCH_ADMISSION_DEFAULT,
        "maker_checker": bool(raw.get("maker_checker")),
        "review_sla_hours": float(sla) if isinstance(sla, (int, float)) and float(sla) > 0 else None,
        "summary_cache_ttl_seconds": (
            int(ttl) if isinstance(ttl, (int, float)) and 5 <= int(ttl) <= 300 else SUMMARY_CACHE_TTL_DEFAULT
        ),
    }


def evaluate_batch_admission(
    admission: str,
    *,
    trust_tier: str,
    verification_summary: dict[str, int],
    conflict_status: str | None = None,
) -> tuple[bool, str]:
    """批量批准准入判定（纯函数）。

    任何档位都要求至少一条逐字校验 OK 的引文（没有可固定证据的候选必须逐条人工）；
    strict 额外要求机器验证 + ≥2 篇文献佐证（VERIFIED_CORROBORATED）；standard 放宽到
    任一机器验证（VERIFIED_SINGLE）；relaxed 只要 OK 引文。冲突中的候选一律不可批量。
    返回 (是否准入, 原因代码)。
    """
    if (conflict_status or "NONE") == "CONTESTED":
        return False, "conflict_open"
    ok_quotes = int(verification_summary.get(VERIFICATION_OK, 0) or 0)
    if ok_quotes <= 0:
        return False, "no_ok_quote"
    if admission == "relaxed":
        return True, "ok_quote"
    if trust_tier == TRUST_VERIFIED_CORROBORATED:
        return True, "corroborated"
    if admission == "standard" and trust_tier == TRUST_VERIFIED_SINGLE:
        return True, "single_source"
    return False, "below_threshold"


def pick_best_pinned_chunk_id(mentions: list[dict[str, Any]]) -> str | None:
    """批量批准的默认 pin 目标：已 pinned 的 OK 引文优先，否则排序最前（OK/高置信/非 hedge）的 OK 引文。"""
    for mention in mentions:
        if mention.get("verification") == VERIFICATION_OK and mention.get("pinned_by"):
            return mention.get("chunk_id")
    for mention in mentions:  # mentions 已按 _mention_rank 排序（OK 优先、置信度降序）
        if mention.get("verification") == VERIFICATION_OK and mention.get("quote"):
            return mention.get("chunk_id")
    return None


class GraphGovernanceService:
    def __init__(
        self,
        *,
        kb_repo: KnowledgeBaseRepository | None = None,
        review_repo: KnowledgeGraphReviewRepository | None = None,
        graph_service: MilvusGraphService | None = None,
        evidence_service: GraphEvidenceService | None = None,
        chunk_counter: Any = None,
    ):
        self.kb_repo = kb_repo or KnowledgeBaseRepository()
        self.review_repo = review_repo or KnowledgeGraphReviewRepository()
        self._graph_service = graph_service
        self._evidence_service = evidence_service
        self._chunk_counter = chunk_counter

    @property
    def graph_service(self) -> MilvusGraphService:
        if self._graph_service is None:
            self._graph_service = MilvusGraphService()
        return self._graph_service

    @property
    def evidence_service(self) -> GraphEvidenceService:
        if self._evidence_service is None:
            self._evidence_service = GraphEvidenceService(review_repo=self.review_repo)
        return self._evidence_service

    # ── 治理设置（读写均带默认合并；写路径 append-only 审计）─────

    async def get_settings(self, kb_id: str) -> dict[str, Any]:
        record = await self.kb_repo.get_by_kb_id(kb_id)
        if record is None:
            raise ValueError(f"知识库 {kb_id} 不存在")
        governance = getattr(record, "graph_governance_settings", None)
        settings = normalize_governance_settings(governance)
        if not isinstance(governance, dict) or not governance:
            # 迁移 0047 之前的兜底：旧显示设置里可能还残留 review_policy
            legacy = getattr(record, "graph_view_settings", None)
            if isinstance(legacy, dict) and legacy.get("review_policy") in REVIEW_POLICIES:
                settings["review_policy"] = legacy["review_policy"]
        return settings

    async def update_settings(self, kb_id: str, *, actor_uid: str, changes: dict[str, Any]) -> dict[str, Any]:
        """更新治理设置 = 审计 before/after + 写列 + 失效 summary 缓存。

        只接受白名单键；review_policy 变更直接影响生产 Graph-RAG 行为，
        因此所有变更都进审计账本（action=GOVERNANCE_SETTINGS_UPDATE）。
        """
        allowed = {"review_policy", "batch_admission", "maker_checker", "review_sla_hours", "summary_cache_ttl_seconds"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"不支持的治理设置项: {sorted(unknown)}")
        before = await self.get_settings(kb_id)
        merged = {
            **before,
            **{key: value for key, value in changes.items() if value is not None or key == "review_sla_hours"},
        }
        normalized = normalize_governance_settings(merged)
        if normalized == before:
            return {"settings": normalized, "unchanged": True}
        await self.kb_repo.update(kb_id, {"graph_governance_settings": normalized})
        tenant_id = await self._tenant_id(actor_uid)
        await self.review_repo.append_audit(
            kb_id=kb_id,
            tenant_id=tenant_id,
            target_kind="kb",
            target_id=kb_id,
            action=AUDIT_GOVERNANCE_SETTINGS_UPDATE,
            actor_uid=actor_uid,
            before_snapshot=before,
            after_snapshot=normalized,
            reason="治理设置更新（review_policy 影响生产检索，必须可追责）",
        )
        invalidate_governance_summary(kb_id)
        return {"settings": normalized, "unchanged": False}

    @staticmethod
    async def _tenant_id(actor_uid: str) -> int:
        async with pg_manager.get_async_session_context() as session:
            return await resolve_tenant_id(session, actor_uid)

    # ── 聚合总览（治理头指标；TTL 缓存 + refresh 直通）──────────

    async def summary(self, kb_id: str, *, refresh: bool = False) -> dict[str, Any]:
        cached = _SUMMARY_CACHE.get(kb_id)
        now = time.monotonic()
        if not refresh and cached and cached[0] > now:
            payload = dict(cached[1])
            payload["cached"] = True
            return payload
        payload = await self._build_summary(kb_id)
        ttl = float(payload.get("settings", {}).get("summary_cache_ttl_seconds") or SUMMARY_CACHE_TTL_DEFAULT)
        _SUMMARY_CACHE[kb_id] = (now + ttl, payload)
        return {**payload, "cached": False}

    async def _build_summary(self, kb_id: str) -> dict[str, Any]:
        settings = await self.get_settings(kb_id)
        review_repo = self.review_repo
        gate_page = await review_repo.list_gate_reviews(kb_id, status="PENDING", page=1, page_size=1)
        conflict_page = await review_repo.list_conflicts(kb_id, status="OPEN", page=1, page_size=1)
        integrity = await review_repo.integrity_counts(kb_id)
        rejected_triple_ids = integrity.pop("rejected_triple_ids", [])
        integrity["I4_rejected_triple_ids_count"] = len(rejected_triple_ids)
        build = await self.graph_service.get_status(kb_id)
        summary: dict[str, Any] = {
            "kb_id": kb_id,
            "generated_at": utc_now().isoformat(),
            "settings": settings,
            "counts": await review_repo.count_statuses(kb_id),
            "gates": {"pending": gate_page.get("counts", {}).get("PENDING", {})},
            "conflicts": {"open": conflict_page.get("counts", {}).get("OPEN", {})},
            # 轻量口径：纯 SQL 计数（I3/I5/I6），不做引文逐条重验；
            # 完整重验审计在质量治理页显式触发（GET /graph/integrity）。
            "integrity": integrity,
            "build": {
                key: build.get(key)
                for key in (
                    "configured",
                    "locked",
                    "total_chunks",
                    "pending_chunks",
                    "indexed_chunks",
                    "dead_chunks",
                    "stale_cached_chunks",
                    "build_task_status",
                    "build_task_progress",
                )
            },
            "release": await self._release_summary(kb_id),
            "last_audit_at": await review_repo.last_audit_at(kb_id),
        }
        return summary

    async def _release_summary(self, kb_id: str) -> dict[str, Any]:
        from sqlalchemy import select

        from yuxi.storage.postgres.models_knowledge import KnowledgeBase, KnowledgeRelease

        async with pg_manager.get_async_session_context() as session:
            kb = (await session.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))).scalar_one_or_none()
            latest = (
                (
                    await session.execute(
                        select(KnowledgeRelease)
                        .where(KnowledgeRelease.kb_id == kb_id)
                        .order_by(KnowledgeRelease.created_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
        if latest is None:
            return {"active_release_id": getattr(kb, "active_release_id", None), "last_release": None}
        return {
            "active_release_id": getattr(kb, "active_release_id", None),
            "last_release": {
                "release_id": latest.release_id,
                "status": latest.status,
                "manifest_hash": latest.manifest_hash,
                "retrieval_policy_revision_id": latest.retrieval_policy_revision_id,
                "created_at": latest.created_at.isoformat() if latest.created_at else None,
                "published_at": latest.published_at.isoformat() if latest.published_at else None,
                "graph_decisions_frozen": bool((latest.manifest_json or {}).get("graph")),
            },
        }

    # ── 批量批准准入预检（P0：显式证据 + 准入档位）───────────────

    async def batch_preview(
        self, kb_id: str, targets: list[dict[str, Any]], *, admission: str | None = None
    ) -> dict[str, Any]:
        """批量批准预检：逐条判定准入并给出默认 pin 的证据 chunk。

        不写任何数据；确认页据此展示筛选条件、准入/阻断明细与抽样预览，
        随后 batch 请求逐条携带 pinned_chunk_id。
        """
        settings = await self.get_settings(kb_id)
        level = admission if admission in BATCH_ADMISSION_LEVELS else settings["batch_admission"]
        items: list[dict[str, Any]] = []
        admissible = 0
        for target in targets[:500]:
            target_id = str(target.get("id") or target.get("target_id") or "")
            if not target_id:
                continue
            try:
                evidence = await self.evidence_service.triple_evidence(kb_id, target_id)
            except ValueError:
                items.append({"target_id": target_id, "admissible": False, "reason": "not_found"})
                continue
            ok, reason = evaluate_batch_admission(
                level,
                trust_tier=evidence.get("trust_tier") or "CANDIDATE",
                verification_summary=evidence.get("verification_summary") or {},
                conflict_status=evidence.get("conflict_status"),
            )
            if ok:
                admissible += 1
            items.append(
                {
                    "target_id": target_id,
                    "admissible": ok,
                    "reason": reason,
                    "trust_tier": evidence.get("trust_tier"),
                    "verification_summary": evidence.get("verification_summary"),
                    "pinned_chunk_id": pick_best_pinned_chunk_id(evidence.get("mentions") or []),
                    "preview_quote": next(
                        (m.get("quote") for m in evidence.get("mentions") or [] if m.get("quote")), None
                    ),
                }
            )
        return {
            "kb_id": kb_id,
            "admission_level": level,
            "items": items,
            "admissible_count": admissible,
            "blocked_count": len(items) - admissible,
        }

    # ── 发布 go/no-go（P2：publish 前的门禁评估）────────────────

    async def evaluate_publish_gates(self, kb_id: str, *, operator_uid: str) -> dict[str, Any]:
        """发布门禁：完整性违规阻断（blocker）；死信/门禁送审/开放冲突披露（warning）。

        maker_checker 开启时，发布人不得出现在本库现行决策的 actor 集合中
        （审核与发布职责分离）。返回 go/no-go 与明细，router 层据此 409 或放行。
        """
        settings = await self.get_settings(kb_id)
        integrity = await self.review_repo.integrity_counts(kb_id)
        integrity.pop("rejected_triple_ids", None)
        blockers: list[str] = []
        warnings: list[str] = []
        integrity_violations = {key: value for key, value in integrity.items() if key.startswith("I") and value}
        if integrity_violations:
            blockers.append({"code": "integrity_violation", "detail": integrity_violations})
        gate_page = await self.review_repo.list_gate_reviews(kb_id, status="PENDING", page=1, page_size=1)
        pending_gates = int(gate_page.get("counts", {}).get("PENDING", {}).get("_total", 0) or 0)
        if pending_gates:
            warnings.append({"code": "gate_reviews_pending", "detail": {"count": pending_gates}})
        conflict_page = await self.review_repo.list_conflicts(kb_id, status="OPEN", page=1, page_size=1)
        open_conflicts = int(conflict_page.get("counts", {}).get("OPEN", {}).get("_total", 0) or 0)
        if open_conflicts:
            warnings.append({"code": "conflicts_open", "detail": {"count": open_conflicts}})
        dead = await self._count_dead_chunks(kb_id)
        if dead:
            warnings.append({"code": "dead_chunks", "detail": {"count": dead}})
        if settings["maker_checker"]:
            actors = await self.review_repo.list_decision_actors(kb_id)
            if operator_uid in actors:
                blockers.append(
                    {"code": "maker_checker_violation", "detail": "发布人曾参与本库审核决策，与 maker-checker 策略冲突"}
                )
        return {
            "kb_id": kb_id,
            "go": not blockers,
            "blockers": blockers,
            "warnings": warnings,
            "checked_at": utc_now().isoformat(),
        }

    async def _count_dead_chunks(self, kb_id: str) -> int:
        counter = self._chunk_counter
        if counter is None:
            counter = getattr(self.graph_service.chunk_repo, "count_graph_dead_by_kb_id", None)
        if counter is None:
            return 0
        try:
            return int(await counter(kb_id))
        except Exception:  # noqa: BLE001
            return 0
