"""BibTeX（WoS 导出）导入与 Markdown 直导（G2；语义移植自 paperlit/ingest/bib_ingest.py
+ dedup.py，v1.4.0）。

- 字符级 BibTeX 解析（嵌套花括号安全）逐字移植上游 `_read_fields`/`parse_bib`；
- 去重口径：DOI/RID 已存在即跳过（上游 deduplicate 语义）；
- 引用边：`CR` 字段的被引 DOI 列表 → ``literature_citations``；
- Markdown 直导：无解析直接建文献身份（nd- 指纹 RID）+ 内容落 MinIO（0 API）。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yuxi.literature.identity.rid import doi_to_dirname, is_doi, make_rid
from yuxi.literature.access import current_access
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteratureCitation, LiteraturePaper

# ---------------------------------------------------------------- BibTeX 解析（移植）


def _read_fields(text: str, start: int) -> dict[str, str]:
    """从 ``{`` 开始读取所有 field = {value} 对（上游逐字移植：嵌套花括号计数）。"""
    fields: dict[str, str] = {}
    pos = start + 1
    while pos < len(text):
        fm = re.match(r"\s*(\w[\w-]*)\s*=\s*", text[pos:])
        if not fm:
            if text[pos] == "}":
                break
            pos += 1
            continue
        key = fm.group(1).lower()
        pos += fm.end()
        if pos < len(text) and text[pos] == "{":
            depth = 1
            val_start = pos + 1
            pos += 1
            while pos < len(text) and depth:
                if text[pos] == "{":
                    depth += 1
                elif text[pos] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                pos += 1
            fields[key] = text[val_start:pos].strip()
            pos += 1
        else:
            m = re.match(r"[^,\n]+", text[pos:])
            fields[key] = m.group(0).strip() if m else ""
            pos += m.end() if m else 0
        if pos < len(text) and text[pos] == ",":
            pos += 1
    return fields


def parse_bib(text: str) -> list[dict[str, str]]:
    """BibTeX 文本 → 原始字段字典列表（上游 parse_bib 主循环移植）。"""
    records: list[dict[str, str]] = []
    pos = 0
    while pos < len(text):
        at_idx = text.find("@", pos)
        if at_idx == -1:
            break
        m = re.match(r"@(\w+)\s*\{\s*([^,]*?),", text[at_idx:])
        if not m:
            pos = at_idx + 1
            continue
        entry_type = m.group(1).lower()
        cite_key = m.group(2).strip()
        if entry_type == "comment":
            pos = at_idx + m.end()
            continue
        brace_start = at_idx + m.end() - 1
        fields = _read_fields(text, brace_start)
        fields["_type"] = entry_type
        fields["_key"] = cite_key
        records.append(fields)
        # 跳过本条记录的花括号块
        pos = brace_start + 1
        depth = 0
        while pos < len(text):
            if text[pos] == "{":
                depth += 1
            elif text[pos] == "}":
                if depth == 0:
                    pos += 1
                    break
                depth -= 1
            pos += 1
    return records


def _clean_bib_value(value: str) -> str:
    """BibTeX 值清洗：去包裹花括号与多余空白。"""
    text = re.sub(r"\s+", " ", (value or "").strip())
    return re.sub(r"[{}]", "", text).strip()


def _parse_cited_dois(value: str) -> list[str]:
    """CR 字段 → 被引 DOI 列表（只收 DOI 形态，避免"文件名当 DOI"）。"""
    dois: list[str] = []
    for token in re.split(r";|\n", value or ""):
        candidate = _clean_bib_value(token)
        m = re.search(r"(10\.\d{4,9}/\S+)", candidate)
        if m:
            doi = m.group(1).rstrip(".,;")
            if is_doi(doi) and doi not in dois:
                dois.append(doi)
    return dois


# ---------------------------------------------------------------- 入库


async def import_bib(tenant_id: int, bib_text: str, *, source_name: str = "") -> dict[str, Any]:
    """导入 WoS/BibTeX 文本：登记文献 + 引用边（幂等：DOI/RID 已存在跳过）。"""
    records = parse_bib(bib_text)
    report = {
        "source": source_name,
        "records": len(records),
        "papers_written": 0,
        "papers_skipped": 0,
        "citations_written": 0,
        "no_identity": 0,
    }
    rid_by_doi: dict[str, str] = {}
    async with pg_manager.get_async_session_context() as session:
        for fields in records:
            doi = _clean_bib_value(fields.get("doi", ""))
            title = _clean_bib_value(fields.get("title", ""))
            kind = _bib_kind(fields)
            existing_rid = None
            if is_doi(doi):
                existing_rid = f"doi-{doi_to_dirname(doi)}"
            else:
                # 无 DOI：标题+年份指纹身份（同条重导 → 同 RID，天然去重）
                fingerprint = hashlib.md5(f"{title}|{fields.get('year', '')}".encode()).hexdigest()
                existing_rid = make_rid(kind, fingerprint=fingerprint)
            exists = (
                await session.execute(
                    select(LiteraturePaper.id).where(
                        LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == existing_rid
                    )
                )
            ).scalar_one_or_none()
            if exists is not None:
                scope = current_access()
                if scope is not None and existing_rid not in scope.rids:
                    raise ValueError("导入记录与不可访问来源冲突")
                report["papers_skipped"] += 1
                if is_doi(doi):
                    rid_by_doi[doi] = existing_rid
                continue
            paper = LiteraturePaper(
                tenant_id=tenant_id,
                rid=existing_rid,
                kind=kind,
                doi=doi or None,
                title=title or None,
            )
            session.add(paper)
            await session.flush()
            report["papers_written"] += 1
            if is_doi(doi):
                rid_by_doi[doi] = existing_rid
        # 引用边（DOI 形态才登记）
        for fields in records:
            citing_doi = _clean_bib_value(fields.get("doi", ""))
            citing_rid = rid_by_doi.get(citing_doi)
            if not citing_rid:
                continue
            for cited_doi in _parse_cited_dois(fields.get("cr", "")):
                stmt = (
                    pg_insert(LiteratureCitation)
                    .values(tenant_id=tenant_id, citing_rid=citing_rid, cited_doi=cited_doi)
                    .on_conflict_do_nothing(index_elements=("tenant_id", "citing_rid", "cited_doi"))
                    .returning(LiteratureCitation.id)
                )
                # RETURNING 判实插：冲突跳过不计入 written（幂等语义）
                inserted = (await session.execute(stmt)).scalar_one_or_none()
                if inserted is not None:
                    report["citations_written"] += 1
        await session.commit()
    if report["records"] and not report["papers_written"] and not report["papers_skipped"]:
        report["no_identity"] = report["records"]
    return report


def _bib_kind(fields: dict[str, str]) -> str:
    entry_type = (fields.get("_type") or "").lower()
    mapping = {"phdthesis": "thesis", "book": "book", "inbook": "chapter", "patent": "patent", "techreport": "report"}
    return mapping.get(entry_type, "paper")


async def import_markdown_document(
    tenant_id: int,
    *,
    markdown: str,
    title: str = "",
    doi: str = "",
) -> dict[str, Any]:
    """Markdown 直导（上游 imports.import_markdown 语义：免解析、0 API）。"""
    if not markdown.strip():
        raise ValueError("Markdown 内容为空")
    if doi and not is_doi(doi):
        raise ValueError(f"非法 DOI: {doi}")
    rid = make_rid("paper", doi=doi, fingerprint=hashlib.md5(markdown.encode("utf-8")).hexdigest())
    async with pg_manager.get_async_session_context() as session:
        exists = (
            await session.execute(
                select(LiteraturePaper.id).where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
        scope = current_access()
        if exists is not None and scope is not None and rid not in scope.rids:
            raise ValueError("导入记录与不可访问来源冲突")
        if exists is None:
            session.add(
                LiteraturePaper(
                    tenant_id=tenant_id,
                    rid=rid,
                    kind="paper",
                    doi=doi or None,
                    title=title or markdown.strip().splitlines()[0][:200],
                )
            )
            await session.commit()
    await get_minio_client().aupload_file(
        bucket_name=RENDER_BUCKET,
        object_name=f"tenants/{tenant_id}/literature/notes/{rid}/imported.md",
        data=markdown.encode("utf-8"),
        content_type="text/markdown",
    )
    return {"rid": rid, "duplicated": exists is not None}
