"""Independent Milvus navigation snapshots with PostgreSQL manifests/outbox.

No KnowledgeBase adapter and no evidence fields. ACL/current input filtering is
performed before vector search; unavailable channels are explicitly reported.
Embeddings are built once, not recomputed over keyword hits during queries.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import timedelta

from sqlalchemy import select

from yuxi.literature.access import access_scope, current_access, paper_visibility
from yuxi.literature.provenance import current_job
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteratureAttachment,
    LiteratureCompileJob,
    LiteratureNavigationIndex,
    LiteraturePaper,
)
from yuxi.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)
BUILD_JOB = "process_literature_navigation_index"
MAX_ATTEMPTS = 3
MAX_CORPUS_PAPERS = 20_000
MAX_PRODUCT_BYTES = 5 * 1024 * 1024
MAX_NAVIGATION_CHUNKS = 20_000


def _fingerprint(paper) -> str:
    payload = [
        paper.rid,
        paper.title,
        paper.abstract,
        paper.journal,
        paper.pub_year,
        paper.doi,
        paper.source_revision_id,
        paper.reading_version,
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def _visible_inputs(tenant_id: int):
    async with pg_manager.get_async_session_context() as session:
        papers = list(
            (
                await session.execute(
                    select(LiteraturePaper)
                    .where(paper_visibility(tenant_id))
                    .order_by(LiteraturePaper.id)
                    .limit(MAX_CORPUS_PAPERS + 1)
                )
            ).scalars()
        )
        if len(papers) > MAX_CORPUS_PAPERS:
            raise ValueError("导航索引超过单批上限，请拆分知识源构建")
        jobs = (
            (
                await session.execute(
                    select(LiteratureCompileJob)
                    .join(LiteraturePaper, LiteraturePaper.id == LiteratureCompileJob.paper_id)
                    .where(
                        paper_visibility(tenant_id),
                        LiteratureCompileJob.tenant_id == tenant_id,
                        current_job(LiteratureCompileJob),
                        LiteratureCompileJob.level.in_(("L1", "L2", "L3")),
                        LiteratureCompileJob.status == "SUCCEEDED",
                        LiteratureCompileJob.note_uri.isnot(None),
                    )
                    .order_by(LiteratureCompileJob.updated_at.desc(), LiteratureCompileJob.id.desc())
                )
            )
            .scalars()
            .all()
        )
        attachments = list(
            (
                await session.execute(
                    select(LiteratureAttachment)
                    .join(LiteraturePaper, LiteraturePaper.id == LiteratureAttachment.paper_id)
                    .where(
                        paper_visibility(tenant_id),
                        LiteratureAttachment.tenant_id == tenant_id,
                        LiteratureAttachment.text_uri.isnot(None),
                        LiteratureAttachment.text_digest.isnot(None),
                        LiteratureAttachment.text_error.is_(None),
                    )
                )
            ).scalars()
        )
    notes = {}
    for job in jobs:
        notes.setdefault(job.paper_id, {}).setdefault(job.level, {"uri": job.note_uri, "job_id": job.id})
    manifest = {
        str(p.id): {
            "rid": p.rid,
            "fingerprint": _fingerprint(p),
            "note_uri": notes.get(p.id, {}).get("L1", {}).get("uri"),
            "note_job_id": notes.get(p.id, {}).get("L1", {}).get("job_id"),
            "notes": notes.get(p.id, {}),
            "attachments": [
                {"id": a.id, "uri": a.text_uri, "digest": a.text_digest, "source_digest": a.content_digest}
                for a in attachments
                if a.paper_id == p.id
            ],
        }
        for p in papers
    }
    return papers, manifest


async def _dispatch(index_id: str, attempt: int = 0) -> bool:
    from yuxi.services.run_queue_service import get_arq_pool

    try:
        queue = await get_arq_pool()
        slot = int(utc_now().timestamp()) // 60
        await queue.enqueue_job(BUILD_JOB, index_id, _job_id=f"lit-nav:{index_id}:{attempt}:{slot}")
        return True
    except Exception:  # noqa: BLE001 - persistent PENDING is reconciled
        logger.warning("导航索引 %s 已持久化，等待调度补偿", index_id)
        return False


async def enqueue_navigation_index(tenant_id: int, *, requested_by: str, model_spec: str = "") -> dict:
    from yuxi.config import config

    scope = current_access()
    if scope is None or scope.tenant_id != tenant_id or scope.user.uid != requested_by:
        raise ValueError("导航索引构建必须在请求者来源权限范围内执行")
    papers, manifest = await _visible_inputs(tenant_id)
    if not papers:
        raise ValueError("当前权限范围内没有可索引文献")
    model_spec = model_spec or config.embed_model
    if not model_spec:
        raise ValueError("未配置 Embedding 模型")
    async with pg_manager.get_async_session_context() as session:
        from yuxi.literature.llm_gateway import freeze_literature_policy
        from yuxi.storage.postgres.models_business import User

        # Serialize the actor's identical-corpus submissions before reuse or
        # quota policy creation. Cross-actor snapshots retain their own policy.
        await session.execute(select(User.uid).where(User.uid == requested_by).with_for_update())
        # A user can reuse only their own exact corpus and model generation.
        rows = list(
            (
                await session.execute(
                    select(LiteratureNavigationIndex)
                    .where(
                        LiteratureNavigationIndex.tenant_id == tenant_id,
                        LiteratureNavigationIndex.requested_by == requested_by,
                        LiteratureNavigationIndex.model_spec == model_spec,
                        LiteratureNavigationIndex.status.in_(("PENDING", "RUNNING", "SUCCEEDED")),
                    )
                    .order_by(LiteratureNavigationIndex.created_at.desc())
                    .limit(10)
                )
            ).scalars()
        )
        existing = next((row for row in rows if row.input_manifest == manifest and row.llm_run_id), None)
        if existing:
            return {"index_id": existing.index_id, "status": existing.status, "reused": True}
        index_id = f"lni_{uuid.uuid4().hex}"
        policy = await freeze_literature_policy(
            session,
            tenant_id=tenant_id,
            uid=requested_by,
            operation="navigation_build",
            resource_ref=index_id,
            model_spec=model_spec,
            model_type="embedding",
        )
        row = LiteratureNavigationIndex(
            index_id=index_id,
            tenant_id=tenant_id,
            requested_by=requested_by,
            model_spec=model_spec,
            llm_run_id=policy.run_id,
            credential_source=policy.credential_source,
            credential_id=policy.credential_id,
            provider_id=policy.provider_id,
            policy_version=policy.policy_version,
            collection_name=f"literature_nav_{tenant_id}_{index_id[4:]}",
            status="PENDING",
            input_manifest=manifest,
        )
        session.add(row)
        await session.commit()
    queued = await _dispatch(index_id)
    return {"index_id": index_id, "status": "PENDING", "reused": False, "enqueue_deferred": not queued}


def _milvus_client():
    from pymilvus import MilvusClient

    # The literature_nav_ collection namespace is independent of evidence collections.
    return MilvusClient(
        uri=os.getenv("MILVUS_URI") or "http://milvus:19530", token=os.getenv("MILVUS_TOKEN") or "", timeout=30
    )


def _write_collection(name: str, documents: list[dict], vectors: list[list[float]]) -> None:
    client = _milvus_client()
    try:
        dimension = len(vectors[0])
        if not dimension or len(vectors) != len(documents) or any(len(vector) != dimension for vector in vectors):
            raise ValueError("Embedding 数量或维度不一致")
        if not client.has_collection(collection_name=name):
            client.create_collection(
                collection_name=name, dimension=dimension, metric_type="COSINE", consistency_level="Strong"
            )
        for start in range(0, len(documents), 100):
            rows = [
                {"id": i + 1, "vector": vectors[i], **documents[i]}
                for i in range(start, min(start + 100, len(documents)))
            ]
            client.upsert(collection_name=name, data=rows)
        client.flush(collection_name=name)
        client.load_collection(collection_name=name)
    finally:
        client.close()


async def process_literature_navigation_index(ctx: dict, index_id: str):
    async with pg_manager.get_async_session_context() as session:
        row = (
            await session.execute(
                select(LiteratureNavigationIndex)
                .where(LiteratureNavigationIndex.index_id == index_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or row.status != "PENDING":
            return None
        row.status, row.updated_at = "RUNNING", utc_now()
        attempt = row.attempt
        row.collection_name = f"literature_nav_{row.tenant_id}_{index_id[4:]}_a{attempt}"
        await session.commit()
    try:
        from yuxi.literature import embedding_gateway
        from yuxi.literature.llm_gateway import policy_for_job_attempt
        from yuxi.literature.reading.service import RENDER_BUCKET
        from yuxi.literature.workflow import _request_user
        from yuxi.storage.minio import get_minio_client

        user = await _request_user(row)
        async with access_scope(user, int(row.tenant_id)):
            papers, current = await _visible_inputs(row.tenant_id)
            current = {pid: item for pid, item in current.items() if pid in row.input_manifest}
            papers = [paper for paper in papers if str(paper.id) in row.input_manifest]
            if current != row.input_manifest:
                raise ValueError("索引输入或来源权限已变更，请重新构建")
            documents, texts = [], []
            for paper in papers:
                meta = f"{paper.title or ''}\n{paper.abstract or ''}\n{paper.journal or ''}\n{paper.doi or ''}"
                if meta.strip():
                    documents.append(
                        {"paper_id": int(paper.id), "rid": paper.rid, "channel": "catalog", "excerpt": meta[:1200]}
                    )
                    texts.append(meta[:6000])
                products = [{"kind": level, **note} for level, note in current[str(paper.id)]["notes"].items()] + [
                    {"kind": "attachment", **attachment} for attachment in current[str(paper.id)]["attachments"]
                ]
                for product in products:
                    data = await get_minio_client().adownload_file(
                        RENDER_BUCKET, product["uri"].removeprefix("kbasset://")
                    )
                    if len(data) > MAX_PRODUCT_BYTES:
                        raise ValueError("导航产物超过 5 MiB，请拆分后重建；未截断或激活不完整索引")
                    if product["kind"] == "attachment" and hashlib.sha256(data).hexdigest() != product["digest"]:
                        raise ValueError("附件导航文本完整性校验失败")
                    note = data.decode("utf-8")
                    # Bounded chunks retain semantic recall beyond the opening paragraph.
                    for start in range(0, len(note), 2000):
                        text = note[start : start + 2400]
                        if text.strip():
                            documents.append(
                                {
                                    "paper_id": int(paper.id),
                                    "rid": paper.rid,
                                    "channel": "compiled",
                                    "product_kind": product["kind"],
                                    "excerpt": text[:1200],
                                }
                            )
                            texts.append(text)
                            if len(documents) > MAX_NAVIGATION_CHUNKS:
                                raise ValueError("导航分块超过单批预算，请拆分后重建；未调用 Embedding")
            if not documents:
                raise ValueError("没有可索引文本")
            policy = await policy_for_job_attempt(row, operation="navigation_build")
            vectors = await embedding_gateway.encode(policy, texts)
            await asyncio.to_thread(_write_collection, row.collection_name, documents, vectors)
        # Recheck both versions and ACL after the external model/storage calls.
        async with access_scope(user, int(row.tenant_id)):
            _, current = await _visible_inputs(row.tenant_id)
            current = {pid: item for pid, item in current.items() if pid in row.input_manifest}
            if current != row.input_manifest:
                raise ValueError("构建期间输入或来源权限变更；未激活索引")
        async with pg_manager.get_async_session_context() as session:
            fresh = (
                await session.execute(
                    select(LiteratureNavigationIndex)
                    .where(LiteratureNavigationIndex.index_id == index_id)
                    .with_for_update()
                )
            ).scalar_one()
            if fresh.attempt != attempt or fresh.status != "RUNNING":
                return {"index_id": index_id, "status": "STALE"}
            fresh.status, fresh.error, fresh.document_count = "SUCCEEDED", None, len(documents)
            await session.commit()
        return {"index_id": index_id, "status": "SUCCEEDED", "documents": len(documents)}
    except Exception as exc:  # noqa: BLE001 - bounded retry, old snapshot stays readable
        logger.warning("导航索引构建失败: %s (%s)", index_id, type(exc).__name__)
        async with pg_manager.get_async_session_context() as session:
            fresh = (
                await session.execute(
                    select(LiteratureNavigationIndex)
                    .where(LiteratureNavigationIndex.index_id == index_id)
                    .with_for_update()
                )
            ).scalar_one()
            if fresh.attempt != attempt or fresh.status != "RUNNING":
                return {"index_id": index_id, "status": "STALE"}
            fresh.attempt += 1
            fresh.status = "DEAD" if fresh.attempt >= MAX_ATTEMPTS else "PENDING"
            # Provider errors can contain request URLs and credentials.
            fresh.error = f"{type(exc).__name__}: 导航构建失败，请检查模型和存储服务"
            await session.commit()
        return {"index_id": index_id, "status": fresh.status}


def _vector_search(name: str, vector: list[float], paper_ids: list[int], channel: str, limit: int):
    client = _milvus_client()
    try:
        ids = ",".join(str(pid) for pid in paper_ids)
        return client.search(
            collection_name=name,
            data=[vector],
            filter=f'paper_id in [{ids}] and channel == "{channel}"',
            limit=min(limit * 5, 250),
            output_fields=["rid", "paper_id", "excerpt"],
            search_params={"metric_type": "COSINE"},
        )[0]
    finally:
        client.close()


async def search_navigation_vectors(
    tenant_id: int, query: str, *, channel: str, limit: int = 10
) -> tuple[list[dict], str]:
    if channel not in {"catalog", "compiled"}:
        raise ValueError("无效导航通道")
    try:
        _, current = await _visible_inputs(tenant_id)
        async with pg_manager.get_async_session_context() as session:
            indexes = list(
                (
                    await session.execute(
                        select(LiteratureNavigationIndex)
                        .where(
                            LiteratureNavigationIndex.tenant_id == tenant_id,
                            LiteratureNavigationIndex.status == "SUCCEEDED",
                        )
                        .order_by(LiteratureNavigationIndex.created_at.desc())
                        .limit(20)
                    )
                ).scalars()
            )
        chosen, eligible = None, []
        for index in indexes:
            ids = [
                int(pid)
                for pid, item in current.items()
                if index.input_manifest.get(pid) == item
                and (channel != "compiled" or item.get("notes") or item.get("attachments"))
            ]
            if ids:
                chosen, eligible = index, ids
                break
        if chosen is None:
            return [], "关键词通道：无当前权限/版本对应的导航向量索引"
        from yuxi.literature.embedding_gateway import encode_query

        vectors = await encode_query(tenant_id, chosen.model_spec, query)
        raw = await asyncio.to_thread(_vector_search, chosen.collection_name, vectors[0], eligible, channel, limit)
        # A permission/reading revision can change during the remote calls.
        from yuxi.literature.access import build_access

        scope = current_access()
        if scope is not None:
            fresh_access = await build_access(scope.user, tenant_id)
        else:
            fresh_access = None
        _, latest = await _visible_inputs(tenant_id)
        hits, seen = [], set()
        for entry in raw:
            entity = entry["entity"]
            rid = entity["rid"]
            pid = str(entity["paper_id"])
            if (
                pid not in current
                or current[pid]["rid"] != rid
                or rid in seen
                or latest.get(pid) != current.get(pid)
                or (fresh_access is not None and rid not in fresh_access.rids)
            ):
                continue
            seen.add(rid)
            hits.append({"rid": rid, "excerpt": str(entity.get("excerpt") or ""), "score": float(entry["distance"])})
        expected = {
            int(pid)
            for pid, item in current.items()
            if channel != "compiled" or item.get("notes") or item.get("attachments")
        }
        coverage_note = "" if set(eligible) == expected else "向量索引仅覆盖部分当前文献；请重建导航索引"
        return hits[:limit], coverage_note
    except Exception as exc:  # noqa: BLE001 - explicitly degraded, no fake vector channel
        logger.warning("文献导航向量不可用 (%s)", type(exc).__name__)
        return [], "关键词通道：导航向量索引或模型暂不可用"


async def reconcile_literature_navigation_indexes(ctx: dict | None = None) -> int:
    async with pg_manager.get_async_session_context() as session:
        stale = list(
            (
                await session.execute(
                    select(LiteratureNavigationIndex)
                    .where(
                        LiteratureNavigationIndex.status == "RUNNING",
                        LiteratureNavigationIndex.updated_at < utc_now() - timedelta(minutes=30),
                    )
                    .with_for_update()
                )
            ).scalars()
        )
        for row in stale:
            row.attempt += 1
            row.status = "DEAD" if row.attempt >= MAX_ATTEMPTS else "PENDING"
        await session.commit()
        pending = list(
            (
                await session.execute(
                    select(LiteratureNavigationIndex).where(LiteratureNavigationIndex.status == "PENDING").limit(100)
                )
            ).scalars()
        )
    queued = 0
    for row in pending:
        queued += int(await _dispatch(row.index_id, row.attempt))
    return queued
