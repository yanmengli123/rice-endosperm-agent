"""Owner-scoped, versioned bibliometric topic runs. No model work in HTTP."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import uuid

from sqlalchemy import select

from yuxi.literature.access import current_access
from yuxi.literature.reference_batches import _DATABASE_FORMATS, record_conditions
from yuxi.literature.topic_contract import (
    MAX_DOCUMENTS,
    MODES,
    VERSION,
    corpus_digest,
    corpus_summary,
    prepare_document,
    validate_result,
)
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteraturePaper,
    LiteratureReferenceBatch,
    LiteratureReferenceRecord,
    LiteratureReferenceMember,
    LiteratureTopicDocument,
    LiteratureTopicRun,
)
from yuxi.utils.datetime_utils import utc_now

QUEUE = "arq:literature-topics"
BUCKET = "knowledgebases"
ACTIVE = ("PENDING", "RUNNING")


async def preview(tenant_id: int, *, mode: str = "papers", **filters) -> dict:
    documents = await snapshot(tenant_id, mode=mode, **filters)
    return {
        **corpus_summary(documents),
        "mode": mode,
        "input_digest": corpus_digest(documents),
        "schema_version": VERSION,
    }


async def snapshot(tenant_id: int, *, mode: str = "papers", **filters) -> list[dict]:
    uid = _owner(tenant_id)
    if mode not in MODES:
        raise ValueError("分析口径需为 papers 或 records")
    async with pg_manager.get_async_session_context() as db:
        condition = await record_conditions(db, tenant_id, **filters)
        papers = select(LiteraturePaper).where(condition)
        if mode == "papers":
            rows = (await db.execute(papers.order_by(LiteraturePaper.rid).limit(MAX_DOCUMENTS + 1))).scalars().all()
            documents = [
                prepare_document(
                    p.rid,
                    p.rid,
                    {
                        "title": p.title,
                        "abstract": p.abstract,
                        "year": p.pub_year,
                        "doi": p.doi,
                        "pmid": p.pmid,
                        "source": p.source_main,
                        "journal": p.journal,
                        "authors": p.authors_json or [],
                        "affiliations": p.affiliations_json or [],
                        "kind": p.kind,
                    },
                )
                for p in rows
                if p.title or p.abstract
            ]
        else:
            allowed = select(LiteraturePaper.rid).where(condition)
            batches = select(LiteratureReferenceBatch.batch_id).where(
                LiteratureReferenceBatch.tenant_id == tenant_id, LiteratureReferenceBatch.created_by == uid
            )
            if filters.get("batch_ids"):
                batches = batches.where(LiteratureReferenceBatch.batch_id.in_(filters["batch_ids"]))
            database = filters.get("database")
            if database in _DATABASE_FORMATS:
                batches = batches.where(LiteratureReferenceBatch.format.in_(_DATABASE_FORMATS[database]))
            elif database == "other":
                batches = batches.where(
                    LiteratureReferenceBatch.format.notin_(
                        [fmt for formats in _DATABASE_FORMATS.values() for fmt in formats]
                    )
                )
            batches = batches.where(
                LiteratureReferenceBatch.batch_id.in_(
                    select(LiteratureReferenceMember.batch_id).where(
                        LiteratureReferenceMember.tenant_id == tenant_id,
                        LiteratureReferenceMember.rid.in_(allowed),
                    )
                )
            )
            # Do not silently represent a legacy, deduplicated receipt as its original rows.
            recorded = select(LiteratureReferenceRecord.batch_id).where(
                LiteratureReferenceRecord.tenant_id == tenant_id
            )
            missing = (
                (await db.execute(batches.where(LiteratureReferenceBatch.batch_id.notin_(recorded)))).scalars().all()
            )
            if missing:
                raise ValueError("历史批次尚无原始行，请重新上传原文件补齐；不影响已有文献清单")
            rows = (
                await db.execute(
                    select(LiteratureReferenceRecord, LiteratureReferenceBatch.format)
                    .join(
                        LiteratureReferenceBatch,
                        LiteratureReferenceBatch.batch_id == LiteratureReferenceRecord.batch_id,
                    )
                    .where(
                        LiteratureReferenceRecord.tenant_id == tenant_id,
                        LiteratureReferenceRecord.batch_id.in_(batches),
                        LiteratureReferenceRecord.rid.in_(allowed),
                    )
                    .order_by(LiteratureReferenceRecord.batch_id, LiteratureReferenceRecord.record_no)
                    .limit(MAX_DOCUMENTS + 1)
                )
            ).all()
            documents = [
                prepare_document(f"{r.batch_id}:{r.record_no}", r.rid, {**r.payload_json, "source": fmt})
                for r, fmt in rows
                if r.payload_json.get("title") or r.payload_json.get("abstract")
            ]
    if len(rows) > MAX_DOCUMENTS or len(documents) > MAX_DOCUMENTS:
        raise ValueError("主题语料超过 50000 条安全预算，请缩小分析范围")
    return documents


async def start_run(tenant_id: int, options: dict, filters: dict, *, reuse_run_id: str | None = None) -> dict:
    uid = _owner(tenant_id)
    mode = options.get("mode", "papers")
    if reuse_run_id:
        base, documents = await authorized_run(tenant_id, reuse_run_id, full=True)
        mode = base.mode
        config = {**base.config_json, **options, "allow_remote": False}
        if not config.get("embedding_objects"):
            raise ValueError("该历史结果不含嵌入矩阵，不能离线重新拟合")
    else:
        documents = await snapshot(tenant_id, mode=mode, **filters)
        config = {**options, "filters": filters, "schema_version": VERSION}
    if len(documents) < 10:
        raise ValueError("有效语料少于 10 条，不适合此聚类配置")
    digest = corpus_digest(documents)
    if options.get("expected_digest") and options["expected_digest"] != digest:
        raise ValueError("预览后文献或筛选范围已变更，请重新预览")
    if not reuse_run_id and not config.get("allow_remote"):
        raise ValueError("新语料需要嵌入：请导入离线结果，或显式允许调用已配置模型")
    if config.get("allow_remote"):
        from yuxi.models.providers.cache import model_cache

        info = model_cache.get_model_info(config.get("embedding_spec", ""))
        if (
            info is None
            or info.model_type != "embedding"
            or info.model_id != "BAAI/bge-m3"
            or int(info.dimension or 0) != 1024
        ):
            raise ValueError("联合主题分析需选择已配置的 BAAI/bge-m3 embedding 模型")
        if config.get("chinese_comparison"):
            chinese = model_cache.get_model_info(config.get("chinese_embedding_spec", ""))
            if (
                chinese is None
                or chinese.model_type != "embedding"
                or chinese.model_id != "BAAI/bge-large-zh-v1.5"
                or int(chinese.dimension or 0) != 1024
            ):
                raise ValueError("中文对照需选择已配置的 1024 维 BAAI/bge-large-zh-v1.5 模型")
    if config.get("chinese_comparison") and sum(d["language_of_text"] == "zh" for d in documents) < 10:
        raise ValueError("中文子集少于 10 条，请关闭中文对照或增加语料；未调用供应商")
    run_id = uuid.uuid4().hex
    async with pg_manager.get_async_session_context() as db:
        if db.get_bind().dialect.name == "postgresql":
            from sqlalchemy import text

            await db.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"topic:{tenant_id}:{uid}"}
            )
        active = (
            (
                await db.execute(
                    select(LiteratureTopicRun)
                    .where(
                        LiteratureTopicRun.tenant_id == tenant_id,
                        LiteratureTopicRun.created_by == uid,
                        LiteratureTopicRun.status.in_(ACTIVE),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for run in active:
            if run.input_digest == digest and run.config_json == config:
                return _run_dto(run)
        if len(active) >= 2:
            raise ValueError("已有两个主题任务待处理，请完成或取消后再提交")
        run = LiteratureTopicRun(
            run_id=run_id,
            tenant_id=tenant_id,
            created_by=uid,
            name=options.get("name") or "文献主题分析",
            status="PENDING",
            mode=mode,
            input_digest=digest,
            config_json=config,
            progress_json=corpus_summary(documents),
            attempts=0,
            cancel_requested=False,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        db.add(run)
        await db.flush()
        await _save_documents(db, run, documents)
        await db.commit()
    await dispatch(run_id)
    return _run_dto(run)


async def list_runs(tenant_id: int, *, offset: int = 0, limit: int = 25) -> dict:
    uid = _owner(tenant_id)
    async with pg_manager.get_async_session_context() as db:
        rows = (
            (
                await db.execute(
                    select(LiteratureTopicRun)
                    .where(LiteratureTopicRun.tenant_id == tenant_id, LiteratureTopicRun.created_by == uid)
                    .order_by(LiteratureTopicRun.created_at.desc(), LiteratureTopicRun.run_id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        ids = [row.run_id for row in rows[:limit]]
        pairs = (
            await db.execute(
                select(LiteratureTopicDocument.run_id, LiteratureTopicDocument.rid)
                .where(LiteratureTopicDocument.run_id.in_(ids))
                .distinct()
            )
        ).all()
    visible = current_access().rids
    blocked = {run_id for run_id, rid in pairs if rid not in visible}
    items = [_run_dto(row) for row in rows[:limit] if row.run_id not in blocked]
    return {"items": items, "has_more": len(rows) > limit, "next_offset": offset + min(limit, len(rows))}


async def authorized_run(tenant_id: int, run_id: str, *, full: bool = False) -> tuple[LiteratureTopicRun, list[dict]]:
    uid = _owner(tenant_id)
    async with pg_manager.get_async_session_context() as db:
        run = await db.get(LiteratureTopicRun, run_id)
    if run is None or run.tenant_id != tenant_id or run.created_by != uid:
        raise PermissionError("分析不存在或无权访问")
    documents = await load_documents(run_id, fields=None if full else ("document_key", "rid", "language_of_text"))
    if not documents or any(d["rid"] not in current_access().rids for d in documents):
        raise PermissionError("分析来源权限已变更，拒绝读取历史分析")
    return run, documents


async def get_run(tenant_id: int, run_id: str) -> dict:
    run, documents = await authorized_run(tenant_id, run_id)
    data = _run_dto(run)
    if run.result_object:
        result = await read_result(run, documents)
        data["models"] = {
            key: {k: v for k, v in value.items() if k != "assignments"} for key, value in result["models"].items()
        }
        data["method"] = result.get("method", {})
    return data


async def result_view(
    tenant_id: int,
    run_id: str,
    *,
    model: str,
    topic: int | None = None,
    offset: int = 0,
    limit: int = 25,
    view: str = "documents",
) -> dict:
    run, documents = await authorized_run(tenant_id, run_id)
    result = await read_result(run, documents)
    if model not in result["models"]:
        raise ValueError("模型结果不存在")
    value = result["models"][model]
    if view == "charts":
        metadata_documents = await load_documents(
            run_id,
            fields=(
                "document_key",
                "rid",
                "title",
                "year",
                "source",
                "language_of_text",
                "journal",
                "kind",
                "affiliations",
                "source_fields",
            ),
        )
        return {
            **value,
            "assignments": value["assignments"],
            "metadata": {
                d["document_key"]: {
                    "rid": d["rid"],
                    "title": d["title"],
                    "year": d.get("year"),
                    "source": d.get("source"),
                    "language": d["language_of_text"],
                    "journal": d.get("journal"),
                    "kind": d.get("kind", "paper"),
                    "affiliations": d.get("affiliations") or [],
                    "countries": _countries(d.get("affiliations") or []),
                    "clc": (d.get("source_fields") or {}).get("clc-中图分类号") or (d.get("source_fields") or {}).get("中图分类号"),
                }
                for d in metadata_documents
            },
        }
    rows = [a for a in value["assignments"] if topic is None or a["topic"] == topic]
    page = rows[offset : offset + limit]
    lookup = {d["document_key"]: d for d in await load_documents(run_id, keys=[a["document_key"] for a in page])}
    return {"total": len(rows), "items": [{**lookup[a["document_key"]], **a} for a in page]}


async def cancel_run(tenant_id: int, run_id: str) -> dict:
    await authorized_run(tenant_id, run_id)
    async with pg_manager.get_async_session_context() as db:
        run = await db.get(LiteratureTopicRun, run_id, with_for_update=True)
        if run.status in ACTIVE:
            run.cancel_requested = True
            if run.status == "PENDING":
                run.status = "CANCELLED"
            run.updated_at = utc_now()
        await db.commit()
    return _run_dto(run)


async def export_run(tenant_id: int, run_id: str, *, model: str) -> bytes:
    run, documents = await authorized_run(tenant_id, run_id)
    result = await read_result(run, documents)
    if model not in result["models"]:
        raise ValueError("模型结果不存在")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "run_id",
            "model",
            "mode",
            "document_key",
            "rid",
            "title",
            "year",
            "source",
            "topic",
            "membership",
            "centroid_cosine",
        ]
    )
    lookup = {
        d["document_key"]: d
        for d in await load_documents(run_id, fields=("document_key", "rid", "title", "year", "source"))
    }
    for a in result["models"][model]["assignments"]:
        d = lookup[a["document_key"]]
        values = [
            run_id,
            model,
            run.mode,
            a["document_key"],
            d["rid"],
            d["title"],
            d.get("year"),
            d.get("source"),
            a["topic"],
            a["membership"],
            a["centroid_cosine"],
        ]
        # Spreadsheet consumers must not execute titles as formulas.
        writer.writerow(
            ["'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v for v in values]
        )
    return ("\ufeff" + output.getvalue()).encode()


async def dispatch(run_id: str, *, attempt: int = 0) -> bool:
    from redis.exceptions import RedisError
    from yuxi.services.run_queue_service import get_arq_pool

    try:
        queue = await get_arq_pool()
        await queue.enqueue_job("process_topic_run", run_id, _queue_name=QUEUE, _job_id=f"topic:{run_id}:{attempt}")
        return True
    except (OSError, RuntimeError, RedisError) as exc:
        logging.getLogger(__name__).warning("Topic enqueue deferred (%s)", type(exc).__name__)
        return False  # The persistent PENDING row is retried by the dedicated reconciler.


async def load_documents(run_id: str, *, fields: tuple | None = None, keys: list[str] | None = None) -> list[dict]:
    if fields is not None:
        columns = [
            getattr(LiteratureTopicDocument, name)
            if name in {"document_key", "rid"}
            else LiteratureTopicDocument.payload_json[name].as_json()
            for name in fields
        ]
        async with pg_manager.get_async_session_context() as db:
            values = (
                await db.execute(
                    select(*columns)
                    .where(LiteratureTopicDocument.run_id == run_id)
                    .order_by(LiteratureTopicDocument.ordinal)
                )
            ).all()
        return [dict(zip(fields, value, strict=True)) for value in values]
    async with pg_manager.get_async_session_context() as db:
        rows = (
            (
                await db.execute(
                    select(LiteratureTopicDocument.payload_json)
                    .where(LiteratureTopicDocument.run_id == run_id)
                    .where(True if keys is None else LiteratureTopicDocument.document_key.in_(keys))
                    .order_by(LiteratureTopicDocument.ordinal)
                )
            )
            .scalars()
            .all()
        )
    return rows


async def read_result(run: LiteratureTopicRun, documents: list[dict]) -> dict:
    if not run.result_object:
        raise ValueError("分析尚未完成")
    expected = f"tenants/{run.tenant_id}/literature/topic-runs/{run.run_id}/"
    if not run.result_object.startswith(expected):
        raise ValueError("分析产物路径与运行归属不一致")
    raw = await get_minio_client().adownload_file(BUCKET, run.result_object)
    if len(raw) > 64 * 1024 * 1024:
        raise ValueError("主题结果超过安全预算")
    return await asyncio.to_thread(lambda: validate_result(json.loads(raw), documents))


async def publish_result(
    run: LiteratureTopicRun, documents: list[dict], result: dict, *, lease_token: str | None = None
) -> None:
    validate_result(result, documents)
    name = f"tenants/{run.tenant_id}/literature/topic-runs/{run.run_id}/{uuid.uuid4().hex}/result.json"
    raw = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
    await get_minio_client().aupload_file(BUCKET, name, raw, "application/json")
    if await get_minio_client().adownload_file(BUCKET, name) != raw:
        raise ValueError("主题结果落盘校验失败")
    async with pg_manager.get_async_session_context() as db:
        current = await db.get(LiteratureTopicRun, run.run_id, with_for_update=True)
        if current.cancel_requested or (lease_token and current.lease_token != lease_token):
            raise asyncio.CancelledError("主题任务已取消或租约失效")
        current.result_object, current.status = name, "SUCCEEDED"
        current.progress_json = {**current.progress_json, "stage": "complete", "completed": len(documents)}
        current.updated_at = utc_now()
        await db.commit()


async def _save_documents(db, run: LiteratureTopicRun, documents: list[dict]) -> None:
    from sqlalchemy import insert

    for start in range(0, len(documents), 500):
        await db.execute(
            insert(LiteratureTopicDocument),
            [
                {
                    "tenant_id": run.tenant_id,
                    "run_id": run.run_id,
                    "document_key": d["document_key"],
                    "rid": d["rid"],
                    "ordinal": start + i,
                    "text_sha256": d["text_sha256"],
                    "payload_json": d,
                }
                for i, d in enumerate(documents[start : start + 500])
            ],
        )


def _owner(tenant_id: int) -> str:
    scope = current_access()
    if scope is None or scope.tenant_id != tenant_id or not getattr(scope.user, "uid", None):
        raise PermissionError("主题分析需要当前用户访问上下文")
    return scope.user.uid


def _countries(affiliations):
    from yuxi.literature.bibliometrics import _affiliation_country

    return sorted({country for text in affiliations if (country := _affiliation_country(text))})


def _run_dto(run: LiteratureTopicRun) -> dict:
    config = {
        k: v for k, v in run.config_json.items()
        if k not in {"credential_snapshot", "embedding_objects", "tokenizer_objects"}
    }
    return {
        "run_id": run.run_id,
        "name": run.name,
        "mode": run.mode,
        "status": run.status,
        "input_digest": run.input_digest,
        "config": config,
        "progress": run.progress_json,
        "error": run.error,
        "attempts": run.attempts,
        "created_at": run.created_at.isoformat(),
        "has_embeddings": bool(run.config_json.get("embedding_objects")),
    }
