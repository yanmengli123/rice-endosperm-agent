"""按意图注册的确定性值渲染器（P1 值路径补齐）。

覆盖 ricekb profile/sequence 与 gene-authority 链接卡之外的三个高价值任务：

- PROTEIN_PROFILE（uniprot_search_rest / uniprot_entry_rest）：蛋白值卡
  （accession / 物种 / 长度 / 描述 / 入库类型）；
- LITERATURE_SEARCH（europe_pmc_search_rest）：题录卡（标题 / 作者 / 年份 /
  期刊 / DOI），max 展示条数受限；
- DATASET_DISCOVERY（data-aggregator search）：候选发现卡——发现级结果
  **不可作为最终事实**（capability_registry 判 DISCOVERY 不采纳），呈现为
  候选清单并引导官方核验，符合"发现层只发现、权威层才核验"的分层语义。

零幻觉纪律与 profile_projection 同源：值逐字节来自上游 envelope 的
fact manifest（``string_value``/``numeric_value``），行携带构造性 marker；
组合单元格只连接不改值。渲染失败返回 None，由调用方走通用降级表/五态，
绝不编造标签。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 每类卡片的行数上限（题录/候选是清单型交付，不是全量转储）
_MAX_LITERATURE_ROWS = 5
_MAX_DATASET_ROWS = 5

_PROTEIN_TOOL = "uniprot_search_rest"
_PROTEIN_ENTRY_TOOL = "uniprot_entry_rest"
_LITERATURE_TOOL = "europe_pmc_search_rest"
_DATASET_TOOL = "search"

_PROTEIN_KIND = "protein"
_LITERATURE_KIND = "literature"
_DATASET_KIND = "dataset_candidates"


@dataclass(frozen=True)
class TypedValueSegment:
    kind: str
    blocks: str
    used_fact_ids: tuple[str, ...]


def _value(item: Any, field: str) -> Any:
    if isinstance(item, dict):
        return item.get(field)
    return getattr(item, field, None)


def _fact_value(fact: dict) -> str | int | float | None:
    if "numeric_value" in fact:
        return fact["numeric_value"]
    return fact.get("string_value")


def _manifest_facts(source_use: Any) -> list[dict]:
    provenance = _value(source_use, "provenance") or {}
    if not isinstance(provenance, dict):
        return []
    manifest = provenance.get("fact_manifest") or {}
    return [fact for fact in (manifest.get("facts") or []) if isinstance(fact, dict)]


def _fmt(value: Any) -> str:
    return str(value)


def _marker(audit_id: int, fact: dict) -> str:
    return f"[MCP-F:{audit_id}:{fact.get('id')}]"


def _row(label: str, cells: list[str], facts: list[dict], audit_id: int) -> str:
    markers = " ".join(_marker(audit_id, fact) for fact in facts)
    return f"| {label} | {' '.join(cells)} | {markers} |"


class _FactIndex:
    """路径索引：/data/ 前缀剥离 + 中部数组索引折叠（results/0/x → results/x）。

    与 profile_projection 的差异：上游信封（uniprot/europe pmc/聚合器）的数组
    索引常出现在路径中部（``results/0/sequence/length``），不只尾部——本索引
    把纯数字段一律折叠，按出现顺序保留记录序。
    """

    def __init__(self, facts: list[dict]):
        self._entries: dict[str, list[dict]] = {}
        for fact in facts:
            path = str(fact.get("path") or "").strip("/")
            if not path:
                continue
            parts = path.split("/")
            if parts and parts[0] == "data":
                parts = parts[1:]
            folded = [part for part in parts if not part.isdigit()]
            if not folded:
                continue
            self._entries.setdefault("/".join(folded), []).append(fact)

    def field(self, *key: str) -> dict | None:
        entries = self._entries.get("/".join(key))
        return entries[0] if entries else None

    def array(self, *key: str) -> list[dict]:
        return self._entries.get("/".join(key), [])


def _succeeded_adopted(source_use: Any) -> bool:
    from yuxi.knowledge.planning.turn_execution_plan import source_use_execution_succeeded

    return source_use_execution_succeeded(source_use) and bool(_value(source_use, "adopted"))


# ── PROTEIN_PROFILE：uniprot 信封 → 蛋白值卡 ────────────────────────────────


def _render_protein(use: Any) -> str | None:
    facts = [f for f in _manifest_facts(use) if _fact_value(f) is not None]
    index = _FactIndex(facts)
    audit_id = int((_value(use, "provenance") or {}).get("mcp_call_audit_id"))

    accession = index.array("results", "primaryAccession")
    if not accession:
        accession = [index.field("primaryAccession")] if index.field("primaryAccession") else []
    if not accession:
        return None

    rows: list[str] = []
    used: list[dict] = []

    entry_types = index.array("results", "entryType")
    organisms = index.array("results", "organism", "scientificName")
    lengths = index.array("results", "sequence", "length")
    descriptions = index.array("results", "proteinDescription", "recommendedName", "fullName", "value")

    value = "、".join(f"`{_fmt(_fact_value(f))}`" for f in accession)
    rows.append(_row("UniProt accession", [value], accession, audit_id))
    used.extend(accession)

    if entry_types:
        rows.append(_row("入库类型", [" ".join(_fmt(_fact_value(f)) for f in entry_types)], entry_types, audit_id))
        used.extend(entry_types)
    if organisms:
        rows.append(_row("物种", [" ".join(_fmt(_fact_value(f)) for f in organisms)], organisms, audit_id))
        used.extend(organisms)
    if lengths:
        cells = []
        for fact in lengths:
            number = _fact_value(fact)
            cells.append(f"{_fmt(number)} aa")
        rows.append(_row("蛋白长度", cells, lengths, audit_id))
        used.extend(lengths)
    if descriptions:
        rows.append(_row("蛋白描述", [" ".join(_fmt(_fact_value(f)) for f in descriptions)], descriptions, audit_id))
        used.extend(descriptions)

    lines = ["| 项目 | 值 | 引用 |", "| --- | --- | --- |", *rows]
    return "\n".join(lines)


# ── LITERATURE_SEARCH：europe pmc 信封 → 题录卡 ──────────────────────────────


def _render_literature(use: Any) -> str | None:
    facts = [f for f in _manifest_facts(use) if _fact_value(f) is not None]
    index = _FactIndex(facts)
    audit_id = int((_value(use, "provenance") or {}).get("mcp_call_audit_id"))

    titles = index.array("results", "title")
    if not titles:
        return None

    years = index.array("results", "pubYear")
    journals = index.array("results", "journalTitle")
    dois = index.array("results", "doi")
    authors = index.array("results", "authorString")

    rows: list[str] = []
    for position in range(min(len(titles), _MAX_LITERATURE_ROWS)):
        factsForRow = [titles[position]]
        cells = [_fmt(_fact_value(titles[position]))]
        if position < len(years):
            cells.append(f"（{_fmt(_fact_value(years[position]))}）")
            factsForRow.append(years[position])
        if position < len(journals) and str(_fact_value(journals[position]) or "").strip():
            cells.append(_fmt(_fact_value(journals[position])))
            factsForRow.append(journals[position])
        if position < len(authors):
            author = _fmt(_fact_value(authors[position]))
            cells.append(f"— {author[:60]}{'…' if len(author) > 60 else ''}")
            factsForRow.append(authors[position])
        rows.append(f"| {position + 1} | {' '.join(cells)} | {' '.join(_marker(audit_id, f) for f in factsForRow)} |")
        if position < len(dois):
            rows.append(f"| | DOI：`{_fmt(_fact_value(dois[position]))}` | {_marker(audit_id, dois[position])} |")

    header = ["| # | 题录 | 引用 |", "| --- | --- | --- |"]
    note = f"> 共命中 {len(titles)} 条，展示前 {min(len(titles), _MAX_LITERATURE_ROWS)} 条；完整清单见调用审计。"
    return "\n".join([*header, *rows, "", note])


# ── DATASET_DISCOVERY：聚合器信封 → 候选发现卡（发现级，非最终事实） ─────────


def _render_dataset_candidates(use: Any) -> str | None:
    facts = [f for f in _manifest_facts(use) if _fact_value(f) is not None]
    index = _FactIndex(facts)
    audit_id = int((_value(use, "provenance") or {}).get("mcp_call_audit_id"))

    titles = index.array("results", "title")
    if not titles:
        return None

    sources = index.array("results", "source")
    accessions = index.array("results", "accession")
    years = index.array("results", "publicationYear")

    rows: list[str] = []
    for position in range(min(len(titles), _MAX_DATASET_ROWS)):
        factsForRow = [titles[position]]
        cells = []
        if position < len(sources):
            cells.append(f"[{_fmt(_fact_value(sources[position]))}]")
            factsForRow.append(sources[position])
        if position < len(accessions):
            cells.append(f"`{_fmt(_fact_value(accessions[position]))}`")
            factsForRow.append(accessions[position])
        cells.append(_fmt(_fact_value(titles[position])))
        if position < len(years):
            cells.append(f"（{_fmt(_fact_value(years[position]))}）")
            factsForRow.append(years[position])
        rows.append(f"| {position + 1} | {' '.join(cells)} | {' '.join(_marker(audit_id, f) for f in factsForRow)} |")

    header = ["| # | 候选数据集 | 引用 |", "| --- | --- | --- |"]
    # 发现层语义边界：候选不是核验事实，必须引导官方接口确认
    note = (
        f"> 以上为发现层候选（共 {len(titles)} 条，展示前 {min(len(titles), _MAX_DATASET_ROWS)} 条），"
        "未经官方接口核验；请点名单一数据源（如 NCBI / PRIDE）确认后发布为核验值。"
    )
    return "\n".join([*header, *rows, "", note])


# ── 注册表式入口（RendererRegistry 的最小形态：意图×工具 → 渲染函数） ────────

_RENDERERS: dict[str, tuple[tuple[str, ...], Any]] = {
    _PROTEIN_KIND: ((_PROTEIN_TOOL, _PROTEIN_ENTRY_TOOL), _render_protein),
    _LITERATURE_KIND: ((_LITERATURE_TOOL,), _render_literature),
    _DATASET_KIND: ((_DATASET_TOOL,), _render_dataset_candidates),
}


def render_typed_value_segment(
    source_uses: list[Any] | None,
    *,
    kind: str,
    server_hint: str | None = None,
) -> TypedValueSegment | None:
    """按意图类型渲染首个成功采纳调用的值卡；不适用返回 None。"""
    entry = _RENDERERS.get(kind)
    if entry is None:
        return None
    tools, render = entry
    for use in list(source_uses or []):
        operation = str(_value(use, "operation") or "")
        provider = str(_value(use, "provider_id") or "")
        if operation not in tools:
            continue
        if server_hint and provider != server_hint:
            continue
        if not _succeeded_adopted(use):
            continue
        blocks = render(use)
        if blocks:
            facts = _manifest_facts(use)
            return TypedValueSegment(
                kind=kind,
                blocks=blocks,
                used_fact_ids=tuple(str(f.get("id")) for f in facts),
            )
    return None


# 意图任务名 → 渲染类型（TurnExecutionPlan 任务枚举到渲染注册的桥）
_TASK_KIND_MAP: dict[str, str] = {
    # 蛋白查询当前归入 ENTITY_PROFILE（模型窄门控驱动），按工具存在性渲染
    "LITERATURE_DISCOVERY": _LITERATURE_KIND,
    "DATASET_DISCOVERY": _DATASET_KIND,
}


def render_for_task(
    task_value: str,
    source_uses: list[Any] | None,
    *,
    server_hint: str | None = None,
) -> TypedValueSegment | None:
    kind = _TASK_KIND_MAP.get(str(task_value or "").upper())
    if kind is None:
        return None
    return render_typed_value_segment(source_uses, kind=kind, server_hint=server_hint)


__all__ = [
    "TypedValueSegment",
    "render_for_task",
    "render_typed_value_segment",
]
