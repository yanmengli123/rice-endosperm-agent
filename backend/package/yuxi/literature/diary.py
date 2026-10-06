"""阅读日记（G5；语义移植自 paperkb/diary.py）。

上游语义：日记是**用户主动创建**——写非空内容才建行，清空内容即删除该天
（不留空白日记）。日历视图按月聚合有日记的日期。
"""

from __future__ import annotations

import re
from datetime import date as date_type, UTC

from sqlalchemy import delete, select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteratureDiaryEntry
from yuxi.literature.access import build_access, current_access, paper_visibility
from yuxi.literature.provenance import current_job
from yuxi.storage.postgres.models_knowledge import LiteraturePaper, LiteratureCompileJob, LiteratureVariant

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_date(value: str) -> date_type:
    if not _DATE_RE.fullmatch(value or ""):
        raise ValueError(f"非法日期（需 YYYY-MM-DD）: {value!r}")
    try:
        return date_type.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"非法日期（需合法年月日）: {value!r}") from exc


async def write_diary(tenant_id: int, uid: str, entry_date: str, content: str) -> dict:
    """写某天日记；空内容 → 删除该天（上游 diary_note_write 语义）。"""
    day = _parse_date(entry_date)
    async with pg_manager.get_async_session_context() as session:
        if not (content or "").strip():
            await session.execute(
                delete(LiteratureDiaryEntry).where(
                    LiteratureDiaryEntry.tenant_id == tenant_id,
                    LiteratureDiaryEntry.uid == uid,
                    LiteratureDiaryEntry.entry_date == day,
                )
            )
            await session.commit()
            return {"date": entry_date, "deleted": True}
        stmt = pg_upsert_diary()
        await session.execute(stmt.values(tenant_id=tenant_id, uid=uid, entry_date=day, content=content.strip()))
        await session.commit()
    return {"date": entry_date, "deleted": False}


def pg_upsert_diary():
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    return pg_insert(LiteratureDiaryEntry).on_conflict_do_update(
        index_elements=("tenant_id", "uid", "entry_date"),
        set_={"content": pg_insert(LiteratureDiaryEntry).excluded.content, "updated_at": func_now()},
    )


def func_now():
    from sqlalchemy import func

    return func.now()


async def get_diary(tenant_id: int, uid: str, entry_date: str) -> dict:
    day = _parse_date(entry_date)
    async with pg_manager.get_async_session_context() as session:
        row = (
            await session.execute(
                select(LiteratureDiaryEntry).where(
                    LiteratureDiaryEntry.tenant_id == tenant_id,
                    LiteratureDiaryEntry.uid == uid,
                    LiteratureDiaryEntry.entry_date == day,
                )
            )
        ).scalar_one_or_none()
    return {"date": entry_date, "content": (row.content if row else "") or ""}


async def list_diary_days(tenant_id: int, uid: str, *, month: str | None = None) -> list[str]:
    """有日记的日期列表（month='YYYY-MM' 过滤，供月历圆点）。"""
    async with pg_manager.get_async_session_context() as session:
        rows = (
            await session.execute(
                select(LiteratureDiaryEntry.entry_date).where(
                    LiteratureDiaryEntry.tenant_id == tenant_id,
                    LiteratureDiaryEntry.uid == uid,
                )
            )
        ).scalars()
        days = sorted({d.isoformat() for d in rows})
    if month:
        days = [d for d in days if d.startswith(month)]
    return days


async def export_month(tenant_id: int, uid: str, month: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        raise ValueError("月份需为 YYYY-MM")
    start = _parse_date(month + "-01")
    end = date_type(start.year + (start.month == 12), start.month % 12 + 1, 1)
    async with pg_manager.get_async_session_context() as session:
        rows = (
            await session.execute(
                select(LiteratureDiaryEntry)
                .where(
                    LiteratureDiaryEntry.tenant_id == tenant_id,
                    LiteratureDiaryEntry.uid == uid,
                    LiteratureDiaryEntry.entry_date >= start,
                    LiteratureDiaryEntry.entry_date < end,
                )
                .order_by(LiteratureDiaryEntry.entry_date)
            )
        ).scalars()
        sections = [f"# 阅读日记 {month}", *[f"## {row.entry_date.isoformat()}\n\n{row.content}" for row in rows]]
    scope = current_access()
    if scope is not None:
        calendar = await activity_calendar(tenant_id, uid, month)
        for activity in calendar["activities"]:
            sections.append(f"## {activity['date']} 文献活动")
            sections.extend(f"- [[{paper['rid']}]] {paper['title']}" for paper in activity["papers"])
    return "\n\n".join(sections) + "\n"


async def activity_calendar(tenant_id: int, uid: str, month: str) -> dict:
    """Authorized imports/knowledge products and personal notes, UTC+8 days."""
    from datetime import timedelta, timezone

    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        raise ValueError("月份需为 YYYY-MM")
    _parse_date(month + "-01")
    scope = current_access()
    if scope is None or scope.tenant_id != tenant_id or scope.user.uid != uid:
        raise PermissionError("日记活动仅限当前用户")
    async with pg_manager.get_async_session_context() as db:
        papers = list((await db.execute(select(LiteraturePaper).where(paper_visibility(tenant_id)))).scalars())
        compiled = set(
            (
                await db.execute(
                    select(LiteratureCompileJob.paper_id)
                    .join(LiteraturePaper, LiteraturePaper.id == LiteratureCompileJob.paper_id)
                    .where(
                        paper_visibility(tenant_id),
                        LiteratureCompileJob.tenant_id == tenant_id,
                        current_job(LiteratureCompileJob),
                        LiteratureCompileJob.status == "SUCCEEDED",
                    )
                )
            ).scalars()
        )
        translated = set(
            (
                await db.execute(
                    select(LiteratureVariant.paper_id)
                    .join(LiteraturePaper, LiteraturePaper.id == LiteratureVariant.paper_id)
                    .where(
                        paper_visibility(tenant_id),
                        LiteratureVariant.tenant_id == tenant_id,
                        LiteratureVariant.variant_kind.in_(("zh", "en_zh")),
                        LiteratureVariant.parse_revision_id == LiteraturePaper.source_revision_id,
                        LiteratureVariant.reading_version == LiteraturePaper.reading_version,
                    )
                )
            ).scalars()
        )
    activities = {}
    for paper in papers:
        created = paper.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)  # SQLite only; production TIMESTAMPTZ is aware.
        day = created.astimezone(timezone(timedelta(hours=8))).date().isoformat()
        if not day.startswith(month):
            continue
        item = activities.setdefault(day, {"date": day, "imports": 0, "kb": 0, "papers": []})
        item["imports"] += 1
        item["kb"] += paper.id in compiled
        item["papers"].append(
            {
                "rid": paper.rid,
                "title": paper.title or paper.rid,
                "compiled": paper.id in compiled,
                "translated": paper.id in translated,
            }
        )
    days = set(await list_diary_days(tenant_id, uid, month=month)) | set(activities)
    fresh = await build_access(scope.user, tenant_id)
    if scope.rids != fresh.rids or scope.sources != fresh.sources:
        raise ValueError("日记来源权限已变更，请刷新")
    return {"days": sorted(days), "activities": [activities[day] for day in sorted(activities)]}
