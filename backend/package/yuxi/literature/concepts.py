"""概念页聚合与主题 MOC（G 补集；语义移植自 paperkb 概念惰性聚合 + topic 编译）。

上游语义：L1 编译产出 concepts [{name, definition}]；概念在 **≥N 篇文献出现**
时惰性聚合成概念页（N=2 起步，上游 3+ 为含定义门槛，此处 2+ 聚合、3+ 标
mature）。数据来源两级：``notes/{rid}/L1_data.json``（编译时落库的结构化
concepts，优先）→ ``L1_note.md`` 的「## 核心概念」段解析（兜底，兼容历史产物）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, timedelta
from typing import Any

from sqlalchemy import select

from yuxi.literature.access import paper_visibility
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteraturePaper
from yuxi.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)

#: 概念聚合门槛：≥MIN Papers 出现才成页；≥MATURE 标记成熟（对齐上游 3+ 门槛）
MIN_PAPERS = 2
MATURE_PAPERS = 3

_CONCEPT_LINE_RE = re.compile(r"^-\s+\*\*(.+?)\*\*[：:]\s*(.+)$")


def _parse_concepts_from_note(markdown: str) -> list[dict[str, str]]:
    """从 L1 笔记 markdown 的「## 核心概念」段解析（兜底路径）。"""
    concepts: list[dict[str, str]] = []
    in_section = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = "核心概念" in stripped
            continue
        if in_section:
            match = _CONCEPT_LINE_RE.match(stripped)
            if match:
                concepts.append({"name": match.group(1).strip(), "definition": match.group(2).strip()})
    return concepts


async def _concepts_for_paper(minio, tenant_id: int, rid: str) -> list[dict[str, str]]:
    from yuxi.literature.readops import get_note_uri

    note_uri = await get_note_uri(tenant_id, rid, "L1")
    if not note_uri:
        return []
    note_object = note_uri.removeprefix("kbasset://")
    for object_name in (note_object.rsplit("/", 1)[0] + "/L1_data.json",):
        try:
            data = json.loads((await minio.adownload_file(RENDER_BUCKET, object_name)).decode("utf-8"))
            concepts = data.get("concepts") or []
            if concepts:
                return [
                    {"name": str(c.get("name") or ""), "definition": str(c.get("definition") or "")}
                    for c in concepts
                    if isinstance(c, dict) and c.get("name")
                ]
        except Exception:  # noqa: BLE001 - 缺 L1_data 走 markdown 兜底
            continue
    try:
        note = (await minio.adownload_file(RENDER_BUCKET, note_object)).decode("utf-8")
        return _parse_concepts_from_note(note)
    except Exception:  # noqa: BLE001 - 无 L1 笔记
        return []


async def aggregate_concepts(tenant_id: int) -> list[dict[str, Any]]:
    """聚合租户内概念页：[{concept, definition, papers, mature}]（≥MIN_PAPERS）。"""
    async with pg_manager.get_async_session_context() as session:
        papers = (await session.execute(select(LiteraturePaper).where(paper_visibility(tenant_id)))).scalars().all()
    minio = get_minio_client()
    by_concept: dict[str, dict[str, Any]] = {}
    for paper in papers:
        for concept in await _concepts_for_paper(minio, tenant_id, paper.rid):
            name = concept["name"]
            entry = by_concept.setdefault(
                name, {"concept": name, "definition": concept["definition"], "papers": [], "titles": {}}
            )
            entry["papers"].append(paper.rid)
            entry["titles"][paper.rid] = paper.title or paper.rid
            if not entry["definition"] and concept["definition"]:
                entry["definition"] = concept["definition"]
    result = []
    for entry in by_concept.values():
        if len(entry["papers"]) < MIN_PAPERS:
            continue
        result.append(
            {
                "concept": entry["concept"],
                "definition": entry["definition"],
                "papers": sorted(set(entry["papers"])),
                "paper_count": len(set(entry["papers"])),
                "mature": len(set(entry["papers"])) >= MATURE_PAPERS,
            }
        )
    result.sort(key=lambda item: (-item["paper_count"], item["concept"].lower()))
    return result


async def generate_topic_moc(
    tenant_id: int,
    *,
    topic: str,
    created_by: str = "",
) -> dict[str, Any]:
    """主题 MOC（Map of Content）：按主题匹配概念/标题筛文献 → L1 聚合 → LLM 成文。

    落 literature_reports（report_kind="topic_moc"，literature_notes 派生产品）。
    零匹配 fail-closed（提示先编译或换主题词）。
    """
    if not (topic or "").strip():
        raise ValueError("主题不能为空")
    keyword = topic.strip().lower()
    async with pg_manager.get_async_session_context() as session:
        papers = (await session.execute(select(LiteraturePaper).where(paper_visibility(tenant_id)))).scalars().all()
    minio = get_minio_client()
    matched: list[tuple[str, str]] = []  # (rid, L1 note)
    concepts = await aggregate_concepts(tenant_id)
    concept_hit_rids = {
        rid
        for concept in concepts
        if keyword in concept["concept"].lower() or keyword in concept["definition"].lower()
        for rid in concept["papers"]
    }
    for paper in papers:
        title_hit = keyword in (paper.title or "").lower()
        if paper.rid in concept_hit_rids or title_hit:
            from yuxi.literature.readops import get_note_uri

            note_uri = await get_note_uri(tenant_id, paper.rid, "L1")
            if not note_uri:
                continue
            try:
                note = (await minio.adownload_file(RENDER_BUCKET, note_uri.removeprefix("kbasset://"))).decode("utf-8")
                matched.append((paper.rid, note))
            except Exception:  # noqa: BLE001 - 无笔记跳过
                continue
    if not matched:
        raise ValueError(f"主题「{topic}」零匹配（无概念/标题命中，或相关文献未编译 L1）")
    matched = matched[:20]
    versions = [
        {"rid": p.rid, "parse_revision_id": p.source_revision_id, "reading_version": p.reading_version}
        for p in papers
        if p.rid in {rid for rid, _ in matched}
    ]

    blocks = "\n\n".join(f"## {rid}\n{note}" for rid, note in matched[:20])
    prompt = (
        f"你是文献主题导读（MOC）写作助手。主题：{topic}。基于以下文献的结构化笔记，"
        "写一篇主题导读（该主题是什么/代表性工作与结论对比/方法谱系/研究缺口，"
        "引用文献用「文献N（rid）」形式）。\n\n"
        f"{blocks}\n\n直接输出 Markdown。"
    )
    from yuxi.literature.llm_gateway import complete_once

    response = await complete_once(
        tenant_id=tenant_id,
        uid=created_by,
        operation="topic_moc",
        resource_ref=f"topic:{topic}",
        messages=[{"role": "user", "content": prompt}],
    )
    content = str(response.content or "").strip()
    if not content:
        raise ValueError("主题 MOC 生成为空")

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from yuxi.storage.postgres.models_knowledge import LiteratureReport

    async with pg_manager.get_async_session_context() as session:
        from yuxi.literature.cards import verify_report_sources

        await verify_report_sources(session, tenant_id, versions)
        stmt = (
            pg_insert(LiteratureReport)
            .values(
                tenant_id=tenant_id,
                report_kind="topic_moc",
                title=f"主题导读：{topic}",
                content=content,
                created_by=created_by,
                source_rids_json=[rid for rid, _ in matched],
                source_versions_json=versions,
            )
            .returning(LiteratureReport.id)
        )
        report_id = (await session.execute(stmt)).scalar_one()
        await session.commit()
    content_uri = f"kbasset://tenants/{tenant_id}/literature/reports/{report_id}.md"
    await minio.aupload_file(
        bucket_name=RENDER_BUCKET,
        object_name=content_uri.removeprefix("kbasset://"),
        data=content.encode("utf-8"),
        content_type="text/markdown",
    )
    async with pg_manager.get_async_session_context() as session:
        await session.execute(
            LiteratureReport.__table__.update().where(LiteratureReport.id == report_id).values(content_uri=content_uri)
        )
        await session.commit()
    return {"report_id": report_id, "topic": topic, "sources": len(matched), "content_uri": content_uri}


# ---------------------------------------------------------------------------
# 主题导读（MOC）异步任务：HTTP 只入队，ARQ worker 执行，租约 + 对账兜底。
# 同步长任务在 2 万篇库上会超过前端 30s 请求超时，且非幂等 POST 命中
# 半开 keep-alive 连接时整个请求无声丢失，故必须走 202 + 任务模式。
# ---------------------------------------------------------------------------

REPORT_JOB_ACTIVE = ("PENDING", "RUNNING")
REPORT_JOB_TERMINAL = ("SUCCEEDED", "FAILED", "DEAD")


async def _dispatch_report_job(job_id: str) -> bool:
    from yuxi.services.run_queue_service import get_arq_pool

    try:
        queue = await get_arq_pool()
        slot = int(utc_now().timestamp()) // 60
        job = await queue.enqueue_job(
            "process_literature_topic_moc", job_id, _job_id=f"lit-report:{job_id}:{slot}"
        )
        return job is not None
    except Exception:  # noqa: BLE001 - 对账 cron 会补派
        logger.warning("主题导读任务 %s 等待队列调度补偿", job_id)
        return False


async def enqueue_topic_moc(tenant_id: int, *, topic: str, created_by: str) -> dict[str, Any]:
    """接受任务并立即返回；同请求者同主题的活跃任务幂等复用。"""
    import uuid as _uuid

    if not (topic or "").strip():
        raise ValueError("主题不能为空")
    keyword = topic.strip()
    if len(keyword) > 256:
        raise ValueError("主题过长（≤256 字符）")
    async with pg_manager.get_async_session_context() as session:
        from yuxi.storage.postgres.models_business import User
        from yuxi.storage.postgres.models_knowledge import LiteratureReportJob

        # 按请求者串行化，防止并发双击重复建任务。
        await session.execute(select(User.uid).where(User.uid == created_by).with_for_update())
        existing = (
            await session.execute(
                select(LiteratureReportJob).where(
                    LiteratureReportJob.tenant_id == tenant_id,
                    LiteratureReportJob.requested_by == created_by,
                    LiteratureReportJob.topic == keyword,
                    LiteratureReportJob.status.in_(REPORT_JOB_ACTIVE),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return {"job_id": existing.job_id, "status": existing.status, "reused": True}
        job_id = f"lrj_{_uuid.uuid4().hex}"
        session.add(
            LiteratureReportJob(
                job_id=job_id,
                tenant_id=tenant_id,
                requested_by=created_by,
                report_kind="topic_moc",
                topic=keyword,
                status="PENDING",
            )
        )
        await session.commit()
    return {
        "job_id": job_id,
        "status": "PENDING",
        "reused": False,
        "enqueue_deferred": not await _dispatch_report_job(job_id),
    }


async def get_topic_moc_job(tenant_id: int, job_id: str) -> dict[str, Any] | None:
    """状态查询仅限本租户；requested_by 校验由调用方（端点）执行。"""
    from yuxi.storage.postgres.models_knowledge import LiteratureReportJob

    async with pg_manager.get_async_session_context() as session:
        row = (
            await session.execute(
                select(LiteratureReportJob).where(
                    LiteratureReportJob.tenant_id == tenant_id,
                    LiteratureReportJob.job_id == job_id,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    lease = row.lease_expires_at
    if lease is not None and lease.tzinfo is None:
        lease = lease.replace(tzinfo=UTC)
    return {
        "job_id": row.job_id,
        "status": row.status,
        "topic": row.topic,
        "attempt": row.attempt,
        "error": row.error,
        "result": row.result_json or {},
        "requested_by": row.requested_by,
        "lease_alive": bool(row.lease_token and lease and lease > utc_now()),
    }


async def process_literature_topic_moc(ctx: dict, job_id: str) -> dict | None:
    import uuid as _uuid

    from yuxi.storage.postgres.models_knowledge import LiteratureReportJob

    # 幂等护栏：已完成任务直接返回，不重复成文、不重复计费。
    async with pg_manager.get_async_session_context() as session:
        row = (
            await session.execute(
                select(LiteratureReportJob).where(LiteratureReportJob.job_id == job_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        if row.result_json:
            return {"job_id": job_id, "status": row.status, "skipped": "already_completed"}
        if row.status not in REPORT_JOB_ACTIVE:
            return None

    token = _uuid.uuid4().hex
    async with pg_manager.get_async_session_context() as session:
        claimed = (
            await session.execute(
                select(LiteratureReportJob)
                .where(LiteratureReportJob.job_id == job_id, LiteratureReportJob.status.in_(REPORT_JOB_ACTIVE))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if claimed is None:
            return None
        claimed.status, claimed.attempt = "RUNNING", claimed.attempt + 1
        claimed.lease_token, claimed.lease_expires_at = token, utc_now() + timedelta(minutes=15)
        tenant_id, requested_by, topic = int(claimed.tenant_id), claimed.requested_by, claimed.topic
        await session.commit()
    try:
        from yuxi.literature.workflow import _request_user

        user_holder = _JobUser(tenant_id, requested_by)
        await _request_user(user_holder)  # 请求者存在性/租户一致性校验
        result = await generate_topic_moc(tenant_id, topic=topic, created_by=requested_by)
        async with pg_manager.get_async_session_context() as session:
            row = (
                await session.execute(
                    select(LiteratureReportJob).where(LiteratureReportJob.job_id == job_id).with_for_update()
                )
            ).scalar_one()
            if row.lease_token != token:
                raise ValueError("主题导读任务租约已失效")
            row.status, row.result_json, row.error = "SUCCEEDED", result, None
            row.lease_token, row.lease_expires_at = None, None
            await session.commit()
        return {"job_id": job_id, "status": "SUCCEEDED", "result": result}
    except Exception as exc:  # noqa: BLE001 - 失败留档，不自动重试付费调用
        logger.warning("主题导读任务失败 %s (%s)", job_id, type(exc).__name__)
        async with pg_manager.get_async_session_context() as session:
            row = (
                await session.execute(
                    select(LiteratureReportJob).where(LiteratureReportJob.job_id == job_id).with_for_update()
                )
            ).scalar_one()
            if row.lease_token == token:
                row.status, row.error = "FAILED", str(exc)[:2000]
                row.lease_token, row.lease_expires_at = None, None
                await session.commit()
        return {"job_id": job_id, "status": "FAILED", "error": str(exc)[:500]}


class _JobUser:
    """_request_user(run) 只读取 run.requested_by / run.tenant_id 两个字段。"""

    def __init__(self, tenant_id: int, requested_by: str) -> None:
        self.tenant_id, self.requested_by = tenant_id, requested_by


async def reconcile_literature_topic_mocs(ctx: dict | None = None) -> int:
    """PENDING 补派（入队失败兜底）；RUNNING 租约超 30 分钟不自动重跑（防重复成文），置 DEAD 留档。"""
    from yuxi.storage.postgres.models_knowledge import LiteratureReportJob

    redispatched = 0
    async with pg_manager.get_async_session_context() as session:
        rows = list(
            (
                await session.execute(
                    select(LiteratureReportJob)
                    .where(LiteratureReportJob.status.in_(REPORT_JOB_ACTIVE))
                    .order_by(LiteratureReportJob.updated_at)
                    .limit(100)
                )
            ).scalars()
        )
        for row in rows:
            if row.status == "PENDING":
                redispatched += int(await _dispatch_report_job(row.job_id))
                continue
            expires = row.lease_expires_at
            if expires is not None and expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if row.lease_token and expires and expires < utc_now() - timedelta(minutes=30):
                row.status, row.error = "DEAD", "执行租约失效且未恢复（防止重复成文不自动重跑）"
                row.lease_token, row.lease_expires_at = None, None
        await session.commit()
    return redispatched
