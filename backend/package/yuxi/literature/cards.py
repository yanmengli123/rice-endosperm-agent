"""知识卡片与综述报告（G6；语义移植自 paperkb/cards.py + writing_tools 落盘语义）。

- 卡片四类（上游 CARD_TYPES）：card-translate / card-summary / card-qa / card-note；
  同 (paper, type, slug) 幂等覆盖（上游「同 slug 覆盖需显式」→ Yuxi upsert）。
- 综述报告：多文献 L1 笔记聚合生成（LLM 走 Yuxi 模型链）+ 手写落盘；
  产物为 literature_notes 派生产品（导航平面，永不进证据通道）。
"""

from __future__ import annotations

import re
from typing import Any

from yuxi.literature.access import paper_visibility
from yuxi.literature.access import report_visible

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteratureCard, LiteraturePaper, LiteratureReport

CARD_TYPES = ("card-translate", "card-summary", "card-qa", "card-note")

_SLUG_RE = re.compile(r"[^\w\-]+", re.UNICODE)


async def verify_report_sources(session, tenant_id: int, versions: list[dict]) -> None:
    from yuxi.literature.access import build_access, current_access

    scope = current_access()
    if scope is not None:
        fresh = await build_access(scope.user, tenant_id)
        if not {v["rid"] for v in versions} <= fresh.rids:
            raise ValueError("报告来源已更新或不可访问，请重新生成")
    rows = (
        (
            await session.execute(
                select(LiteraturePaper)
                .where(paper_visibility(tenant_id), LiteraturePaper.rid.in_([v["rid"] for v in versions]))
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    actual = {(p.rid, p.source_revision_id, p.reading_version) for p in rows}
    expected = {(v["rid"], v["parse_revision_id"], v["reading_version"]) for v in versions}
    if actual != expected:
        raise ValueError("报告来源已更新或不可访问，请重新生成")


def make_slug(text: str, max_len: int = 40) -> str:
    """标题/内容 → slug（上游 _slug 语义）。"""
    slug = _SLUG_RE.sub("-", (text or "").strip())[:max_len].strip("-")
    return slug or "card"


async def write_card(
    tenant_id: int,
    rid: str,
    *,
    card_type: str,
    content: str,
    title: str = "",
    para_ids: list[str] | None = None,
    tags: list[str] | None = None,
    slug: str | None = None,
) -> dict[str, Any]:
    """写一张卡片（upsert）。"""
    if card_type not in CARD_TYPES:
        raise ValueError(f"未知卡片类型: {card_type}（可选 {CARD_TYPES}）")
    if not (content or "").strip():
        raise ValueError("卡片内容不能为空")
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(paper_visibility(tenant_id), LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
        if paper is None:
            raise ValueError(f"文献不存在: {rid}")
        final_slug = make_slug(slug or title or content[:40])
        stmt = (
            pg_insert(LiteratureCard)
            .values(
                tenant_id=tenant_id,
                paper_id=paper.id,
                card_type=card_type,
                slug=final_slug,
                title=title or content[:80],
                content=content.strip(),
                para_ids_json=list(para_ids or []),
                tags_json=list(tags or []),
            )
            .on_conflict_do_update(
                index_elements=("tenant_id", "paper_id", "card_type", "slug"),
                set_={
                    "title": title or content[:80],
                    "content": content.strip(),
                    "para_ids_json": list(para_ids or []),
                    "tags_json": list(tags or []),
                },
            )
        )
        await session.execute(stmt)
        await session.commit()
    return {"rid": rid, "card_type": card_type, "slug": final_slug}


async def list_cards(tenant_id: int, rid: str, *, card_type: str | None = None) -> list[dict[str, Any]]:
    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(paper_visibility(tenant_id), LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
        if paper is None:
            return []
        conditions = [LiteratureCard.tenant_id == tenant_id, LiteratureCard.paper_id == paper.id]
        if card_type:
            conditions.append(LiteratureCard.card_type == card_type)
        rows = (await session.execute(select(LiteratureCard).where(*conditions))).scalars().all()
    return [
        {
            "card_type": row.card_type,
            "slug": row.slug,
            "title": row.title,
            "content": row.content,
            "para_ids": row.para_ids_json or [],
            "tags": row.tags_json or [],
        }
        for row in rows
    ]


async def generate_review_report(
    tenant_id: int,
    rids: list[str],
    *,
    title: str,
    created_by: str,
) -> dict[str, Any]:
    """综述生成：多篇文献的 L1 笔记聚合 → LLM 综述 → MinIO + 表（异步产物）。

    属 literature_notes 派生产品（导航平面）；零篇笔记时 fail-closed。
    """
    if not rids:
        raise ValueError("综述至少需要一篇文献")
    rids = list(dict.fromkeys(rids))[:20]
    async with pg_manager.get_async_session_context() as session:
        papers = (
            (
                await session.execute(
                    select(LiteraturePaper).where(paper_visibility(tenant_id), LiteraturePaper.rid.in_(rids))
                )
            )
            .scalars()
            .all()
        )
    if {p.rid for p in papers} != set(rids):
        raise ValueError("所选文献不存在或无权访问")
    minio = get_minio_client()
    notes: list[tuple[str, str]] = []
    for rid in rids[:20]:
        from yuxi.literature.readops import get_note_uri

        note_uri = await get_note_uri(tenant_id, rid, "L1")
        if not note_uri:
            continue
        try:
            data = await minio.adownload_file(RENDER_BUCKET, note_uri.removeprefix("kbasset://"))
            notes.append((rid, data.decode("utf-8")))
        except Exception:  # noqa: BLE001 - 缺笔记的文献跳过
            continue
    if not notes:
        raise ValueError("所选文献均无 L1 笔记（先编译）")
    versions = [
        {"rid": p.rid, "parse_revision_id": p.source_revision_id, "reading_version": p.reading_version}
        for p in papers
        if p.rid in {rid for rid, _ in notes}
    ]

    blocks = "\n\n".join(f"## {rid}\n{note}" for rid, note in notes)
    prompt = (
        "你是文献综述写作助手。基于以下多篇文献的结构化笔记（L1），"
        "写一篇中文综述报告（研究背景/方法对比/核心发现/研究缺口与展望四节，"
        "引用文献用「文献N（rid）」形式）。\n\n"
        f"{blocks}\n\n直接输出 Markdown 综述正文。"
    )
    from yuxi.literature.llm_gateway import complete_once

    response = await complete_once(
        tenant_id=tenant_id,
        uid=created_by,
        operation="literature_review",
        resource_ref="review:" + ",".join(rids[:20]),
        messages=[{"role": "user", "content": prompt}],
    )
    content = str(response.content or "").strip()
    if not content:
        raise ValueError("综述生成为空")

    report_id: int
    async with pg_manager.get_async_session_context() as session:
        await verify_report_sources(session, tenant_id, versions)
        report = LiteratureReport(
            tenant_id=tenant_id,
            report_kind="literature_review",
            title=title,
            content=content,
            created_by=created_by,
            source_rids_json=[rid for rid, _ in notes],
            source_versions_json=versions,
        )
        session.add(report)
        await session.flush()
        report_id = report.id
        await session.commit()
    content_uri = f"kbasset://tenants/{tenant_id}/literature/reports/{report_id}.md"
    await minio.aupload_file(
        bucket_name=RENDER_BUCKET,
        object_name=content_uri.removeprefix("kbasset://"),
        data=content.encode("utf-8"),
        content_type="text/markdown",
    )
    async with pg_manager.get_async_session_context() as session:
        report = (await session.execute(select(LiteratureReport).where(LiteratureReport.id == report_id))).scalar_one()
        report.content_uri = content_uri
        await session.commit()
    return {"report_id": report_id, "title": title, "content_uri": content_uri, "sources": len(notes)}


async def list_reports(tenant_id: int, *, limit: int = 50) -> list[dict[str, Any]]:
    async with pg_manager.get_async_session_context() as session:
        rows = (
            (
                await session.execute(
                    select(LiteratureReport)
                    .where(LiteratureReport.tenant_id == tenant_id)
                    .order_by(LiteratureReport.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "id": row.id,
            "report_kind": row.report_kind,
            "title": row.title,
            "content_uri": row.content_uri,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in [r for r in rows if report_visible(r)][:limit]
    ]
