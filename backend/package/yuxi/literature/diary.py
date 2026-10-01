"""阅读日记（G5；语义移植自 paperkb/diary.py）。

上游语义：日记是**用户主动创建**——写非空内容才建行，清空内容即删除该天
（不留空白日记）。日历视图按月聚合有日记的日期。
"""

from __future__ import annotations

import re
from datetime import date as date_type

from sqlalchemy import delete, select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteratureDiaryEntry

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
