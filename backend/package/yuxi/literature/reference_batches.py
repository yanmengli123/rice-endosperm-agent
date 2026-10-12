"""上传清单回执、去重成员与分析范围。HTTP 鉴权与当前来源 ACL 每次重查。"""

from __future__ import annotations

from sqlalchemy import Text, and_, cast, func, literal, or_, select

from yuxi.literature.access import current_access, paper_visibility
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteraturePaper, LiteratureReferenceBatch, LiteratureReferenceMember

_DATABASE_FORMATS = {
    "pubmed": ("pubmed-nbib", "pubmed-csv"),
    "wos": ("wos-txt", "wos-csv"),
    "cnki": ("cnki-xls", "cnki-endnote"),
    "wanfang": ("wanfang-xlsx", "wanfang-endnote"),
}


async def list_sources(tenant_id: int) -> dict:
    """数据库切换入口：当前可见主文献，包含无批次历史题录；多来源成员不互斥。"""
    uid = _owner(tenant_id)
    async with pg_manager.get_async_session_context() as db:
        condition = await record_conditions(db, tenant_id)
        counts = [
            select(literal("").label("database"), func.count().label("papers"))
            .select_from(LiteraturePaper)
            .where(condition)
        ]
        counts.extend(
            select(literal(database), func.count())
            .select_from(LiteraturePaper)
            .where(condition, _database_condition(tenant_id, uid, database))
            for database in (*_DATABASE_FORMATS, "other")
        )
        rows = (await db.execute(counts[0].union_all(*counts[1:]))).all()
    return {"items": [{"database": database, "papers": papers} for database, papers in rows]}


async def list_batches(tenant_id: int, *, limit: int = 50, offset: int = 0) -> dict:
    uid = _owner(tenant_id)
    async with pg_manager.get_async_session_context() as db:
        rows = (
            (
                await db.execute(
                    select(LiteratureReferenceBatch)
                    .where(LiteratureReferenceBatch.tenant_id == tenant_id, LiteratureReferenceBatch.created_by == uid)
                    .order_by(LiteratureReferenceBatch.created_at.desc(), LiteratureReferenceBatch.batch_id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
    return {
        "items": [
            {
                "batch_id": row.batch_id,
                "source": row.source_name,
                "format": row.format,
                "sha256": row.source_sha256,
                "created_at": row.created_at.isoformat(),
                "report": row.report_json,
            }
            for row in rows[:limit]
        ],
        "has_more": len(rows) > limit,
    }


async def list_records(tenant_id: int, *, limit: int = 25, offset: int = 0, **filters) -> dict:
    async with pg_manager.get_async_session_context() as db:
        condition = await record_conditions(db, tenant_id, **filters)
        total = await db.scalar(select(func.count()).select_from(LiteraturePaper).where(condition))
        rows = (
            (
                await db.execute(
                    select(LiteraturePaper)
                    .where(condition)
                    .order_by(LiteraturePaper.pub_year.desc().nullslast(), LiteraturePaper.rid)
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return {
        "items": [
            {
                "rid": row.rid,
                "title": row.title,
                "authors": row.authors_json or [],
                "journal": row.journal,
                "year": row.pub_year,
                "doi": row.doi,
                "pmid": row.pmid,
                "abstract": row.abstract,
                "keywords": row.keywords_json or [],
                "affiliations": row.affiliations_json or [],
                "language": row.language,
                "source": row.source_main,
                "external_cited": row.external_cited,
                "has_pdf": bool(row.source_revision_id),
            }
            for row in rows
        ],
        "total": total or 0,
        "offset": offset,
        "limit": limit,
    }


async def record_conditions(
    db,
    tenant_id: int,
    *,
    batch_ids: list[str] | None = None,
    only_imports: bool = False,
    database: str = "",
    query: str = "",
    year_from: int | None = None,
    year_to: int | None = None,
):
    """清单、总览与网络共用同一个谓词，过滤必须先于计数/分页/聚合。"""
    uid = _owner(tenant_id)
    if year_from and year_to and year_from > year_to:
        raise ValueError("起始年份不能大于结束年份")
    ids = list(dict.fromkeys(batch_ids or []))
    if len(ids) > 50:
        raise ValueError("一次最多选择 50 个导入批次")
    conditions = [paper_visibility(tenant_id), LiteraturePaper.is_reference.is_(False)]
    if database:
        conditions.append(_database_condition(tenant_id, uid, database))
    if only_imports or ids:
        batches = select(LiteratureReferenceBatch.batch_id).where(
            LiteratureReferenceBatch.tenant_id == tenant_id, LiteratureReferenceBatch.created_by == uid
        )
        if ids:
            batches = batches.where(LiteratureReferenceBatch.batch_id.in_(ids))
            found = set((await db.execute(batches)).scalars())
            if found != set(ids):
                raise PermissionError("导入批次不存在或无权访问")
        members = select(LiteratureReferenceMember.rid).where(
            LiteratureReferenceMember.tenant_id == tenant_id, LiteratureReferenceMember.batch_id.in_(batches)
        )
        conditions.append(LiteraturePaper.rid.in_(members))
    if query.strip():
        escaped = query.strip().replace("/", "//").replace("%", "/%").replace("_", "/_")
        pattern = f"%{escaped}%"
        conditions.append(
            or_(
                *(
                    column.ilike(pattern, escape="/")
                    for column in (
                        LiteraturePaper.title,
                        LiteraturePaper.doi,
                        LiteraturePaper.pmid,
                        LiteraturePaper.journal,
                        cast(LiteraturePaper.authors_json, Text),
                        cast(LiteraturePaper.keywords_json, Text),
                    )
                )
            )
        )
    if year_from is not None:
        conditions.append(LiteraturePaper.pub_year >= year_from)
    if year_to is not None:
        conditions.append(LiteraturePaper.pub_year <= year_to)
    return and_(*conditions)


def _database_condition(tenant_id: int, uid: str, database: str):
    if database == "other":
        return ~or_(*(_database_condition(tenant_id, uid, key) for key in _DATABASE_FORMATS))
    if database not in _DATABASE_FORMATS:
        raise ValueError("不支持的文献数据库来源")
    formats = _DATABASE_FORMATS[database]
    members = (
        select(LiteratureReferenceMember.rid)
        .join(
            LiteratureReferenceBatch,
            and_(
                LiteratureReferenceBatch.batch_id == LiteratureReferenceMember.batch_id,
                LiteratureReferenceBatch.tenant_id == LiteratureReferenceMember.tenant_id,
            ),
        )
        .where(
            LiteratureReferenceBatch.tenant_id == tenant_id,
            LiteratureReferenceBatch.created_by == uid,
            LiteratureReferenceBatch.format.in_(formats),
        )
    )
    # Canonical rows keep their first source. Membership, not that single field,
    # carries later imports; historical rows without receipts remain reachable.
    return or_(LiteraturePaper.source_main.in_(formats), LiteraturePaper.rid.in_(members))


def _owner(tenant_id: int) -> str:
    scope = current_access()
    uid = getattr(getattr(scope, "user", None), "uid", None)
    if scope is None or scope.tenant_id != tenant_id or not uid:
        raise PermissionError("文献清单分析需要当前用户访问上下文")
    return uid
