"""PaperAgent 数据目录无损导入器（P3，ADR-0010 决议 5 /「完美兼容」支柱②）。

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

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yuxi.literature.identity.rid import doi_to_dirname
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCitation,
    LiteraturePaper,
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
    conflicts: list[str] = field(default_factory=list)
    dry_run: bool = True

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "papers": {"source": self.papers_source, "written": self.papers_written, "skipped": self.papers_skipped},
            "citations": {"source": self.citations_source, "written": self.citations_written},
            "variants": {"source": self.variants_source, "written": self.variants_written},
            "notes_written": self.notes_written,
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


async def import_paperagent_data(
    *,
    tenant_id: int,
    paperagent_root: Path,
    dry_run: bool = True,
) -> ImportReport:
    """执行导入。``dry_run=True`` 只统计与冲突检查，不写任何库/对象存储。"""
    report = ImportReport(dry_run=dry_run)
    biblio_path = paperagent_root / "data" / "biblio" / "biblio.db"
    if not biblio_path.exists():
        raise ValueError(f"biblio.db 不存在: {biblio_path}")
    paper_rows, citation_rows = _read_biblio(biblio_path)
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
                doi = str(row.get("doi") or "") or None
                kind = _upstream_kind(row)
                stmt = (
                    pg_insert(LiteraturePaper)
                    .values(
                        tenant_id=tenant_id,
                        rid=rid,
                        kind=kind,
                        doi=doi,
                        title=str(row.get("title") or "") or None,
                        paper_rank=float(row.get("paper_rank") or 0),
                        cocitation_cluster=int(row.get("cocitation_cluster") or 0),
                        impact_factor=row.get("impact_factor"),
                        quartile=str(row.get("quartile") or ""),
                        value_score=float(row.get("ai_value_score") or 0)
                        if row.get("ai_value_score") is not None
                        else 0.0,
                        ai_value_score=row.get("ai_value_score"),
                        topic_score=row.get("topic_score"),
                    )
                    .on_conflict_do_update(
                        index_elements=("tenant_id", "rid"),
                        set_={
                            "title": str(row.get("title") or "") or None,
                            "paper_rank": float(row.get("paper_rank") or 0),
                            "impact_factor": row.get("impact_factor"),
                            "quartile": str(row.get("quartile") or ""),
                            "ai_value_score": row.get("ai_value_score"),
                            "topic_score": row.get("topic_score"),
                            "updated_at": utc_now(),
                        },
                    )
                    .returning(LiteraturePaper.id)
                )
                paper_id = (await session.execute(stmt)).scalar_one()
                rid_to_paper_id[rid] = paper_id
                report.papers_written += 1
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
                await session.execute(stmt)
                report.citations_written += 1
            await session.commit()

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
            for paper_dir in sorted(p for p in kb_root.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
                paper_row = existing.get(paper_dir.name)
                # 上游布局：knowledge_base/<DOI 目录名>（无 doi- 前缀）、papers_meta.rid
                # 带 doi- 前缀——两种键形态都要命中（P0 三键归一语义的变现）。
                dir_rid_candidates = (paper_dir.name, f"doi-{paper_dir.name}")
                matched = (
                    any(c in rid_to_paper_id for c in dir_rid_candidates)
                    or paper_row is not None
                    or (dry_run and any(c in expected_rids for c in dir_rid_candidates))
                )
                paper_id = next((rid_to_paper_id[c] for c in dir_rid_candidates if c in rid_to_paper_id), None) or (
                    paper_row.id if paper_row else None
                )
                for filename, (variant_kind, lang) in _VARIANT_FILES.items():
                    file_path = paper_dir / filename
                    if not file_path.exists() or not matched:
                        continue
                    report.variants_source += 1
                    if dry_run:
                        continue
                    content = file_path.read_bytes()
                    digest = hashlib.sha256(content).hexdigest()
                    revision = paper_row.source_revision_id or "imported" if paper_row else "imported"
                    uri = f"kbasset://tenants/{tenant_id}/literature/variants/{paper_dir.name}/{filename}"
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
                            variant_kind=variant_kind,
                            lang=lang,
                            uri=uri,
                            content_digest=digest,
                            model_id="paperagent-import",
                        )
                        .on_conflict_do_nothing(
                            index_elements=("tenant_id", "paper_id", "variant_kind", "lang", "parse_revision_id")
                        )
                    )
                    await session.execute(stmt)
                    report.variants_written += 1
                for filename, target_name in _NOTE_FILES.items():
                    file_path = paper_dir / filename
                    if not file_path.exists():
                        continue
                    if dry_run:
                        report.notes_written += 1
                        continue
                    await minio.aupload_file(
                        bucket_name=RENDER_BUCKET,
                        object_name=f"tenants/{tenant_id}/literature/notes/{paper_dir.name}/{target_name}",
                        data=file_path.read_bytes(),
                        content_type="text/markdown",
                    )
                    report.notes_written += 1
            if not dry_run:
                await session.commit()
    return report
