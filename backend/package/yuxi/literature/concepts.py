"""概念页聚合与主题 MOC（G 补集；语义移植自 paperkb 概念惰性聚合 + topic 编译）。

上游语义：L1 编译产出 concepts [{name, definition}]；概念在 **≥N 篇文献出现**
时惰性聚合成概念页（N=2 起步，上游 3+ 为含定义门槛，此处 2+ 聚合、3+ 标
mature）。数据来源两级：``notes/{rid}/L1_data.json``（编译时落库的结构化
concepts，优先）→ ``L1_note.md`` 的「## 核心概念」段解析（兜底，兼容历史产物）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select

from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteraturePaper

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
    for object_name in (f"tenants/{tenant_id}/literature/notes/{rid}/L1_data.json",):
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
        note = (
            await minio.adownload_file(RENDER_BUCKET, f"tenants/{tenant_id}/literature/notes/{rid}/L1_note.md")
        ).decode("utf-8")
        return _parse_concepts_from_note(note)
    except Exception:  # noqa: BLE001 - 无 L1 笔记
        return []


async def aggregate_concepts(tenant_id: int) -> list[dict[str, Any]]:
    """聚合租户内概念页：[{concept, definition, papers, mature}]（≥MIN_PAPERS）。"""
    async with pg_manager.get_async_session_context() as session:
        papers = (
            (await session.execute(select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
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
        papers = (
            (await session.execute(select(LiteraturePaper).where(LiteraturePaper.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
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
            try:
                note = (
                    await minio.adownload_file(
                        RENDER_BUCKET, f"tenants/{tenant_id}/literature/notes/{paper.rid}/L1_note.md"
                    )
                ).decode("utf-8")
                matched.append((paper.rid, note))
            except Exception:  # noqa: BLE001 - 无笔记跳过
                continue
    if not matched:
        raise ValueError(f"主题「{topic}」零匹配（无概念/标题命中，或相关文献未编译 L1）")

    blocks = "\n\n".join(f"## {rid}\n{note}" for rid, note in matched[:20])
    prompt = (
        f"你是文献主题导读（MOC）写作助手。主题：{topic}。基于以下文献的结构化笔记，"
        "写一篇主题导读（该主题是什么/代表性工作与结论对比/方法谱系/研究缺口，"
        "引用文献用「文献N（rid）」形式）。\n\n"
        f"{blocks}\n\n直接输出 Markdown。"
    )
    from yuxi import config
    from yuxi.models.chat import select_model  # noqa: PLC0415 - 延迟导入防循环

    model = select_model(model_spec=config.default_model)
    response = await model.call([{"role": "user", "content": prompt}], stream=False)
    content = str(response.content or "").strip()
    if not content:
        raise ValueError("主题 MOC 生成为空")

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from yuxi.storage.postgres.models_knowledge import LiteratureReport

    async with pg_manager.get_async_session_context() as session:
        stmt = (
            pg_insert(LiteratureReport)
            .values(
                tenant_id=tenant_id,
                report_kind="topic_moc",
                title=f"主题导读：{topic}",
                content=content,
                created_by=created_by,
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
