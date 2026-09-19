"""人工审核决策叠加层的持久化边界（决策表、append-only 审计表、缓存状态列、队列查询）。

决策与审计表独立于 knowledge_graph_* 五张图谱表：reset/重建删图谱行不删决策，
重放钩子据此恢复审核状态。审计表只 INSERT，本仓储不提供任何 UPDATE/DELETE。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from yuxi.knowledge.graphs.review_overlay import KIND_ENTITY, KIND_TRIPLE, STATUS_CANONICAL
from yuxi.repositories.knowledge_graph_repository import build_extracted_alias_rows, refresh_triple_support_statement
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeChunk,
    KnowledgeFile,
    KnowledgeGraphConflict,
    KnowledgeGraphEntity,
    KnowledgeGraphEntityAlias,
    KnowledgeGraphEntityMention,
    KnowledgeGraphGateReview,
    KnowledgeGraphReviewAudit,
    KnowledgeGraphReviewDecision,
    KnowledgeGraphTriple,
    KnowledgeGraphTripleMention,
)
from yuxi.utils import hashstr
from yuxi.utils.datetime_utils import utc_now

QUEUE_ORDERS = ("support_asc", "support_desc", "recent")

# 门禁送审与冲突的裁决动作（写进审计账本的 action）
GATE_REVIEW_PROMOTE = "GATE_PROMOTE"
GATE_REVIEW_DISCARD = "GATE_DISCARD"
CONFLICT_RESOLVE = "CONFLICT_RESOLVE"


class ReviewConflictError(Exception):
    """乐观并发冲突：调用方携带的 if_version 已过期。"""


class ReviewTargetReadOnlyError(ValueError):
    """CANONICAL（托管导入）对象不接受人工审核决策。"""


def decision_key(kb_id: str, target_kind: str, target_id: str) -> str:
    return hashstr(f"{kb_id}:{target_kind}:{target_id}", length=32)


class KnowledgeGraphReviewRepository:
    # ── 决策 ─────────────────────────────────────────────────────

    async def list_decisions(self, kb_id: str) -> list[dict[str, Any]]:
        async with pg_manager.get_async_session_context() as session:
            rows = (
                (
                    await session.execute(
                        select(KnowledgeGraphReviewDecision).where(KnowledgeGraphReviewDecision.kb_id == kb_id)
                    )
                )
                .scalars()
                .all()
            )
            return [_decision_dict(row) for row in rows]

    async def get_decision(self, kb_id: str, target_kind: str, target_id: str) -> dict[str, Any] | None:
        async with pg_manager.get_async_session_context() as session:
            row = await self._get_decision_row(session, kb_id, target_kind, target_id)
            return _decision_dict(row) if row else None

    async def save_decision(
        self,
        *,
        kb_id: str,
        tenant_id: int,
        target_kind: str,
        target_id: str,
        action: str,
        actor_uid: str,
        payload: dict[str, Any] | None = None,
        pinned_chunk_id: str | None = None,
        pinned_quote: str | None = None,
        reason: str | None = None,
        if_version: int | None = None,
        cache_status: str | None = None,
        before_snapshot: dict[str, Any] | None = None,
        after_snapshot: dict[str, Any] | None = None,
        batch_id: str | None = None,
        audit_action: str | None = None,
    ) -> dict[str, Any]:
        """一个事务完成：决策 upsert（版本校验）+ 审计追加 + 缓存状态列更新。"""
        async with pg_manager.get_async_session_context() as session:
            row = await self._get_decision_row(session, kb_id, target_kind, target_id)
            if row is None:
                if if_version not in (None, 0):
                    raise ReviewConflictError(f"{target_kind} {target_id} 尚无决策，if_version={if_version} 无效")
                row = KnowledgeGraphReviewDecision(
                    decision_id=decision_key(kb_id, target_kind, target_id),
                    kb_id=kb_id,
                    tenant_id=tenant_id,
                    target_kind=target_kind,
                    target_id=target_id,
                    version=1,
                )
                session.add(row)
            else:
                if if_version is not None and int(row.version) != int(if_version):
                    raise ReviewConflictError(
                        f"{target_kind} {target_id} 决策版本已变为 {row.version}（请求 if_version={if_version}）"
                    )
                row.version = int(row.version) + 1
            row.action = action
            row.payload = payload
            row.pinned_chunk_id = pinned_chunk_id
            row.pinned_quote = pinned_quote
            row.reason = reason
            row.actor_uid = actor_uid
            row.updated_at = utc_now()
            session.add(
                KnowledgeGraphReviewAudit(
                    kb_id=kb_id,
                    tenant_id=tenant_id,
                    target_kind=target_kind,
                    target_id=target_id,
                    action=audit_action or action,
                    actor_uid=actor_uid,
                    before_snapshot=before_snapshot,
                    after_snapshot=after_snapshot,
                    reason=reason,
                    batch_id=batch_id,
                )
            )
            if cache_status:
                await session.execute(_status_update(target_kind, [target_id], cache_status, int(row.version)))
            await session.flush()
            return _decision_dict(row)

    async def append_audit(
        self,
        *,
        kb_id: str,
        tenant_id: int,
        target_kind: str,
        target_id: str,
        action: str,
        actor_uid: str,
        before_snapshot: dict[str, Any] | None = None,
        after_snapshot: dict[str, Any] | None = None,
        reason: str | None = None,
        batch_id: str | None = None,
    ) -> None:
        async with pg_manager.get_async_session_context() as session:
            session.add(
                KnowledgeGraphReviewAudit(
                    kb_id=kb_id,
                    tenant_id=tenant_id,
                    target_kind=target_kind,
                    target_id=target_id,
                    action=action,
                    actor_uid=actor_uid,
                    before_snapshot=before_snapshot,
                    after_snapshot=after_snapshot,
                    reason=reason,
                    batch_id=batch_id,
                )
            )

    async def list_audit(self, kb_id: str, *, target_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        async with pg_manager.get_async_session_context() as session:
            stmt = select(KnowledgeGraphReviewAudit).where(KnowledgeGraphReviewAudit.kb_id == kb_id)
            if target_id:
                stmt = stmt.where(KnowledgeGraphReviewAudit.target_id == target_id)
            rows = (
                (await session.execute(stmt.order_by(KnowledgeGraphReviewAudit.id.desc()).limit(max(1, limit))))
                .scalars()
                .all()
            )
            return [
                {
                    "id": row.id,
                    "target_kind": row.target_kind,
                    "target_id": row.target_id,
                    "action": row.action,
                    "actor_uid": row.actor_uid,
                    "before_snapshot": row.before_snapshot,
                    "after_snapshot": row.after_snapshot,
                    "reason": row.reason,
                    "batch_id": row.batch_id,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]

    # ── 缓存状态 / pinned ────────────────────────────────────────

    async def set_review_status(self, target_kind: str, target_ids: list[str], status: str) -> int:
        if not target_ids:
            return 0
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(_status_update(target_kind, target_ids, status, None))
            return int(result.rowcount or 0)

    async def pin_mention(self, target_kind: str, target_id: str, chunk_id: str, actor_uid: str) -> int:
        model, key = _mention_model(target_kind)
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                update(model)
                .where(key == target_id, model.chunk_id == chunk_id)
                .values(pinned_by=actor_uid, pinned_at=utc_now())
            )
            return int(result.rowcount or 0)

    async def add_entity_aliases(
        self, kb_id: str, entity_id: str, entity_name: str, aliases: list[str], *, alias_type: str = "HUMAN"
    ) -> int:
        """人工补充的别名进别名表（与 IMPORTED/EXTRACTED 同表不同类型），下次词法层自动变厚。"""
        rows = build_extracted_alias_rows(kb_id, [{"entity_id": entity_id, "name": entity_name, "aliases": aliases}])
        if not rows:
            return 0
        for row in rows:
            row["alias_type"] = alias_type
            row["source"] = "human_review"
        async with pg_manager.get_async_session_context() as session:
            result = await session.execute(
                insert(KnowledgeGraphEntityAlias)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["kb_id", "normalized_alias", "entity_id"])
            )
            return int(result.rowcount or 0)

    async def list_promoted_aliases(self, kb_id: str) -> list[dict[str, Any]]:
        """R4c：ALIAS_PROMOTE 晋升的 KB 级官方别名（带实体 label，供词法层装载）。"""
        async with pg_manager.get_async_session_context() as session:
            rows = (
                await session.execute(
                    select(KnowledgeGraphEntityAlias.id, KnowledgeGraphEntityAlias.alias, KnowledgeGraphEntity.label)
                    .join(KnowledgeGraphEntity, KnowledgeGraphEntity.entity_id == KnowledgeGraphEntityAlias.entity_id)
                    .where(
                        KnowledgeGraphEntityAlias.kb_id == kb_id,
                        KnowledgeGraphEntityAlias.alias_type == "DOCLEX_PROMOTED",
                    )
                )
            ).all()
        return [
            {"alias_id": row[0], "alias": row[1], "label": row[2]} for row in rows if row[1] and str(row[1]).strip()
        ]

    # ── 读取 ─────────────────────────────────────────────────────

    async def get_triple_with_endpoints(self, kb_id: str, triple_id: str) -> dict[str, Any] | None:
        async with pg_manager.get_async_session_context() as session:
            triple = (
                await session.execute(
                    select(KnowledgeGraphTriple).where(
                        KnowledgeGraphTriple.kb_id == kb_id, KnowledgeGraphTriple.triple_id == triple_id
                    )
                )
            ).scalar_one_or_none()
            if triple is None:
                return None
            entities = (
                (
                    await session.execute(
                        select(KnowledgeGraphEntity).where(
                            KnowledgeGraphEntity.entity_id.in_([triple.source_entity_id, triple.target_entity_id])
                        )
                    )
                )
                .scalars()
                .all()
            )
            mentions = (
                await session.execute(
                    select(KnowledgeGraphTripleMention, KnowledgeChunk.content)
                    .join(KnowledgeChunk, KnowledgeChunk.chunk_id == KnowledgeGraphTripleMention.chunk_id)
                    .where(KnowledgeGraphTripleMention.triple_id == triple_id)
                )
            ).all()
        by_id = {row.entity_id: _entity_dict(row) for row in entities}
        return {
            "triple": _triple_dict(triple),
            "source": by_id.get(triple.source_entity_id),
            "target": by_id.get(triple.target_entity_id),
            "mentions": [
                {
                    "chunk_id": mention.chunk_id,
                    "file_id": mention.file_id,
                    "quote": mention.text,
                    "chunk_content": content,
                    "pinned_by": mention.pinned_by,
                    "extractor_type": mention.extractor_type,
                }
                for mention, content in mentions
            ],
        }

    async def get_entity(self, kb_id: str, entity_id: str) -> dict[str, Any] | None:
        async with pg_manager.get_async_session_context() as session:
            row = (
                await session.execute(
                    select(KnowledgeGraphEntity).where(
                        KnowledgeGraphEntity.kb_id == kb_id, KnowledgeGraphEntity.entity_id == entity_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            mentions = (
                await session.execute(
                    select(KnowledgeGraphEntityMention, KnowledgeChunk.content)
                    .join(KnowledgeChunk, KnowledgeChunk.chunk_id == KnowledgeGraphEntityMention.chunk_id)
                    .where(KnowledgeGraphEntityMention.entity_id == entity_id)
                )
            ).all()
            endpoint_triple_ids = (
                (
                    await session.execute(
                        select(KnowledgeGraphTriple.triple_id).where(
                            KnowledgeGraphTriple.kb_id == kb_id,
                            or_(
                                KnowledgeGraphTriple.source_entity_id == entity_id,
                                KnowledgeGraphTriple.target_entity_id == entity_id,
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
        return {
            "entity": _entity_dict(row),
            "mentions": [
                {"chunk_id": m.chunk_id, "file_id": m.file_id, "quote": m.text, "chunk_content": content}
                for m, content in mentions
            ],
            "triple_ids": list(endpoint_triple_ids),
        }

    async def count_statuses(self, kb_id: str) -> dict[str, dict[str, int]]:
        async with pg_manager.get_async_session_context() as session:
            result: dict[str, dict[str, int]] = {}
            for name, model in (("triples", KnowledgeGraphTriple), ("entities", KnowledgeGraphEntity)):
                rows = (
                    await session.execute(
                        select(model.review_status, func.count())
                        .where(model.kb_id == kb_id)
                        .group_by(model.review_status)
                    )
                ).all()
                result[name] = {str(status): int(count) for status, count in rows}
            return result

    async def integrity_counts(self, kb_id: str, *, rejected_limit: int = 2000) -> dict[str, Any]:
        """审核层不变式计数：I3 APPROVED 无 pinned 引文、I5 决策无审计、I6 CANONICAL 带决策；
        附 REJECTED 三元组 ID 供投影核对（I4）。"""
        pinned_triple_mention = exists().where(
            KnowledgeGraphTripleMention.triple_id == KnowledgeGraphTriple.triple_id,
            KnowledgeGraphTripleMention.pinned_by.is_not(None),
            KnowledgeGraphTripleMention.text.is_not(None),
        )
        pinned_entity_mention = exists().where(
            KnowledgeGraphEntityMention.entity_id == KnowledgeGraphEntity.entity_id,
            KnowledgeGraphEntityMention.pinned_by.is_not(None),
            KnowledgeGraphEntityMention.text.is_not(None),
        )
        decision_has_audit = exists().where(
            KnowledgeGraphReviewAudit.kb_id == KnowledgeGraphReviewDecision.kb_id,
            KnowledgeGraphReviewAudit.target_id == KnowledgeGraphReviewDecision.target_id,
        )
        canonical_triple_with_decision = exists().where(
            KnowledgeGraphTriple.triple_id == KnowledgeGraphReviewDecision.target_id,
            KnowledgeGraphTriple.review_status == STATUS_CANONICAL,
        )
        canonical_entity_with_decision = exists().where(
            KnowledgeGraphEntity.entity_id == KnowledgeGraphReviewDecision.target_id,
            KnowledgeGraphEntity.review_status == STATUS_CANONICAL,
        )
        async with pg_manager.get_async_session_context() as session:
            approved_triples_without_pin = await session.scalar(
                select(func.count())
                .select_from(KnowledgeGraphTriple)
                .where(
                    KnowledgeGraphTriple.kb_id == kb_id,
                    KnowledgeGraphTriple.review_status == "APPROVED",
                    ~pinned_triple_mention,
                )
            )
            approved_entities_without_pin = await session.scalar(
                select(func.count())
                .select_from(KnowledgeGraphEntity)
                .where(
                    KnowledgeGraphEntity.kb_id == kb_id,
                    KnowledgeGraphEntity.review_status == "APPROVED",
                    ~pinned_entity_mention,
                )
            )
            decisions_without_audit = await session.scalar(
                select(func.count())
                .select_from(KnowledgeGraphReviewDecision)
                .where(KnowledgeGraphReviewDecision.kb_id == kb_id, ~decision_has_audit)
            )
            canonical_with_decision = await session.scalar(
                select(func.count())
                .select_from(KnowledgeGraphReviewDecision)
                .where(
                    KnowledgeGraphReviewDecision.kb_id == kb_id,
                    or_(canonical_triple_with_decision, canonical_entity_with_decision),
                )
            )
            decisions_total = await session.scalar(
                select(func.count())
                .select_from(KnowledgeGraphReviewDecision)
                .where(KnowledgeGraphReviewDecision.kb_id == kb_id)
            )
            rejected_triple_ids = list(
                (
                    await session.execute(
                        select(KnowledgeGraphTriple.triple_id)
                        .where(KnowledgeGraphTriple.kb_id == kb_id, KnowledgeGraphTriple.review_status == "REJECTED")
                        .limit(rejected_limit)
                    )
                )
                .scalars()
                .all()
            )
        return {
            "decisions_total": int(decisions_total or 0),
            "I3_approved_triples_without_pinned_quote": int(approved_triples_without_pin or 0),
            "I3_approved_entities_without_pinned_quote": int(approved_entities_without_pin or 0),
            "I5_decisions_without_audit": int(decisions_without_audit or 0),
            "I6_canonical_with_decision": int(canonical_with_decision or 0),
            "rejected_triple_ids": rejected_triple_ids,
        }

    async def list_queue(
        self,
        kb_id: str,
        *,
        status: str,
        page: int = 1,
        page_size: int = 20,
        file_id: str | None = None,
        order: str = "support_asc",
    ) -> dict[str, Any]:
        """审核队列：分页取三元组 + 两端实体名 + mention 聚合（引文预览、推测/触发词/复核任一）。"""
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        conditions = [KnowledgeGraphTriple.kb_id == kb_id, KnowledgeGraphTriple.review_status == status]
        if file_id:
            conditions.append(
                exists().where(
                    KnowledgeGraphTripleMention.triple_id == KnowledgeGraphTriple.triple_id,
                    KnowledgeGraphTripleMention.file_id == file_id,
                )
            )
        if order == "support_desc":
            ordering = (KnowledgeGraphTriple.support_count.desc(), KnowledgeGraphTriple.id.asc())
        elif order == "recent":
            ordering = (KnowledgeGraphTriple.updated_at.desc(), KnowledgeGraphTriple.id.desc())
        else:
            ordering = (KnowledgeGraphTriple.support_count.asc(), KnowledgeGraphTriple.id.asc())
        async with pg_manager.get_async_session_context() as session:
            total = await session.scalar(select(func.count()).select_from(KnowledgeGraphTriple).where(*conditions))
            triples = (
                (
                    await session.execute(
                        select(KnowledgeGraphTriple)
                        .where(*conditions)
                        .order_by(*ordering)
                        .offset((page - 1) * page_size)
                        .limit(page_size)
                    )
                )
                .scalars()
                .all()
            )
            entity_ids = {t.source_entity_id for t in triples} | {t.target_entity_id for t in triples}
            entities = (
                (
                    await session.execute(
                        select(KnowledgeGraphEntity).where(KnowledgeGraphEntity.entity_id.in_(list(entity_ids)))
                    )
                )
                .scalars()
                .all()
                if entity_ids
                else []
            )
            triple_ids = [t.triple_id for t in triples]
            mentions = (
                (
                    await session.execute(
                        select(KnowledgeGraphTripleMention).where(KnowledgeGraphTripleMention.triple_id.in_(triple_ids))
                    )
                )
                .scalars()
                .all()
                if triple_ids
                else []
            )
        entity_by_id = {row.entity_id: _entity_dict(row) for row in entities}
        mentions_by_triple: dict[str, list[Any]] = {}
        for mention in mentions:
            mentions_by_triple.setdefault(mention.triple_id, []).append(mention)
        items = []
        for triple in triples:
            triple_mentions = mentions_by_triple.get(triple.triple_id, [])
            preview = next((m.text for m in triple_mentions if m.text), None)
            items.append(
                {
                    **_triple_dict(triple),
                    "source": entity_by_id.get(triple.source_entity_id),
                    "target": entity_by_id.get(triple.target_entity_id),
                    "preview_quote": preview,
                    "mention_count": len(triple_mentions),
                    "file_count": len({m.file_id for m in triple_mentions}),
                    "hedge_any": any(bool(m.hedge) for m in triple_mentions),
                    "trigger_verified_any": any(bool(m.trigger_verified) for m in triple_mentions),
                    "verifier_confirmed_any": any(bool(m.verifier_confirmed) for m in triple_mentions),
                    "max_confidence": max(
                        (float(m.confidence) for m in triple_mentions if m.confidence is not None), default=None
                    ),
                    "pinned_any": any(bool(m.pinned_by) for m in triple_mentions),
                }
            )
        return {"items": items, "total": int(total or 0), "page": page, "page_size": page_size}

    # ── 单 chunk 引用清理（重抽前）────────────────────────────────

    async def delete_chunk_references(self, chunk_id: str, *, keep_pinned: bool = True) -> dict[str, list[str]]:
        """删除该 chunk 的（未 pinned）mention，清孤儿三元组/实体并重算佐证；pinned 证据保留（I3）。"""
        async with pg_manager.get_async_session_context() as session:
            triple_filter = [KnowledgeGraphTripleMention.chunk_id == chunk_id]
            entity_filter = [KnowledgeGraphEntityMention.chunk_id == chunk_id]
            if keep_pinned:
                triple_filter.append(KnowledgeGraphTripleMention.pinned_by.is_(None))
                entity_filter.append(KnowledgeGraphEntityMention.pinned_by.is_(None))
            affected_triple_ids = list(
                (await session.execute(select(KnowledgeGraphTripleMention.triple_id).where(*triple_filter).distinct()))
                .scalars()
                .all()
            )
            affected_entity_ids = list(
                (await session.execute(select(KnowledgeGraphEntityMention.entity_id).where(*entity_filter).distinct()))
                .scalars()
                .all()
            )
            await session.execute(delete(KnowledgeGraphTripleMention).where(*triple_filter))
            await session.execute(delete(KnowledgeGraphEntityMention).where(*entity_filter))

            orphan_triple_ids: list[str] = []
            if affected_triple_ids:
                has_mention = exists().where(KnowledgeGraphTripleMention.triple_id == KnowledgeGraphTriple.triple_id)
                orphan_triple_ids = list(
                    (
                        await session.execute(
                            select(KnowledgeGraphTriple.triple_id).where(
                                KnowledgeGraphTriple.triple_id.in_(affected_triple_ids), ~has_mention
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if orphan_triple_ids:
                    await session.execute(
                        delete(KnowledgeGraphTriple).where(KnowledgeGraphTriple.triple_id.in_(orphan_triple_ids))
                    )
                surviving = [t for t in affected_triple_ids if t not in orphan_triple_ids]
                if surviving:
                    await session.execute(refresh_triple_support_statement(surviving))

            orphan_entity_ids: list[str] = []
            if affected_entity_ids:
                has_entity_mention = exists().where(
                    KnowledgeGraphEntityMention.entity_id == KnowledgeGraphEntity.entity_id
                )
                has_triple = exists().where(
                    or_(
                        KnowledgeGraphTriple.source_entity_id == KnowledgeGraphEntity.entity_id,
                        KnowledgeGraphTriple.target_entity_id == KnowledgeGraphEntity.entity_id,
                    )
                )
                orphan_entity_ids = list(
                    (
                        await session.execute(
                            select(KnowledgeGraphEntity.entity_id).where(
                                KnowledgeGraphEntity.entity_id.in_(affected_entity_ids),
                                ~has_entity_mention,
                                ~has_triple,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if orphan_entity_ids:
                    await session.execute(
                        delete(KnowledgeGraphEntity).where(KnowledgeGraphEntity.entity_id.in_(orphan_entity_ids))
                    )
        return {
            "affected_triple_ids": affected_triple_ids,
            "affected_entity_ids": affected_entity_ids,
            "orphan_triple_ids": orphan_triple_ids,
            "orphan_entity_ids": orphan_entity_ids,
        }

    # ── 门禁送审队列（D4）与冲突队列（D6）────────────────────────

    async def list_gate_reviews(
        self,
        kb_id: str,
        *,
        status: str = "PENDING",
        gate_code: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """门禁 REVIEW 路由队列：join chunk 原文与文件名（「点开即见原文」），按状态分页。"""
        async with pg_manager.get_async_session_context() as session:
            conditions = [KnowledgeGraphGateReview.kb_id == kb_id]
            if status and status != "ALL":
                conditions.append(KnowledgeGraphGateReview.status == status)
            if gate_code:
                conditions.append(KnowledgeGraphGateReview.gate_code == gate_code)
            total = await session.scalar(select(func.count()).select_from(KnowledgeGraphGateReview).where(*conditions))
            rows = (
                await session.execute(
                    select(
                        KnowledgeGraphGateReview,
                        KnowledgeChunk.content,
                        KnowledgeFile.filename,
                        KnowledgeFile.original_filename,
                    )
                    .join(KnowledgeChunk, KnowledgeChunk.chunk_id == KnowledgeGraphGateReview.chunk_id)
                    .join(KnowledgeFile, KnowledgeFile.file_id == KnowledgeGraphGateReview.file_id)
                    .where(*conditions)
                    .order_by(KnowledgeGraphGateReview.id.asc())
                    .limit(max(1, page_size))
                    .offset(max(0, (page - 1)) * max(1, page_size))
                )
            ).all()
            counts_rows = (
                await session.execute(
                    select(KnowledgeGraphGateReview.status, KnowledgeGraphGateReview.gate_code, func.count())
                    .where(KnowledgeGraphGateReview.kb_id == kb_id)
                    .group_by(KnowledgeGraphGateReview.status, KnowledgeGraphGateReview.gate_code)
                )
            ).all()
        grouped_counts: dict[str, dict[str, int]] = {}
        for row_status, code, count in counts_rows:
            bucket = grouped_counts.setdefault(row_status, {})
            bucket[code] = bucket.get(code, 0) + int(count)
            bucket["_total"] = bucket.get("_total", 0) + int(count)
        return {
            "items": [
                {
                    "review_id": row.review_id,
                    "gate_code": row.gate_code,
                    "gate_version": row.gate_version,
                    "status": row.status,
                    "candidate": row.candidate,
                    "chunk_id": row.chunk_id,
                    "file_id": row.file_id,
                    "filename": original_filename or filename,
                    "chunk_content": content,
                    "resolution": row.resolution,
                    "resolved_by": row.resolved_by,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
                }
                for row, content, filename, original_filename in rows
            ],
            "total": int(total or 0),
            "page": page,
            "page_size": page_size,
            "counts": grouped_counts,
        }

    async def get_gate_review(self, review_id: str) -> dict[str, Any] | None:
        async with pg_manager.get_async_session_context() as session:
            row = (
                await session.execute(
                    select(KnowledgeGraphGateReview).where(KnowledgeGraphGateReview.review_id == review_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "review_id": row.review_id,
                "kb_id": row.kb_id,
                "file_id": row.file_id,
                "chunk_id": row.chunk_id,
                "gate_code": row.gate_code,
                "status": row.status,
                "candidate": row.candidate,
                "resolution": row.resolution,
                "resolved_by": row.resolved_by,
            }

    async def resolve_gate_review_row(
        self, review_id: str, *, resolution: str, resolved_by: str
    ) -> dict[str, Any] | None:
        """幂等裁决：PENDING → RESOLVED；已 RESOLVED 同参返回 unchanged=True，不同参提示冲突。"""
        async with pg_manager.get_async_session_context() as session:
            row = (
                await session.execute(
                    select(KnowledgeGraphGateReview).where(KnowledgeGraphGateReview.review_id == review_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if row.status == "RESOLVED":
                return {
                    "review_id": review_id,
                    "status": "RESOLVED",
                    "unchanged": row.resolution == resolution,
                    "resolution": row.resolution,
                }
            row.status = "RESOLVED"
            row.resolution = resolution
            row.resolved_by = resolved_by
            row.resolved_at = utc_now()
            return {"review_id": review_id, "status": "RESOLVED", "unchanged": False, "resolution": resolution}

    async def list_conflicts(
        self,
        kb_id: str,
        *,
        kind: str | None = None,
        status: str = "OPEN",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """冲突队列：DIRECTION（极性矛盾）/ DEFINITION（定义区间口径不一），只登记不删证。"""
        async with pg_manager.get_async_session_context() as session:
            conditions = [KnowledgeGraphConflict.kb_id == kb_id]
            if status and status != "ALL":
                conditions.append(KnowledgeGraphConflict.status == status)
            if kind:
                conditions.append(KnowledgeGraphConflict.kind == kind)
            total = await session.scalar(select(func.count()).select_from(KnowledgeGraphConflict).where(*conditions))
            rows = (
                (
                    await session.execute(
                        select(KnowledgeGraphConflict)
                        .where(*conditions)
                        .order_by(KnowledgeGraphConflict.id.asc())
                        .limit(max(1, page_size))
                        .offset(max(0, (page - 1)) * max(1, page_size))
                    )
                )
                .scalars()
                .all()
            )
            counts_rows = (
                await session.execute(
                    select(KnowledgeGraphConflict.kind, KnowledgeGraphConflict.status, func.count())
                    .where(KnowledgeGraphConflict.kb_id == kb_id)
                    .group_by(KnowledgeGraphConflict.kind, KnowledgeGraphConflict.status)
                )
            ).all()
        grouped: dict[str, dict[str, int]] = {}
        for conflict_kind, conflict_status, count in counts_rows:
            bucket = grouped.setdefault(conflict_status, {})
            bucket[conflict_kind] = bucket.get(conflict_kind, 0) + int(count)
            bucket["_total"] = bucket.get("_total", 0) + int(count)
        return {
            "items": [
                {
                    "conflict_id": row.conflict_id,
                    "kind": row.kind,
                    "subject_ref": row.subject_ref,
                    "detail": row.detail,
                    "status": row.status,
                    "resolution": row.resolution,
                    "resolved_by": row.resolved_by,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
                }
                for row in rows
            ],
            "total": int(total or 0),
            "page": page,
            "page_size": page_size,
            "counts": grouped,
        }

    async def get_conflict(self, conflict_id: str) -> dict[str, Any] | None:
        async with pg_manager.get_async_session_context() as session:
            row = (
                await session.execute(
                    select(KnowledgeGraphConflict).where(KnowledgeGraphConflict.conflict_id == conflict_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "conflict_id": row.conflict_id,
                "kb_id": row.kb_id,
                "kind": row.kind,
                "subject_ref": row.subject_ref,
                "detail": row.detail,
                "status": row.status,
            }

    async def resolve_conflict_row(
        self, conflict_id: str, *, resolution: str, note: str | None, resolved_by: str
    ) -> dict[str, Any] | None:
        """冲突裁决（SUPERSEDED/CONTESTED/RECONCILED）：只改冲突行与备注，绝不触碰证据行。"""
        async with pg_manager.get_async_session_context() as session:
            row = (
                await session.execute(
                    select(KnowledgeGraphConflict).where(KnowledgeGraphConflict.conflict_id == conflict_id)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if row.status == "RESOLVED":
                return {
                    "conflict_id": conflict_id,
                    "status": "RESOLVED",
                    "unchanged": row.resolution == resolution,
                    "resolution": row.resolution,
                }
            before = {"status": row.status, "resolution": row.resolution}
            row.status = "RESOLVED"
            row.resolution = resolution
            row.resolved_by = resolved_by
            row.resolved_at = utc_now()
            if note:
                existing = row.detail if isinstance(row.detail, dict) else {}
                row.detail = {**existing, "resolution_note": note}
            return {
                "conflict_id": conflict_id,
                "status": "RESOLVED",
                "unchanged": False,
                "resolution": resolution,
                "before": before,
            }

    @staticmethod
    async def _get_decision_row(session, kb_id: str, target_kind: str, target_id: str):
        return (
            await session.execute(
                select(KnowledgeGraphReviewDecision).where(
                    KnowledgeGraphReviewDecision.kb_id == kb_id,
                    KnowledgeGraphReviewDecision.target_kind == target_kind,
                    KnowledgeGraphReviewDecision.target_id == target_id,
                )
            )
        ).scalar_one_or_none()


def _status_update(target_kind: str, target_ids: list[str], status: str, version: int | None):
    model, key = (
        (KnowledgeGraphTriple, KnowledgeGraphTriple.triple_id)
        if target_kind == KIND_TRIPLE
        else (
            KnowledgeGraphEntity,
            KnowledgeGraphEntity.entity_id,
        )
    )
    values: dict[str, Any] = {"review_status": status}
    if version is not None:
        values["review_version"] = version
    # CANONICAL 行永不被人工审核状态覆盖
    return update(model).where(key.in_(target_ids), model.review_status != STATUS_CANONICAL).values(**values)


def _mention_model(target_kind: str):
    if target_kind == KIND_TRIPLE:
        return KnowledgeGraphTripleMention, KnowledgeGraphTripleMention.triple_id
    if target_kind == KIND_ENTITY:
        return KnowledgeGraphEntityMention, KnowledgeGraphEntityMention.entity_id
    raise ValueError(f"未知审核对象类型: {target_kind}")


def _decision_dict(row: KnowledgeGraphReviewDecision) -> dict[str, Any]:
    return {
        "decision_id": row.decision_id,
        "kb_id": row.kb_id,
        "tenant_id": row.tenant_id,
        "target_kind": row.target_kind,
        "target_id": row.target_id,
        "action": row.action,
        "payload": row.payload,
        "pinned_chunk_id": row.pinned_chunk_id,
        "pinned_quote": row.pinned_quote,
        "reason": row.reason,
        "actor_uid": row.actor_uid,
        "version": int(row.version),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _entity_dict(row: KnowledgeGraphEntity) -> dict[str, Any]:
    return {
        "entity_id": row.entity_id,
        "kb_id": row.kb_id,
        "canonical_identity": row.canonical_identity,
        "normalized_name": row.normalized_name,
        "label": row.label,
        "name": row.name,
        "attributes": row.attributes,
        "review_status": row.review_status,
        "review_version": row.review_version,
    }


def _triple_dict(row: KnowledgeGraphTriple) -> dict[str, Any]:
    return {
        "triple_id": row.triple_id,
        "kb_id": row.kb_id,
        "source_entity_id": row.source_entity_id,
        "target_entity_id": row.target_entity_id,
        "relation_type": row.relation_type,
        "content": row.content,
        "support_count": row.support_count,
        "literature_count": row.literature_count,
        "review_status": row.review_status,
        "review_version": row.review_version,
    }
