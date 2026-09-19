"""图谱人工审核服务：验证 / 拒绝 / 批量 / 编辑（SUPERSEDE）/ 展示覆盖 / 手动补关系 / 单 chunk 重抽 / 队列 / 审计。

所有写操作 = 一条决策（按内容哈希身份，乐观并发 if_version）+ 一条 append-only 审计
+ 缓存状态列 + Neo4j/Milvus 投影同步。决策独立于图谱行，reset/重抽后由重放钩子恢复。
CANONICAL（托管导入）对象只读。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from yuxi.knowledge.graphs.extractors.llm_scientific import SCIENTIFIC_RELATION_TYPES
from yuxi.knowledge.graphs.graph_utils import compute_triple_id
from yuxi.knowledge.graphs.milvus_graph_service import (
    GRAPH_CONFIG_KEY,
    MilvusGraphService,
    normalized_result_from_snapshot,
)
from yuxi.knowledge.graphs.review_overlay import (
    ACTION_ALIAS_PROMOTE,
    ACTION_APPROVE,
    ACTION_REJECT,
    ACTION_RENAME,
    ACTION_RETYPE,
    ACTION_SUPERSEDE,
    KIND_ALIAS,
    KIND_ENTITY,
    KIND_TRIPLE,
    STATUS_APPROVED,
    STATUS_CANONICAL,
    STATUS_REJECTED,
    entity_snapshot,
    triple_snapshot,
)
from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository
from yuxi.repositories.knowledge_chunk_repository import KnowledgeChunkRepository
from yuxi.repositories.knowledge_graph_repository import KnowledgeGraphRepository
from yuxi.repositories.knowledge_graph_review_repository import (
    CONFLICT_RESOLVE,
    GATE_REVIEW_DISCARD,
    GATE_REVIEW_PROMOTE,
    KnowledgeGraphReviewRepository,
    ReviewConflictError,
    ReviewTargetReadOnlyError,
)
from yuxi.services.principal import resolve_tenant_id
from yuxi.storage.postgres.manager import pg_manager
from yuxi.utils import hashstr

AUDIT_ADD_RELATION = "ADD_RELATION"
AUDIT_REEXTRACT_CHUNK = "REEXTRACT_CHUNK"
AUDIT_ENTITY_REJECT_CASCADE = "REJECT_CASCADE"
KIND_CHUNK = "CHUNK"


class GraphReviewService:
    def __init__(
        self,
        *,
        graph_service: MilvusGraphService | None = None,
        review_repo: KnowledgeGraphReviewRepository | None = None,
        graph_repo: KnowledgeGraphRepository | None = None,
        chunk_repo: KnowledgeChunkRepository | None = None,
        kb_repo: KnowledgeBaseRepository | None = None,
    ):
        self.graph = graph_service or MilvusGraphService()
        self.review_repo = review_repo or KnowledgeGraphReviewRepository()
        self.graph_repo = graph_repo or KnowledgeGraphRepository()
        self.chunk_repo = chunk_repo or KnowledgeChunkRepository()
        self.kb_repo = kb_repo or KnowledgeBaseRepository()

    # ── 验证 / 拒绝 ──────────────────────────────────────────────

    async def approve(
        self,
        kb_id: str,
        target_kind: str,
        target_id: str,
        *,
        actor_uid: str,
        pinned_chunk_id: str | None = None,
        note: str | None = None,
        if_version: int | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """验证 = 记 APPROVE 决策并把审核人看着的那句原文 pin 住（重抽不删、重建可补回）。"""
        target, mentions, payload = await self._load_target(kb_id, target_kind, target_id)
        pinned = _pick_pinned_mention(mentions, pinned_chunk_id)
        if pinned is None:
            raise ValueError(f"{target_kind} {target_id} 没有可固定的原文引文，无法验证（先重抽或手动补引文）")
        existing = await self.review_repo.get_decision(kb_id, target_kind, target_id)
        if (
            existing
            and existing["action"] == ACTION_APPROVE
            and existing.get("pinned_chunk_id") == pinned["chunk_id"]
            and (if_version is None or int(existing["version"]) == int(if_version))
        ):
            return {"decision": existing, "unchanged": True}
        tenant_id = await self._tenant_id(actor_uid)
        decision = await self.review_repo.save_decision(
            kb_id=kb_id,
            tenant_id=tenant_id,
            target_kind=target_kind,
            target_id=target_id,
            action=ACTION_APPROVE,
            actor_uid=actor_uid,
            payload=payload,
            pinned_chunk_id=pinned["chunk_id"],
            pinned_quote=pinned["quote"],
            reason=note,
            if_version=if_version,
            cache_status=STATUS_APPROVED,
            before_snapshot={"review_status": target["review_status"], "review_version": target["review_version"]},
            after_snapshot={"review_status": STATUS_APPROVED, "pinned_chunk_id": pinned["chunk_id"]},
            batch_id=batch_id,
        )
        await self.review_repo.pin_mention(target_kind, target_id, pinned["chunk_id"], actor_uid)
        if target_kind == KIND_TRIPLE:
            for endpoint_id in (target["source_entity_id"], target["target_entity_id"]):
                await self.review_repo.pin_mention(KIND_ENTITY, endpoint_id, pinned["chunk_id"], actor_uid)
            await asyncio.to_thread(
                self.graph.apply_review_projection, kb_id, triple_status={target_id: STATUS_APPROVED}
            )
        return {"decision": decision, "unchanged": False}

    async def reject(
        self,
        kb_id: str,
        target_kind: str,
        target_id: str,
        *,
        actor_uid: str,
        reason: str,
        if_version: int | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        """拒绝 = REJECT 决策 + 缓存 REJECTED + 删 Neo4j 投影与 Milvus 向量；拒绝实体级联其全部三元组。"""
        if not (reason or "").strip():
            raise ValueError("拒绝必须填写理由")
        target, _mentions, payload = await self._load_target(kb_id, target_kind, target_id)
        cascaded: list[str] = list(target.get("triple_ids") or []) if target_kind == KIND_ENTITY else []
        tenant_id = await self._tenant_id(actor_uid)
        decision = await self.review_repo.save_decision(
            kb_id=kb_id,
            tenant_id=tenant_id,
            target_kind=target_kind,
            target_id=target_id,
            action=ACTION_REJECT,
            actor_uid=actor_uid,
            payload=payload,
            reason=reason,
            if_version=if_version,
            cache_status=STATUS_REJECTED,
            before_snapshot={"review_status": target["review_status"], "review_version": target["review_version"]},
            after_snapshot={"review_status": STATUS_REJECTED, "cascaded_triple_ids": cascaded},
            batch_id=batch_id,
        )
        if target_kind == KIND_TRIPLE:
            await asyncio.to_thread(self.graph.apply_review_projection, kb_id, reject_triple_ids=[target_id])
            await self.graph.graph_vector_store.delete_graph_records(kb_id, entity_ids=[], triple_ids=[target_id])
        else:
            if cascaded:
                await self.review_repo.set_review_status(KIND_TRIPLE, cascaded, STATUS_REJECTED)
            await asyncio.to_thread(self.graph.apply_review_projection, kb_id, reject_entity_ids=[target_id])
            await self.graph.graph_vector_store.delete_graph_records(kb_id, entity_ids=[target_id], triple_ids=cascaded)
        return {"decision": decision, "cascaded_triple_ids": cascaded}

    async def batch(
        self,
        kb_id: str,
        action: str,
        targets: list[dict[str, Any]],
        *,
        actor_uid: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """批量操作逐条执行：版本冲突/校验失败的条目跳过并返回明细，不因一条失败整批回滚。"""
        batch_id = uuid.uuid4().hex
        succeeded: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for target in targets:
            kind = str(target.get("kind") or KIND_TRIPLE)
            target_id = str(target.get("id") or "")
            try:
                if action == ACTION_APPROVE:
                    result = await self.approve(
                        kb_id,
                        kind,
                        target_id,
                        actor_uid=actor_uid,
                        pinned_chunk_id=target.get("pinned_chunk_id"),
                        note=reason,
                        if_version=target.get("if_version"),
                        batch_id=batch_id,
                    )
                elif action == ACTION_REJECT:
                    result = await self.reject(
                        kb_id,
                        kind,
                        target_id,
                        actor_uid=actor_uid,
                        reason=reason or "",
                        if_version=target.get("if_version"),
                        batch_id=batch_id,
                    )
                else:
                    raise ValueError(f"批量操作不支持 {action}")
                succeeded.append({"kind": kind, "id": target_id, "version": result["decision"]["version"]})
            except ReviewConflictError as exc:
                skipped.append({"kind": kind, "id": target_id, "reason": "version_conflict", "detail": str(exc)})
            except (ValueError, ReviewTargetReadOnlyError) as exc:
                skipped.append({"kind": kind, "id": target_id, "reason": "invalid", "detail": str(exc)})
        return {"batch_id": batch_id, "succeeded": succeeded, "skipped": skipped}

    # ── 编辑 ─────────────────────────────────────────────────────

    async def edit_triple(
        self,
        kb_id: str,
        triple_id: str,
        *,
        actor_uid: str,
        relation_type: str | None = None,
        reverse: bool = False,
        pinned_chunk_id: str | None = None,
        note: str | None = None,
        if_version: int | None = None,
    ) -> dict[str, Any]:
        """改谓词/翻转方向 = SUPERSEDE：旧 ID 自动 REJECTED(superseded→新 ID)，新 ID 以 manual 创建并 APPROVED。"""
        old, mentions, _payload = await self._load_target(kb_id, KIND_TRIPLE, triple_id)
        source, target = old["_source"], old["_target"]
        if reverse:
            source, target = target, source
        new_relation = (relation_type or old["relation_type"]).strip().upper()
        await self._validate_relation_type(kb_id, new_relation)
        new_triple_id = compute_triple_id(
            kb_id, source["normalized_name"], source["label"], new_relation, target["normalized_name"], target["label"]
        )
        if new_triple_id == triple_id:
            raise ValueError("未做任何修改：谓词与方向与原三元组一致")
        pinned = _pick_pinned_mention(mentions, pinned_chunk_id)
        if pinned is None:
            raise ValueError("原三元组没有原文引文，无法在保持可回验的前提下编辑")
        chunk = await self._load_chunk(kb_id, pinned["chunk_id"])
        if pinned["quote"] not in (chunk.content or ""):
            raise ValueError("引文与当前原文不一致（DEGRADED），请先重抽该段落再编辑")

        new_triple = {
            "triple_id": new_triple_id,
            "kb_id": kb_id,
            "source_entity_id": source["entity_id"],
            "target_entity_id": target["entity_id"],
            "relation_type": new_relation,
            "content": f"{source['normalized_name']} → {new_relation} → {target['normalized_name']}",
        }
        payload = triple_snapshot(new_triple, source, target)
        kb = await self.kb_repo.get_by_kb_id(kb_id)
        tenant_id = await self._tenant_id(actor_uid)
        await self.review_repo.save_decision(
            kb_id=kb_id,
            tenant_id=tenant_id,
            target_kind=KIND_TRIPLE,
            target_id=triple_id,
            action=ACTION_SUPERSEDE,
            actor_uid=actor_uid,
            payload={"new_triple_id": new_triple_id, **payload},
            reason=f"superseded→{new_triple_id}" + (f"：{note}" if note else ""),
            if_version=if_version,
            cache_status=STATUS_REJECTED,
            before_snapshot={
                "triple": {k: old.get(k) for k in ("relation_type", "source_entity_id", "target_entity_id")}
            },
            after_snapshot={"superseded_by": new_triple_id},
        )
        await self._materialize_snapshot(
            kb, chunk, payload, pinned["quote"], extractor_type="manual", actor_uid=actor_uid
        )
        new_decision = await self.review_repo.save_decision(
            kb_id=kb_id,
            tenant_id=tenant_id,
            target_kind=KIND_TRIPLE,
            target_id=new_triple_id,
            action=ACTION_APPROVE,
            actor_uid=actor_uid,
            payload=payload,
            pinned_chunk_id=pinned["chunk_id"],
            pinned_quote=pinned["quote"],
            reason=f"edited from {triple_id}" + (f"：{note}" if note else ""),
            cache_status=STATUS_APPROVED,
            before_snapshot=None,
            after_snapshot={"review_status": STATUS_APPROVED, "triple": new_triple},
        )
        await asyncio.to_thread(
            self.graph.apply_review_projection,
            kb_id,
            triple_status={new_triple_id: STATUS_APPROVED},
            reject_triple_ids=[triple_id],
        )
        await self.graph.graph_vector_store.delete_graph_records(kb_id, entity_ids=[], triple_ids=[triple_id])
        return {"old_triple_id": triple_id, "new_triple_id": new_triple_id, "decision": new_decision}

    async def edit_entity(
        self,
        kb_id: str,
        entity_id: str,
        *,
        actor_uid: str,
        display_name: str | None = None,
        aliases: list[str] | None = None,
        label: str | None = None,
        note: str | None = None,
        if_version: int | None = None,
    ) -> dict[str, Any]:
        """RENAME/RETYPE 只写展示覆盖（Neo4j display_name/label_override + 别名表），不改内容哈希身份。"""
        entity, _mentions, _payload = await self._load_target(kb_id, KIND_ENTITY, entity_id)
        overrides = {
            key: value
            for key, value in (("display_name", display_name), ("aliases", aliases), ("label", label))
            if value not in (None, "", [])
        }
        if not overrides:
            raise ValueError("未提供任何修改")
        action = ACTION_RENAME if "display_name" in overrides or "aliases" in overrides else ACTION_RETYPE
        tenant_id = await self._tenant_id(actor_uid)
        decision = await self.review_repo.save_decision(
            kb_id=kb_id,
            tenant_id=tenant_id,
            target_kind=KIND_ENTITY,
            target_id=entity_id,
            action=action,
            actor_uid=actor_uid,
            payload=overrides,
            reason=note,
            if_version=if_version,
            before_snapshot={"name": entity["name"], "label": entity["label"]},
            after_snapshot=overrides,
        )
        if overrides.get("aliases"):
            await self.review_repo.add_entity_aliases(kb_id, entity_id, entity["name"], overrides["aliases"])
        await asyncio.to_thread(self.graph.apply_review_projection, kb_id, entity_overrides={entity_id: overrides})
        return {"decision": decision}

    async def add_triple(
        self,
        kb_id: str,
        *,
        actor_uid: str,
        source_entity_id: str,
        target_entity_id: str,
        relation_type: str,
        chunk_id: str,
        evidence_quote: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """手动补关系：同一身份规则（LLM 之后抽到同一条会自然合并），引文必须 ⊆ chunk 原文（G2），直接 APPROVED。"""
        if source_entity_id == target_entity_id:
            raise ValueError("关系两端不能是同一个实体")
        source_loaded = await self.review_repo.get_entity(kb_id, source_entity_id)
        target_loaded = await self.review_repo.get_entity(kb_id, target_entity_id)
        if source_loaded is None or target_loaded is None:
            raise ValueError("关系端点实体不存在")
        source, target = source_loaded["entity"], target_loaded["entity"]
        relation = (relation_type or "").strip().upper()
        if not relation:
            raise ValueError("relation_type 不能为空")
        await self._validate_relation_type(kb_id, relation)
        quote = (evidence_quote or "").strip()
        chunk = await self._load_chunk(kb_id, chunk_id)
        if not quote or quote not in (chunk.content or ""):
            raise ValueError("evidence_quote 必须是该 chunk 原文的逐字子串")
        triple_id = compute_triple_id(
            kb_id, source["normalized_name"], source["label"], relation, target["normalized_name"], target["label"]
        )
        triple = {
            "triple_id": triple_id,
            "kb_id": kb_id,
            "source_entity_id": source["entity_id"],
            "target_entity_id": target["entity_id"],
            "relation_type": relation,
            "content": f"{source['normalized_name']} → {relation} → {target['normalized_name']}",
        }
        payload = triple_snapshot(triple, source, target)
        kb = await self.kb_repo.get_by_kb_id(kb_id)
        await self._materialize_snapshot(kb, chunk, payload, quote, extractor_type="manual", actor_uid=actor_uid)
        tenant_id = await self._tenant_id(actor_uid)
        decision = await self.review_repo.save_decision(
            kb_id=kb_id,
            tenant_id=tenant_id,
            target_kind=KIND_TRIPLE,
            target_id=triple_id,
            action=ACTION_APPROVE,
            actor_uid=actor_uid,
            payload=payload,
            pinned_chunk_id=chunk_id,
            pinned_quote=quote,
            reason=note,
            cache_status=STATUS_APPROVED,
            before_snapshot=None,
            after_snapshot={"review_status": STATUS_APPROVED, "triple": triple, "manual": True},
            audit_action=AUDIT_ADD_RELATION,
        )
        await asyncio.to_thread(self.graph.apply_review_projection, kb_id, triple_status={triple_id: STATUS_APPROVED})
        return {"triple_id": triple_id, "decision": decision}

    # ── 单 chunk 重抽 ───────────────────────────────────────────

    async def prepare_reextract(self, kb_id: str, chunk_id: str, *, actor_uid: str) -> dict[str, Any]:
        """删该 chunk 未 pinned 的 mention（孤儿三元组/实体一并清）、清 Neo4j/Milvus 投影、重置抽取缓存。

        pinned 证据保留（I3），已有决策由重抽写入后的重放钩子再次生效。调用方随后入队构建任务。
        """
        chunk = await self._load_chunk(kb_id, chunk_id)
        refs = await self.review_repo.delete_chunk_references(chunk_id, keep_pinned=True)
        await asyncio.to_thread(self.graph.delete_chunk_graph_from_neo4j, kb_id, chunk_id)
        if refs["orphan_entity_ids"] or refs["orphan_triple_ids"]:
            await self.graph.graph_vector_store.delete_graph_records(
                kb_id, entity_ids=refs["orphan_entity_ids"], triple_ids=refs["orphan_triple_ids"]
            )
        await self.chunk_repo.reset_graph_state_by_chunk_id(chunk_id)
        tenant_id = await self._tenant_id(actor_uid)
        await self.review_repo.append_audit(
            kb_id=kb_id,
            tenant_id=tenant_id,
            target_kind=KIND_CHUNK,
            target_id=chunk_id,
            action=AUDIT_REEXTRACT_CHUNK,
            actor_uid=actor_uid,
            after_snapshot={key: len(value) for key, value in refs.items()},
        )
        return {
            "chunk_id": chunk.chunk_id,
            "file_id": chunk.file_id,
            **{key: len(value) for key, value in refs.items()},
        }

    # ── 队列 / 审计 ─────────────────────────────────────────────

    async def queue(
        self,
        kb_id: str,
        *,
        status: str = "CANDIDATE",
        page: int = 1,
        page_size: int = 20,
        file_id: str | None = None,
        order: str = "support_asc",
    ) -> dict[str, Any]:
        listing = await self.review_repo.list_queue(
            kb_id, status=status, page=page, page_size=page_size, file_id=file_id, order=order
        )
        listing["counts"] = await self.review_repo.count_statuses(kb_id)
        return listing

    async def audit(self, kb_id: str, *, target_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        return await self.review_repo.list_audit(kb_id, target_id=target_id, limit=limit)

    # ── 门禁送审队列（D4）与冲突队列（D6）────────────────────────

    async def gate_reviews(
        self,
        kb_id: str,
        *,
        status: str = "PENDING",
        gate_code: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        return await self.review_repo.list_gate_reviews(
            kb_id, status=status, gate_code=gate_code, page=page, page_size=page_size
        )

    async def resolve_gate_review(
        self,
        kb_id: str,
        review_id: str,
        *,
        action: str,
        actor_uid: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """裁决门禁送审候选：PROMOTE 升格为 APPROVED 三元组（G2 逐字复核），DISCARD 关闭。

        PROMOTE 与手动补关系走同一受控通道（引文必须仍是 chunk 原文子串、闭集谓词校验、
        内容哈希身份、决策 + 审计 + Neo4j 投影），不另开旁门；幂等：已 RESOLVED 直接返回。
        """
        review = await self.review_repo.get_gate_review(review_id)
        if review is None or review["kb_id"] != kb_id:
            raise ValueError(f"门禁送审项 {review_id} 不存在")
        if review["status"] == "RESOLVED":
            return {"review_id": review_id, "unchanged": True, "resolution": review["resolution"]}

        if action not in ("PROMOTE", "DISCARD"):
            raise ValueError("action 必须是 PROMOTE 或 DISCARD")

        if action == "DISCARD":
            resolved = await self.review_repo.resolve_gate_review_row(
                review_id, resolution="DISCARD", resolved_by=actor_uid
            )
            await self.review_repo.append_audit(
                kb_id=kb_id,
                tenant_id=await self._tenant_id(actor_uid),
                target_kind="gate_review",
                target_id=review_id,
                action=GATE_REVIEW_DISCARD,
                actor_uid=actor_uid,
                before_snapshot={"gate_code": review["gate_code"], "candidate": review["candidate"]},
                after_snapshot={"resolution": "DISCARD"},
                reason=note,
            )
            return {"review_id": review_id, **resolved, "action": "DISCARD"}

        candidate = review["candidate"] or {}
        relation = str(candidate.get("predicate") or "").strip().upper()
        await self._validate_relation_type(kb_id, relation)
        quote = str(candidate.get("evidence_quote") or "").strip()
        chunk = await self._load_chunk(kb_id, review["chunk_id"])
        if not quote or quote not in (chunk.content or ""):
            raise ValueError("候选引文已不在 chunk 原文中（chunk 可能已重抽），请改用 DISCARD")
        subject = await self._entity_by_name(
            kb_id, str(candidate.get("subject") or ""), str(candidate.get("subject_label") or "")
        )
        target = await self._entity_by_name(
            kb_id, str(candidate.get("object") or ""), str(candidate.get("object_label") or "")
        )
        if subject is None or target is None:
            raise ValueError("端点实体不存在于图谱（实体候选可能已被拒绝），请改用 DISCARD 或先手动建实体")

        triple_id = compute_triple_id(
            kb_id, subject["normalized_name"], subject["label"], relation, target["normalized_name"], target["label"]
        )
        triple = {
            "triple_id": triple_id,
            "kb_id": kb_id,
            "source_entity_id": subject["entity_id"],
            "target_entity_id": target["entity_id"],
            "relation_type": relation,
            "content": f"{subject['normalized_name']} → {relation} → {target['normalized_name']}",
        }
        payload = triple_snapshot(triple, subject, target)
        kb = await self.kb_repo.get_by_kb_id(kb_id)
        await self._materialize_snapshot(kb, chunk, payload, quote, extractor_type="manual", actor_uid=actor_uid)
        await self.review_repo.save_decision(
            kb_id=kb_id,
            tenant_id=await self._tenant_id(actor_uid),
            target_kind=KIND_TRIPLE,
            target_id=triple_id,
            action=ACTION_APPROVE,
            actor_uid=actor_uid,
            payload=payload,
            pinned_chunk_id=review["chunk_id"],
            pinned_quote=quote,
            reason=note or f"gate review {review['gate_code']} PROMOTE",
            cache_status=STATUS_APPROVED,
            before_snapshot={"gate_code": review["gate_code"]},
            after_snapshot={"review_status": STATUS_APPROVED, "triple": triple, "gate_promoted": True},
            audit_action=GATE_REVIEW_PROMOTE,
        )
        await asyncio.to_thread(self.graph.apply_review_projection, kb_id, triple_status={triple_id: STATUS_APPROVED})
        resolved = await self.review_repo.resolve_gate_review_row(
            review_id, resolution=f"PROMOTED:{triple_id}", resolved_by=actor_uid
        )
        return {"review_id": review_id, "triple_id": triple_id, "action": "PROMOTE", **resolved}

    async def conflicts(
        self,
        kb_id: str,
        *,
        kind: str | None = None,
        status: str = "OPEN",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        return await self.review_repo.list_conflicts(kb_id, kind=kind, status=status, page=page, page_size=page_size)

    async def resolve_conflict(
        self,
        kb_id: str,
        conflict_id: str,
        *,
        resolution: str,
        actor_uid: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        """冲突裁决三态（SUPERSEDED/CONTESTED/RECONCILED）：只登记结论，绝不删除证据行。"""
        if resolution not in ("SUPERSEDED", "CONTESTED", "RECONCILED"):
            raise ValueError("resolution 必须是 SUPERSEDED / CONTESTED / RECONCILED 之一")
        conflict = await self.review_repo.get_conflict(conflict_id)
        if conflict is None or conflict["kb_id"] != kb_id:
            raise ValueError(f"冲突 {conflict_id} 不存在")
        if conflict["status"] == "RESOLVED":
            return {"conflict_id": conflict_id, "unchanged": True, "resolution": conflict["resolution"]}
        resolved = await self.review_repo.resolve_conflict_row(
            conflict_id, resolution=resolution, note=note, resolved_by=actor_uid
        )
        if resolved is None:
            raise ValueError(f"冲突 {conflict_id} 不存在")
        await self.review_repo.append_audit(
            kb_id=kb_id,
            tenant_id=await self._tenant_id(actor_uid),
            target_kind="conflict",
            target_id=conflict_id,
            action=CONFLICT_RESOLVE,
            actor_uid=actor_uid,
            before_snapshot={"kind": conflict["kind"], "detail": conflict["detail"]},
            after_snapshot={"resolution": resolution, "note": note},
            reason=note,
        )
        return {"conflict_id": conflict_id, **resolved}

    async def promote_alias(
        self,
        kb_id: str,
        *,
        surface: str,
        resolved_name: str,
        resolved_label: str,
        actor_uid: str,
        source_doclex_id: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """R4c 词典晋升：文档级别名（doclex 候选）经人工审核升为 KB 级官方别名。

        效果 v1 只落别名表（alias_type=DOCLEX_PROMOTED）——检索期 entity_resolver
        立即可用、可撤销、租户隔离；下次图谱构建时 ``_load_kb_alias_lexicon`` 把它
        装载进词法层（KB 作用域、构建结束回滚，digest 变化自动失效旧缓存）。
        幂等：同 (surface, resolved) 的同 action 决策直接返回 unchanged。
        """
        surface = (surface or "").strip()
        resolved_name = (resolved_name or "").strip()
        if not surface or not resolved_name:
            raise ValueError("surface 与 resolved_name 不能为空")
        target_id = hashstr(f"alias:{surface.lower()}:{resolved_name.lower()}", length=32)
        existing = await self.review_repo.get_decision(kb_id, KIND_ALIAS, target_id)
        if existing and existing.get("action") == ACTION_ALIAS_PROMOTE:
            return {"target_id": target_id, "unchanged": True}

        entity = await self._entity_by_name(kb_id, resolved_name, resolved_label)
        if entity is None:
            raise ValueError(f"规范实体 {resolved_name}（{resolved_label}）不存在，请先确保实体已入图")

        added = await self.review_repo.add_entity_aliases(
            kb_id, entity["entity_id"], entity["name"], [surface], alias_type="DOCLEX_PROMOTED"
        )
        decision = await self.review_repo.save_decision(
            kb_id=kb_id,
            tenant_id=await self._tenant_id(actor_uid),
            target_kind=KIND_ALIAS,
            target_id=target_id,
            action=ACTION_ALIAS_PROMOTE,
            actor_uid=actor_uid,
            payload={
                "surface": surface,
                "resolved_name": resolved_name,
                "resolved_label": resolved_label,
                "entity_id": entity["entity_id"],
                "source_doclex_id": source_doclex_id,
            },
            reason=note,
            audit_action=ACTION_ALIAS_PROMOTE,
        )
        return {"target_id": target_id, "entity_id": entity["entity_id"], "aliases_added": added, "decision": decision}

    async def _entity_by_name(self, kb_id: str, surface: str, label: str) -> dict[str, Any] | None:
        """按 (normalized_name, label) 查实体——门禁候选只带 surface，实体行由实体通道先行落库。"""
        from sqlalchemy import select as sa_select

        from yuxi.knowledge.graphs.graph_utils import normalize_entity_name
        from yuxi.storage.postgres.models_knowledge import KnowledgeGraphEntity

        normalized = normalize_entity_name(surface)
        if not normalized or not label:
            return None
        async with pg_manager.get_async_session_context() as session:
            row = (
                await session.execute(
                    sa_select(KnowledgeGraphEntity).where(
                        KnowledgeGraphEntity.kb_id == kb_id,
                        KnowledgeGraphEntity.normalized_name == normalized,
                        KnowledgeGraphEntity.label == label,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "entity_id": row.entity_id,
                "normalized_name": row.normalized_name,
                "label": row.label,
                "name": row.name,
                "attributes": row.attributes,
                "review_status": row.review_status,
            }

    # ── 内部 ────────────────────────────────────────────────────

    async def _load_target(self, kb_id: str, target_kind: str, target_id: str):
        """返回 (目标行, 其 mention 列表, 恢复快照 payload)；CANONICAL 只读。"""
        if target_kind == KIND_TRIPLE:
            loaded = await self.review_repo.get_triple_with_endpoints(kb_id, target_id)
            if loaded is None:
                raise ValueError(f"三元组 {target_id} 不存在")
            triple, source, target = loaded["triple"], loaded["source"], loaded["target"]
            if triple["review_status"] == STATUS_CANONICAL:
                raise ReviewTargetReadOnlyError("规范图谱（托管导入）三元组只读，不接受人工审核决策")
            if source is None or target is None:
                raise ValueError(f"三元组 {target_id} 端点实体缺失")
            return (
                {**triple, "_source": source, "_target": target},
                loaded["mentions"],
                triple_snapshot(triple, source, target),
            )
        if target_kind == KIND_ENTITY:
            loaded = await self.review_repo.get_entity(kb_id, target_id)
            if loaded is None:
                raise ValueError(f"实体 {target_id} 不存在")
            entity = loaded["entity"]
            if entity["review_status"] == STATUS_CANONICAL:
                raise ReviewTargetReadOnlyError("规范图谱（托管导入）实体只读，不接受人工审核决策")
            return (
                {**entity, "triple_ids": loaded["triple_ids"]},
                loaded["mentions"],
                {"entity": entity_snapshot(entity)},
            )
        raise ValueError(f"未知审核对象类型: {target_kind}")

    async def _load_chunk(self, kb_id: str, chunk_id: str):
        chunk = await self.chunk_repo.get_by_chunk_id(chunk_id)
        if chunk is None or chunk.kb_id != kb_id:
            raise ValueError(f"chunk {chunk_id} 不存在于知识库 {kb_id}")
        return chunk

    async def _validate_relation_type(self, kb_id: str, relation_type: str) -> None:
        """科研闭集库的谓词必须在白名单内；通用库只要求非空大写。"""
        kb = await self.kb_repo.get_by_kb_id(kb_id)
        config = (getattr(kb, "additional_params", None) or {}).get(GRAPH_CONFIG_KEY) or {}
        if (
            config.get("extractor_type") or ""
        ).lower() == "llm_scientific" and relation_type not in SCIENTIFIC_RELATION_TYPES:
            raise ValueError(f"谓词 {relation_type} 不在科研闭集词表内")

    async def _materialize_snapshot(self, kb, chunk, payload, quote, *, extractor_type, actor_uid) -> None:
        """按快照走与抽取相同的写入路径创建三元组（Neo4j → PG → Milvus），并 pin 引文。"""
        normalized = normalized_result_from_snapshot(payload, quote, extractor_type=extractor_type)
        entities, triples = await asyncio.to_thread(self.graph.write_chunk_graph, chunk.kb_id, chunk, normalized)
        await self.graph_repo.upsert_chunk_graph(
            kb_id=chunk.kb_id, file_id=chunk.file_id, chunk_id=chunk.chunk_id, entities=entities, triples=triples
        )
        await self.graph.graph_vector_store.insert_missing_graph_records(
            kb_id=chunk.kb_id, embedding_model_spec=kb.embedding_model_spec, entities=entities, triples=triples
        )
        for entity in entities:
            await self.review_repo.pin_mention(KIND_ENTITY, entity["entity_id"], chunk.chunk_id, actor_uid)
        for triple in triples:
            await self.review_repo.pin_mention(KIND_TRIPLE, triple["triple_id"], chunk.chunk_id, actor_uid)

    @staticmethod
    async def _tenant_id(actor_uid: str) -> int:
        async with pg_manager.get_async_session_context() as db:
            return await resolve_tenant_id(db, actor_uid)


def _pick_pinned_mention(mentions: list[dict[str, Any]], pinned_chunk_id: str | None) -> dict[str, Any] | None:
    """审核人指定的 chunk 优先；否则已 pinned 的；否则第一条有引文的。"""
    with_quote = [m for m in mentions if (m.get("quote") or "").strip()]
    if pinned_chunk_id:
        chosen = next((m for m in with_quote if m["chunk_id"] == pinned_chunk_id), None)
        if chosen is None:
            raise ValueError(f"chunk {pinned_chunk_id} 不是该对象的原文来源")
        return chosen
    pinned = next((m for m in with_quote if m.get("pinned_by")), None)
    return pinned or (with_quote[0] if with_quote else None)
