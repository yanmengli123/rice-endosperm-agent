"""PaperAgent 核心数据兼容导入器（不等同于整机无损备份）（P3，ADR-0010 决议 5 /「完美兼容」支柱②）。

把 PaperAgent（v1.4.0，DATA_FORMAT 4）的数据目录映射进 Yuxi：

- ``data/biblio/biblio.db``：papers_meta → ``literature_papers``（RID 经 P0
  身份模块重算并断言与上游目录名一致）；citations → ``literature_citations``
- ``knowledge_base/<目录>/``：zh.md / en_zh.md / summary.md → ``literature_variants``
  （lang 进身份）；_note/_wiki/_relations → MinIO notes 前缀
- ``library/<目录>/document.json``：解析产物 → 阅读前缀（source_revision 挂
  导入批次伪 revision，P3 最小实现；重新渲染可后续覆盖）

纪律（对标上游 ``tools/migrate_data_layout.py``）：**dry-run 先行**（逐表
源/目标行数核对，不写库）；幂等（RID/唯一键 upsert，可重复执行）；报告含
源行数/写入/跳过/冲突。上游 SQLite 只读连接，不经任何 vendor 运行时。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yuxi.literature.identity.rid import doi_to_dirname
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseRevision,
    LiteratureArtifactRevision,
    LiteratureCitation,
    LiteratureCompileJob,
    LiteraturePaper,
    LiteratureReaderRender,
    LiteratureVariant,
)
from yuxi.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)

_KIND_ALIASES = {"book": "book", "thesis": "thesis", "standard": "standard", "patent": "patent", "chapter": "chapter"}
_NOTE_FILES = {"_note.md": "L1_note.md", "_wiki.md": "L2_wiki.md", "_relations.md": "L3_relations.md"}
_VARIANT_FILES = {"zh.md": ("zh", "zh"), "en_zh.md": ("en_zh", "zh"), "summary.md": ("summary", "zh")}


@dataclass
class ImportReport:
    """逐表导入报告（dry-run 与正式执行共用形状）。"""

    papers_source: int = 0
    papers_written: int = 0
    papers_skipped: int = 0
    citations_source: int = 0
    citations_written: int = 0
    variants_source: int = 0
    variants_written: int = 0
    notes_written: int = 0
    reader_renders_written: int = 0
    conflicts: list[str] = field(default_factory=list)
    dry_run: bool = True

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "papers": {"source": self.papers_source, "written": self.papers_written, "skipped": self.papers_skipped},
            "citations": {"source": self.citations_source, "written": self.citations_written},
            "variants": {"source": self.variants_source, "written": self.variants_written},
            "notes_written": self.notes_written,
            "reader_renders_written": self.reader_renders_written,
            "conflicts": self.conflicts,
        }


def _read_biblio(db_path: Path) -> tuple[list[dict], list[dict]]:
    """只读 biblio.db：papers_meta + citations 行（dict 化；表缺失视为空）。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        papers: list[dict] = []
        citations: list[dict] = []
        try:
            papers = [dict(r) for r in conn.execute("SELECT * FROM papers_meta")]
        except sqlite3.OperationalError:
            pass
        try:
            citations = [dict(r) for r in conn.execute("SELECT * FROM citations")]
        except sqlite3.OperationalError:
            pass
        return papers, citations
    finally:
        conn.close()


def _upstream_kind(meta_row: dict) -> str:
    kind = str(meta_row.get("kind") or "paper").strip()
    return _KIND_ALIASES.get(kind, "paper" if kind not in _KIND_ALIASES else kind)


def _expected_dirname(meta_row: dict) -> str:
    """上游 RID（论文 `doi-…`/前缀身份）→ 期望目录名（P0 身份兼容断言的基准）。"""
    rid = str(meta_row.get("rid") or "")
    if rid.startswith("doi-"):
        return rid.removeprefix("doi-")
    return rid


def _document_markdown(document: dict) -> str:
    metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
    title = str(metadata.get("title") or "").strip()
    paragraphs = [
        str(row.get("text_en") or row.get("text") or "").strip()
        for row in (document.get("paragraphs") or [])
        if isinstance(row, dict)
    ]
    body = "\n\n".join(part for part in paragraphs if part)
    return (f"# {title}\n\n" if title else "") + body + "\n"


def _read_named_table(db_path: Path, table: str) -> list[dict]:
    """只读归档 SQLite 的单表（kb_trash 注册行；表缺失视为空）。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        try:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()


def knowledge_directories(root: Path) -> dict[str, Path]:
    """Registered trash only; ambiguous snapshots stay immutable history.

    Match upstream's timestamp suffix exactly, never follow archive DB paths.
    Active directories take priority. A duplicate is not silently selected.
    """
    import re

    kb = root / "knowledge_base"
    directories = {}
    trash = kb / ".trash"
    rows = _read_named_table(root / "data/biblio/biblio.db", "kb_trash")
    if trash.is_dir():
        for row in rows:
            name = str(row.get("dirname") or "")
            if not name or name.startswith(("_", ".")) or any(c in name for c in "/\\:"):
                continue
            pattern = re.compile(re.escape(name) + r"(?:__\d{14})?\Z")
            candidates = [path for path in trash.iterdir() if path.is_dir() and pattern.fullmatch(path.name)]
            if len(candidates) == 1:
                directories[name] = candidates[0]
    if kb.is_dir():
        directories.update(
            {path.name: path for path in kb.iterdir() if path.is_dir() and not path.name.startswith(("_", "."))}
        )
    return directories


async def _import_library_renders(
    *,
    tenant_id: int,
    paperagent_root: Path,
    papers: dict[str, LiteraturePaper],
    report: ImportReport,
    actor_uid: str,
) -> None:
    """Map PaperAgent self-contained library folders into Yuxi reader products."""
    # DATA-LAYOUT: knowledge_base/document.json is the finalized document;
    # library/document.json is only a fallback. Never reverse that priority.
    directories = {}
    library = paperagent_root / "library"
    if library.is_dir():
        directories.update(
            {
                path.name: path
                for path in library.iterdir()
                if path.is_dir() and not path.name.startswith(("_", ".")) and (path / "document.json").is_file()
            }
        )
    directories.update(
        {
            name: path
            for name, path in knowledge_directories(paperagent_root).items()
            if (path / "document.json").is_file()
        }
    )
    if not directories:
        return
    pending_documents = []
    kb_id = f"paperagent-import-{tenant_id}"
    minio = get_minio_client()
    async with pg_manager.get_async_session_context() as session:
        await session.execute(
            pg_insert(KnowledgeBase)
            .values(
                tenant_id=tenant_id,
                kb_id=kb_id,
                name="PaperAgent 导入文献",
                description="PaperAgent DATA_FORMAT 4 迁移的权威源占位；阅读产物保持派生身份。",
                kb_type="document",
                governance_status="ACTIVE",
                normalized_name=f"paperagent-import-{tenant_id}",
                created_by=actor_uid,
            )
            .on_conflict_do_nothing(index_elements=("kb_id",))
        )
        for dirname, paper_dir in sorted(directories.items()):
            paper = papers.get(dirname) or papers.get(f"doi-{dirname}")
            if paper is not None and paper.source_revision_id and not paper.source_revision_id.startswith("pair-"):
                report.conflicts.append(f"保留已有权威解析关联: {paper.rid}")
                continue
            document_path = paper_dir / "document.json"
            if paper is None or not document_path.is_file():
                continue
            document_bytes = await asyncio.to_thread(document_path.read_bytes)
            document = json.loads(document_bytes.decode("utf-8"))
            if not isinstance(document, dict) or not isinstance(document.get("paragraphs"), list):
                raise ValueError(f"非法 document.json: {paper.rid}")
            for index, paragraph in enumerate(document["paragraphs"]):
                if not isinstance(paragraph, dict):
                    raise ValueError(f"非法段落: {paper.rid}")
                paragraph["para_id"] = str(paragraph.get("para_id") or f"P{index + 1:03d}")
            source_pdf = paper_dir / "source.pdf"
            source_bytes = await asyncio.to_thread(source_pdf.read_bytes) if source_pdf.is_file() else b""
            source_sha = hashlib.sha256(source_bytes or document_bytes).hexdigest()
            document_digest = hashlib.sha256(document_bytes).hexdigest()
            identity = hashlib.sha256(f"{tenant_id}:{paper.rid}:{source_sha}:{document_digest}".encode()).hexdigest()
            file_id = f"paif-{identity[:32]}"
            revision_id = f"pair-{identity[:32]}"
            if (
                paper.source_revision_id
                and paper.source_revision_id != revision_id
                and "document" in (paper.frozen_artifacts or [])
            ):
                report.conflicts.append(f"保留已冻结正文及来源: {paper.rid}")
                continue
            prefix = f"tenants/{tenant_id}/documents/{source_sha}/reader_render/{revision_id}/"
            document["_image_base"] = prefix
            normalized_bytes = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
            markdown_path = paper_dir / "en.md"
            markdown = (
                await asyncio.to_thread(markdown_path.read_text, encoding="utf-8")
                if markdown_path.is_file()
                else _document_markdown(document)
            )
            await minio.aupload_file(
                bucket_name=RENDER_BUCKET,
                object_name=f"{prefix}document.json",
                data=normalized_bytes,
                content_type="application/json",
            )
            await minio.aupload_file(
                bucket_name=RENDER_BUCKET,
                object_name=f"{prefix}render.md",
                data=markdown.encode("utf-8"),
                content_type="text/markdown",
            )
            image_count = 0
            images_dir = paper_dir / "images"
            if images_dir.is_dir():
                for image_path in sorted(path for path in images_dir.rglob("*") if path.is_file()):
                    relative = image_path.relative_to(images_dir).as_posix()
                    await minio.aupload_file(
                        bucket_name=RENDER_BUCKET,
                        object_name=f"{prefix}images/{relative}",
                        data=await asyncio.to_thread(image_path.read_bytes),
                        content_type="application/octet-stream",
                    )
                    image_count += 1
            if source_bytes:
                await minio.aupload_file(
                    bucket_name=RENDER_BUCKET,
                    object_name=f"tenants/{tenant_id}/documents/{source_sha}/source/original.pdf",
                    data=source_bytes,
                    content_type="application/pdf",
                )
            await session.execute(
                pg_insert(KnowledgeFile)
                .values(
                    file_id=file_id,
                    kb_id=kb_id,
                    filename=source_pdf.name if source_pdf.is_file() else f"{paper_dir.name}.json",
                    original_filename=source_pdf.name if source_pdf.is_file() else "document.json",
                    file_type="pdf" if source_pdf.is_file() else "json",
                    status="processed",
                    content_hash=source_sha,
                    file_size=len(source_bytes or document_bytes),
                    active_parse_revision_id=revision_id,
                    evidence_status="DERIVED_IMPORT",
                    created_by=actor_uid,
                )
                .on_conflict_do_update(
                    index_elements=("file_id",),
                    set_={"active_parse_revision_id": revision_id, "status": "processed"},
                )
            )
            await session.execute(
                pg_insert(KnowledgeParseRevision)
                .values(
                    revision_id=revision_id,
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    file_id=file_id,
                    source_sha256=source_sha,
                    parser_fingerprint="paperagent-data-format-4",
                    pipeline_version="paperagent_import_v4",
                    status="ACTIVE",
                    article_summary={"title": paper.title, "doi": paper.doi, "imported": True},
                    qa_report={"source": "paperagent", "evidence_imported": False},
                    capabilities={"reader": True, "source_pdf": bool(source_bytes)},
                    created_by=actor_uid,
                )
                .on_conflict_do_nothing(index_elements=("revision_id",))
            )
            await session.execute(
                pg_insert(LiteratureReaderRender)
                .values(
                    revision_id=revision_id,
                    tenant_id=tenant_id,
                    status="SUCCEEDED",
                    render_version="paperagent_v4",
                    markdown_uri=f"kbasset://{prefix}render.md",
                    image_count=image_count,
                    source_digest=hashlib.sha256(document_bytes).hexdigest(),
                )
                .on_conflict_do_update(
                    index_elements=("revision_id", "render_version"),
                    set_={
                        "status": "SUCCEEDED",
                        "markdown_uri": f"kbasset://{prefix}render.md",
                        "image_count": image_count,
                    },
                )
            )
            await session.execute(
                update(LiteraturePaper)
                .where(LiteraturePaper.id == paper.id)
                .values(source_revision_id=revision_id, updated_at=utc_now())
            )
            pending_documents.append(
                (paper.rid, normalized_bytes, not bool((paper.artifact_pointers or {}).get("document")))
            )
            report.reader_renders_written += 1
        await session.commit()
    # Refresh the request's visibility after creating a source-linked record.
    from contextlib import asynccontextmanager

    from yuxi.literature.access import access_scope, current_access
    from yuxi.literature.artifacts import ArtifactKind, ArtifactSource, store_revision

    @asynccontextmanager
    async def refreshed_scope():
        scope = current_access()
        if scope is None:
            yield
        else:
            async with access_scope(scope.user, tenant_id):
                yield

    async with refreshed_scope():
        for rid, content, first in pending_documents:
            await store_revision(
                tenant_id,
                rid,
                ArtifactKind.DOCUMENT,
                content=content,
                content_type="application/json",
                operator=actor_uid,
                freeze=first,
                advance_reading_version=True,
                source=ArtifactSource.IMPORTED,
                storage=minio,
            )


async def import_paperagent_data(
    *,
    tenant_id: int,
    paperagent_root: Path,
    dry_run: bool = True,
    actor_uid: str = "paperagent-import",
) -> ImportReport:
    """执行导入。``dry_run=True`` 只统计与冲突检查，不写任何库/对象存储。"""
    report = ImportReport(dry_run=dry_run)
    biblio_path = paperagent_root / "data" / "biblio" / "biblio.db"
    if not biblio_path.exists():
        raise ValueError(f"biblio.db 不存在: {biblio_path}")
    paper_rows, citation_rows = await asyncio.to_thread(_read_biblio, biblio_path)
    report.papers_source = len(paper_rows)
    report.citations_source = len(citation_rows)

    rid_to_paper_id: dict[str, int] = {}
    if not dry_run:
        async with pg_manager.get_async_session_context() as session:
            for row in paper_rows:
                rid = str(row.get("rid") or "")
                if not rid:
                    report.papers_skipped += 1
                    continue

                def json_list(value):
                    if isinstance(value, list):
                        return value
                    if isinstance(value, str):
                        try:
                            parsed = json.loads(value)
                            return parsed if isinstance(parsed, list) else []
                        except ValueError:
                            return [part.strip() for part in value.split(";") if part.strip()]
                    return []

                values = {
                    "doi": str(row.get("doi") or "") or None,
                    "kind": _upstream_kind(row),
                    "title": str(row.get("title") or "") or None,
                    "paper_rank": float(row.get("paper_rank") or 0),
                    "cocitation_cluster": int(row.get("cocitation_cluster") or 0),
                    "impact_factor": row.get("impact_factor"),
                    "quartile": str(row.get("quartile") or ""),
                    "value_score": float(row.get("ai_value_score") or 0),
                    "ai_value_score": row.get("ai_value_score"),
                    "topic_score": row.get("topic_score"),
                    "authors_json": json_list(row.get("authors_json") or row.get("authors")),
                    "abstract": str(row.get("abstract") or ""),
                    "journal": str(row.get("journal") or ""),
                    "issn": str(row.get("issn") or ""),
                    "pub_year": row.get("pub_year") or row.get("year"),
                    "keywords_json": json_list(row.get("keywords_json") or row.get("keywords")),
                    "source_main": str(row.get("source_main") or "paperagent-import"),
                    "is_reference": bool(row.get("is_reference", False)),
                    "external_cited": row.get("external_cited")
                    if row.get("external_cited") is not None
                    else row.get("times_cited"),
                    "meta_locked": bool(row.get("meta_locked", False)),
                }
                # Insert first; a repeat must not overwrite local annotations,
                # metrics, locked metadata, or a native PDF association.
                inserted = (
                    await session.execute(
                        pg_insert(LiteraturePaper)
                        .values(tenant_id=tenant_id, rid=rid, **values)
                        .on_conflict_do_nothing(index_elements=("tenant_id", "rid"))
                        .returning(LiteraturePaper.id)
                    )
                ).scalar_one_or_none()
                paper = (
                    await session.execute(
                        select(LiteraturePaper)
                        .where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == rid)
                        .with_for_update()
                    )
                ).scalar_one()
                rid_to_paper_id[rid] = paper.id
                if inserted is not None:
                    report.papers_written += 1
                else:
                    report.papers_skipped += 1
                    if not paper.meta_locked:
                        for key in (
                            "doi",
                            "title",
                            "authors_json",
                            "abstract",
                            "journal",
                            "issn",
                            "pub_year",
                            "keywords_json",
                            "impact_factor",
                            "quartile",
                            "external_cited",
                        ):
                            if getattr(paper, key) in (None, "", []) and values[key] not in (None, "", []):
                                setattr(paper, key, values[key])
            await session.commit()

    # 目录名 ↔ RID 兼容性断言（P0 身份契约的变现）：doi 行的目录名必须可由
    # doi_to_dirname 重建（目录名不再是身份单一来源，但导入需对齐上游布局）
    for row in paper_rows:
        rid = str(row.get("rid") or "")
        if rid.startswith("doi-") and row.get("doi"):
            expected = doi_to_dirname(str(row["doi"]))
            if expected != _expected_dirname(row):
                report.conflicts.append(f"目录名漂移: rid={rid} 期望={expected}")

    if not dry_run:
        async with pg_manager.get_async_session_context() as session:
            for edge in citation_rows:
                citing = str(edge.get("citing_doi") or "")
                cited = str(edge.get("cited_doi") or "")
                if not citing or not cited:
                    continue
                citing_rid = citing if citing.startswith("doi-") else f"doi-{doi_to_dirname(citing)}"
                paper_id = rid_to_paper_id.get(citing_rid)
                if paper_id is None:
                    continue
                stmt = (
                    pg_insert(LiteratureCitation)
                    .values(
                        tenant_id=tenant_id,
                        citing_rid=citing_rid,
                        cited_doi=cited,
                        cited_brief=str(edge.get("cited_brief") or "") or None,
                    )
                    .on_conflict_do_nothing(index_elements=("tenant_id", "citing_rid", "cited_doi"))
                )
                inserted = (await session.execute(stmt.returning(LiteratureCitation.id))).scalar_one_or_none()
                report.citations_written += int(inserted is not None)
            await session.commit()

    if not dry_run:
        async with pg_manager.get_async_session_context() as session:
            papers = {
                row.rid: row
                for row in (
                    await session.execute(
                        select(LiteraturePaper).where(
                            LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid.in_(rid_to_paper_id)
                        )
                    )
                )
                .scalars()
                .all()
            }
        await _import_library_renders(
            tenant_id=tenant_id,
            paperagent_root=paperagent_root,
            papers=papers,
            report=report,
            actor_uid=actor_uid,
        )

    # knowledge_base/<目录>/：变体与笔记（dry-run 按 biblio 预期 RID 匹配计数）
    kb_root = paperagent_root / "knowledge_base"
    minio = None if dry_run else get_minio_client()
    expected_rids = {str(r.get("rid") or "") for r in paper_rows}
    if kb_root.is_dir():
        async with pg_manager.get_async_session_context() as session:
            existing: dict[str, LiteraturePaper] = {}
            if not dry_run:
                existing = {
                    row.rid: row
                    for row in (
                        await session.execute(select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id))
                    )
                    .scalars()
                    .all()
                }
            for dirname, paper_dir in sorted(knowledge_directories(paperagent_root).items()):
                paper_row = existing.get(dirname) or existing.get(f"doi-{dirname}")
                # 上游布局：knowledge_base/<DOI 目录名>（无 doi- 前缀）、papers_meta.rid
                # 带 doi- 前缀——两种键形态都要命中（P0 三键归一语义的变现）。
                dir_rid_candidates = (dirname, f"doi-{dirname}")
                matched = (
                    any(c in rid_to_paper_id for c in dir_rid_candidates)
                    or paper_row is not None
                    or (dry_run and any(c in expected_rids for c in dir_rid_candidates))
                )
                paper_id = next((rid_to_paper_id[c] for c in dir_rid_candidates if c in rid_to_paper_id), None) or (
                    paper_row.id if paper_row else None
                )
                if not dry_run and paper_row:
                    pointer = (paper_row.artifact_pointers or {}).get("document")
                    if pointer:
                        source = (
                            await session.execute(
                                select(LiteratureArtifactRevision.source).where(
                                    LiteratureArtifactRevision.revision_id == pointer,
                                    LiteratureArtifactRevision.tenant_id == tenant_id,
                                    LiteratureArtifactRevision.paper_id == paper_row.id,
                                )
                            )
                        ).scalar_one_or_none()
                        if source != "imported":
                            report.conflicts.append(f"不把历史笔记/译文挂到人工修改的当前正文: {paper_row.rid}")
                            continue
                for filename, (variant_kind, lang) in _VARIANT_FILES.items():
                    file_path = paper_dir / filename
                    if not file_path.exists() or not matched:
                        continue
                    report.variants_source += 1
                    if dry_run:
                        continue
                    content = await asyncio.to_thread(file_path.read_bytes)
                    digest = hashlib.sha256(content).hexdigest()
                    if paper_row.source_revision_id and not paper_row.source_revision_id.startswith("pair-"):
                        report.conflicts.append(f"不把历史译文挂到新的权威来源: {paper_row.rid}")
                        continue
                    revision = paper_row.source_revision_id or ""
                    reading_version = paper_row.reading_version
                    already = (
                        await session.execute(
                            select(LiteratureVariant.id)
                            .where(
                                LiteratureVariant.tenant_id == tenant_id,
                                LiteratureVariant.paper_id == paper_id,
                                LiteratureVariant.variant_kind == variant_kind,
                                LiteratureVariant.lang == lang,
                                LiteratureVariant.parse_revision_id == revision,
                                LiteratureVariant.reading_version == reading_version,
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if already is not None:
                        continue
                    uri = f"kbasset://tenants/{tenant_id}/literature/variants/{paper_dir.name}/{digest}/{filename}"
                    await minio.aupload_file(
                        bucket_name=RENDER_BUCKET,
                        object_name=uri.removeprefix("kbasset://"),
                        data=content,
                        content_type="text/markdown",
                    )
                    stmt = (
                        pg_insert(LiteratureVariant)
                        .values(
                            tenant_id=tenant_id,
                            paper_id=paper_id,
                            parse_revision_id=revision,
                            reading_version=reading_version,
                            generator_version="paperagent-import-v4",
                            variant_kind=variant_kind,
                            lang=lang,
                            uri=uri,
                            content_digest=digest,
                            model_id="paperagent-import",
                        )
                        .on_conflict_do_nothing(
                            index_elements=(
                                "tenant_id",
                                "paper_id",
                                "variant_kind",
                                "lang",
                                "parse_revision_id",
                                "reading_version",
                                "generator_version",
                            )
                        )
                    )
                    await session.execute(stmt)
                    report.variants_written += 1
                for filename, target_name in _NOTE_FILES.items():
                    file_path = paper_dir / filename
                    if not file_path.exists() or not matched:
                        continue
                    if dry_run:
                        report.notes_written += 1
                        continue
                    if paper_row.source_revision_id and not paper_row.source_revision_id.startswith("pair-"):
                        report.conflicts.append(f"不把历史笔记挂到新的权威来源: {paper_row.rid}")
                        continue
                    level = target_name.split("_", 1)[0]
                    already = (
                        await session.execute(
                            select(LiteratureCompileJob.id)
                            .where(
                                LiteratureCompileJob.tenant_id == tenant_id,
                                LiteratureCompileJob.paper_id == paper_id,
                                LiteratureCompileJob.level == level,
                                LiteratureCompileJob.source_revision_id == (paper_row.source_revision_id or ""),
                                LiteratureCompileJob.reading_version == paper_row.reading_version,
                                LiteratureCompileJob.card_revision == paper_row.card_revision,
                                LiteratureCompileJob.status == "SUCCEEDED",
                            )
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    if already is not None:
                        continue
                    note_content = await asyncio.to_thread(file_path.read_bytes)
                    note_digest = hashlib.sha256(note_content).hexdigest()
                    object_name = f"tenants/{tenant_id}/literature/notes/{paper_dir.name}/{note_digest}/{target_name}"
                    await minio.aupload_file(
                        bucket_name=RENDER_BUCKET,
                        object_name=object_name,
                        data=note_content,
                        content_type="text/markdown",
                    )
                    level = target_name.split("_", 1)[0]
                    if paper_id is not None:
                        await session.execute(
                            pg_insert(LiteratureCompileJob)
                            .values(
                                tenant_id=tenant_id,
                                paper_id=paper_id,
                                level=level,
                                source_revision_id=paper_row.source_revision_id or "",
                                reading_version=paper_row.reading_version,
                                generator_version="paperagent-import-v4",
                                card_revision=paper_row.card_revision,
                                status="SUCCEEDED",
                                note_uri=f"kbasset://{object_name}",
                            )
                            .on_conflict_do_update(
                                index_elements=(
                                    "tenant_id",
                                    "paper_id",
                                    "level",
                                    "source_revision_id",
                                    "reading_version",
                                    "generator_version",
                                    "card_revision",
                                ),
                                set_={"status": "SUCCEEDED", "note_uri": f"kbasset://{object_name}", "error": None},
                            )
                        )
                        if level == "L1":
                            from yuxi.literature.fulltext import project_note

                            imported_job = (
                                await session.execute(
                                    select(LiteratureCompileJob).where(
                                        LiteratureCompileJob.tenant_id == tenant_id,
                                        LiteratureCompileJob.paper_id == paper_id,
                                        LiteratureCompileJob.level == level,
                                        LiteratureCompileJob.note_uri == f"kbasset://{object_name}",
                                        LiteratureCompileJob.source_revision_id == (paper_row.source_revision_id or ""),
                                        LiteratureCompileJob.reading_version == paper_row.reading_version,
                                        LiteratureCompileJob.card_revision == paper_row.card_revision,
                                        LiteratureCompileJob.generator_version == "paperagent-import-v4",
                                    )
                                )
                            ).scalar_one()
                            await project_note(session, imported_job, note_content.decode("utf-8"))
                    report.notes_written += 1
            if not dry_run:
                await session.commit()
    if dry_run:
        directories = set()
        for root in (paperagent_root / "library",):
            if root.is_dir():
                directories.update(
                    path.name
                    for path in root.iterdir()
                    if path.is_dir()
                    and (path / "document.json").is_file()
                    and (path.name in expected_rids or f"doi-{path.name}" in expected_rids)
                )
        directories.update(
            name
            for name, path in knowledge_directories(paperagent_root).items()
            if (path / "document.json").is_file() and (name in expected_rids or f"doi-{name}" in expected_rids)
        )
        report.reader_renders_written = len(directories)
    return report
