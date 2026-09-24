"""服务端数据面投影（呈现层 v3）：档案表 / 序列摘要表由事实清单确定性渲染。

分工：模型只写 ≤4 句叙述（逐句过门禁）；数据表由本模块在发布时从
``source_manifest`` 的事实清单构造——这正是降级表已在生产验证的零幻觉
链路（值逐字节来自 manifest、行携带构造性 marker），从"失败模式"升级为
"成功模式的呈现器"。

公理落点：
- A7 边界：数据面行的 marker 是**构造性绑定**（值与 marker 同源于该 fact），
  不是替模型句子代引；组合单元格只做连接，单事实值逐字节照抄；
- 触发确定性：恰一次成功的 ``ricekb_gene_profile``（或 ≥1 次
  ``ricekb_sequence``）adopted 调用、单一实体、manifest 含值（PUBLIC 数据级）
  ——不满足返回 None，调用方回退现有模型草稿流，零风险；
- **合并渲染（P5）**：双工具轮（profile + sequence 都成功）**两段都渲染**，
  sequence 在前（更原子的用户请求形态）、profile 在后——不做"意图选择"，
  不引入可能出错的判断环节，单一工具命中时行为与历史完全一致；
- **取全必显全（P6）**：``_PROFILE_RENDER_SINKS`` 注册 manifest 域 → 渲染槽
  （主表/边界/折叠/显式跳过+原因），``coverage`` 四桶记账保证每个事实
  （含空值）都落桶——装配器新增域而渲染层没接，覆盖断言测试先红；
- 边界说明行只走"类别→罐头模板"（note 含"未提供"才映射，罐头句是对 note
  存在性的事实陈述而非转述）；未匹配 note 原样进折叠层，一字不改；
- 不存在的字段不列行，绝不编造标签或值；
- 组合文本（叙述+表格）由调用方整体复跑事实门禁后才发布。产物下载走客户端
  原生产物卡（run_artifacts 表权威），不在正文渲染任何产物元数据——size 等
  不是 manifest 事实，进了组合复跑必被 ``unsupported_numbers`` 打回。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from yuxi.knowledge.planning.turn_execution_plan import source_use_execution_succeeded

PROFILE_TOOL = "ricekb_gene_profile"
SEQUENCE_TOOL = "ricekb_sequence"
_MIN_PROFILE_FACTS = 8

_NOTE_CATEGORY_TEMPLATES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("regulators", "targets", "调控"), "调控关系与靶标数据 RiceKB 契约未提供。"),
    (("protein", "蛋白"), "蛋白序列字段 RiceKB 契约未提供。"),
    (("allele", "等位", "变异"), "等位变异信息 RiceKB 契约未提供。"),
)

_FASTA_HINT_RE = re.compile(r"^[ACGTUNacgtn]{120,}$")

#: P6 渲染槽注册表：manifest 域（fact path 首段）→ 处置方式。
#: main=主表行；boundary_or_folded=按 note 语义分派；skip:<原因>=显式跳过。
#: 覆盖断言测试据此保证"取全必显全"——装配器新增域未登记时按
#: ``skip:no_render_sink_registered`` 计账并在 golden 断言中显红。
_PROFILE_RENDER_SINKS: dict[str, str] = {
    "identity": "main",
    "names": "main",
    "locations": "main",
    "annotations": "main",
    "transcripts": "main",
    "references": "folded",
    "qc": "folded",
    "contract_notes": "boundary_or_folded",
    "evidence_refs": "skip:internal_provenance_ids",
    "compare": "skip:aggregation_intermediate",
    "xrefs": "skip:cross_reference_index",
    "support": "skip:not_user_facing",
}
_DEFAULT_RENDER_SINK = "skip:no_render_sink_registered"


@dataclass(frozen=True)
class ProjectionSegment:
    """单一种类的确定性投影段。"""

    kind: str  # "profile" | "sequence"
    audit_id: int
    blocks: str
    used_fact_ids: tuple[str, ...]
    coverage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DataPlaneProjection:
    """复合投影：sequence 段在前、profile 段在后（双工具轮两段都渲染）。

    ``blocks`` / ``kind`` / ``audit_id`` / ``used_fact_ids`` 保持单段时代的
    兼容形状（既有调用点与观测元数据零改动）。
    """

    segments: tuple[ProjectionSegment, ...]

    @property
    def blocks(self) -> str:
        return "\n\n".join(segment.blocks for segment in self.segments)

    @property
    def kind(self) -> str:
        return "+".join(segment.kind for segment in self.segments)

    @property
    def audit_id(self) -> int:
        return self.segments[0].audit_id if self.segments else 0

    @property
    def used_fact_ids(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for segment in self.segments:
            for fact_id in segment.used_fact_ids:
                seen.setdefault(fact_id, None)
        return tuple(seen)


def _value(item: Any, field: str) -> Any:
    if isinstance(item, dict):
        return item.get(field)
    return getattr(item, field, None)


def _fact_value(fact: dict) -> str | int | float | None:
    if "numeric_value" in fact:
        return fact["numeric_value"]
    return fact.get("string_value")


def _fmt(value: Any) -> str:
    return str(value)


def _manifest_facts(source_use: Any) -> list[dict]:
    provenance = _value(source_use, "provenance") or {}
    if not isinstance(provenance, dict):
        return []
    manifest = provenance.get("fact_manifest") or {}
    return [fact for fact in (manifest.get("facts") or []) if isinstance(fact, dict)]


def _is_successful_adopted(source_use: Any, tool: str) -> bool:
    if str(_value(source_use, "operation") or "") != tool:
        return False
    if not source_use_execution_succeeded(source_use):
        return False
    if not bool(_value(source_use, "adopted")):
        return False
    return True


class _FactIndex:
    """按路径段索引事实：/data/<prefix>/<idx?>/<field> 与 /data/<field> 两种形态。"""

    def __init__(self, facts: list[dict]):
        self._exact: dict[str, dict] = {}
        self._array: dict[str, list[tuple[int, dict]]] = {}
        for fact in facts:
            path = str(fact.get("path") or "").strip("/")
            if not path:
                continue
            parts = path.split("/")
            if parts and parts[0] == "data":
                parts = parts[1:]
            if not parts:
                continue
            if parts[-1].isdigit():
                key = "/".join(parts[:-1])
                self._array.setdefault(key, []).append((int(parts[-1]), fact))
            elif len(parts) >= 3 and parts[-2].isdigit():
                key = "/".join(parts[:-2] + [parts[-1]])
                self._array.setdefault(key, []).append((int(parts[-2]), fact))
            else:
                self._exact["/".join(parts)] = fact

    def field(self, *key: str) -> dict | None:
        return self._exact.get("/".join(key))

    def array(self, *key: str) -> list[dict]:
        entries = sorted(self._array.get("/".join(key), []), key=lambda item: item[0])
        return [fact for _, fact in entries]

    def prefixed_keys(self, *prefix: str) -> list[str]:
        """前缀扫描：返回以 prefix 开头的全部索引键（exact + array），字典序。"""
        head = "/".join(prefix)
        keys = [key for key in self._exact if key == head or key.startswith(f"{head}/")]
        keys += [key for key in self._array if key == head or key.startswith(f"{head}/")]
        return sorted(set(keys))


def _marker(audit_id: int, fact: dict) -> str:
    return f"[MCP-F:{audit_id}:{fact.get('id')}]"


def _row(label: str, cells: list[str], facts: list[dict], audit_id: int) -> str:
    markers = " ".join(_marker(audit_id, fact) for fact in facts)
    return f"| {label} | {' '.join(cells)} | {markers} |"


def _render_notes_block(notes: list[tuple[str, dict]], audit_id: int, summary_label: str) -> str:
    """折叠层逐条原样保留；每条挂其自身事实的构造性 marker（过严格门禁）。"""
    if not notes:
        return ""
    lines = [
        f'<details class="yuxi-contract-notes"><summary>{summary_label}{_marker(audit_id, notes[0][1])}</summary>',
        "",
    ]
    lines.extend(f"- {text} {_marker(audit_id, fact)}" for text, fact in notes)
    lines.extend(["", "</details>"])
    return "\n".join(lines)


def _boundary_and_notes(
    contract_note_facts: list[dict], audit_id: int
) -> tuple[list[str], list[dict], list[tuple[str, dict]]]:
    """罐头边界行（note 含"未提供"且命中类别，挂构造性 marker）+ 折叠层原样 note。

    返回 (边界行文本, 边界行事实, 折叠层 (文本, 事实))——边界事实单独返回供
    四桶记账归入 boundary 桶。
    """
    boundary: list[str] = []
    boundary_facts: list[dict] = []
    folded: list[tuple[str, dict]] = []
    seen_boundary: set[str] = set()
    for fact in contract_note_facts:
        text = str(fact.get("string_value") or "").strip()
        if not text:
            continue
        if "未提供" not in text:
            folded.append((text, fact))
            continue
        for keywords, template in _NOTE_CATEGORY_TEMPLATES:
            if any(keyword in text for keyword in keywords):
                if template not in seen_boundary:
                    seen_boundary.add(template)
                    boundary.append(f"{template}{_marker(audit_id, fact)}")
                    boundary_facts.append(fact)
                break
        else:
            folded.append((text, fact))
    return boundary, boundary_facts, [note for note in folded if note]


def _names_alias_rows(index: _FactIndex, audit_id: int) -> list[tuple[str, str, list[dict]]]:
    """names/<NS>/{symbol,synonyms/<i>} → 每命名空间一行「别名（NS）」。

    返回 (行标签, 单元格文本, 事实列表)；值逐字节来自 manifest（同义词连接
    不改值）。symbol 与匹配符号重复时仍列出（命名空间口径不同）。
    """
    rows: list[tuple[str, str, list[dict]]] = []
    namespaces: dict[str, dict[str, Any]] = {}
    for key in index.prefixed_keys("names"):
        parts = key.split("/")
        if len(parts) != 3:
            continue
        _, namespace, field_name = parts
        entry = namespaces.setdefault(namespace, {"symbol": None, "synonyms": []})
        if field_name == "symbol":
            symbol_fact = index.field(*parts)
            if symbol_fact is not None and _fact_value(symbol_fact) is not None:
                entry["symbol"] = symbol_fact
        elif field_name == "synonyms":
            entry["synonyms"].extend(index.array(*parts))
    for namespace in sorted(namespaces):
        entry = namespaces[namespace]
        synonym_facts = [fact for fact in entry["synonyms"] if _fact_value(fact) is not None]
        symbol_fact = entry["symbol"]
        if not synonym_facts and symbol_fact is None:
            continue
        fragments: list[str] = []
        facts: list[dict] = []
        if synonym_facts:
            fragments.append("、".join(f"`{_fmt(_fact_value(fact))}`" for fact in synonym_facts))
            facts.extend(synonym_facts)
        if symbol_fact is not None:
            fragments.append(f"标准符号 `{_fmt(_fact_value(symbol_fact))}`")
            facts.append(symbol_fact)
        rows.append((f"别名（{namespace}）", " ".join(fragments), facts))
    return rows


def _references_folded(index: _FactIndex, audit_id: int) -> tuple[str, list[dict]]:
    """references/<i>/{pubmed_id,title,journal,year} → 折叠层「参考文献」。"""
    pubmed_ids = index.array("references", "pubmed_id")
    if not pubmed_ids:
        return "", []
    titles = index.array("references", "title")
    journals = index.array("references", "journal")
    years = index.array("references", "year")
    lines: list[str] = []
    used: list[dict] = []
    for position in range(len(pubmed_ids)):
        facts = [pubmed_ids[position]]
        head = f"PMID {_fmt(_fact_value(pubmed_ids[position]))}"
        if position < len(titles):
            head = f"{head} — {_fmt(_fact_value(titles[position]))}"
            facts.append(titles[position])
        meta_bits: list[str] = []
        if position < len(journals):
            meta_bits.append(_fmt(_fact_value(journals[position])))
            facts.append(journals[position])
        if position < len(years):
            meta_bits.append(_fmt(_fact_value(years[position])))
            facts.append(years[position])
        if meta_bits:
            head = f"{head}（{', '.join(meta_bits)}）"
        lines.append(f"- {head} {' '.join(_marker(audit_id, fact) for fact in facts)}")
        used.extend(facts)
    block = (
        f'<details class="yuxi-references"><summary>参考文献（{len(pubmed_ids)} 篇）'
        f"{_marker(audit_id, pubmed_ids[0])}</summary>\n\n" + "\n".join(lines) + "\n\n</details>"
    )
    return block, used


def _qc_folded(index: _FactIndex, audit_id: int) -> tuple[str, list[dict]]:
    """qc/<i>/{rule,value,unit,formula} → 折叠层「跨源一致性核验」（值照抄 qc 记录）。"""
    rules = index.array("qc", "rule")
    if not rules:
        return "", []
    values = index.array("qc", "value")
    units = index.array("qc", "unit")
    formulas = index.array("qc", "formula")
    lines: list[str] = []
    used: list[dict] = []
    for position in range(len(rules)):
        facts = [rules[position]]
        cells = [_fmt(_fact_value(rules[position]))]
        if position < len(values):
            cell = f"{_fmt(_fact_value(values[position]))}"
            if position < len(units):
                cell = f"{cell} {_fmt(_fact_value(units[position]))}"
                facts.append(units[position])
            if position < len(formulas):
                cell = f"{cell}（{_fmt(_fact_value(formulas[position]))}）"
                facts.append(formulas[position])
            cells.append(cell)
            facts.append(values[position])
        lines.append(f"- {': '.join(cells)} {' '.join(_marker(audit_id, fact) for fact in facts)}")
        used.extend(facts)
    block = (
        f'<details class="yuxi-qc"><summary>跨源一致性核验（{len(rules)} 条）'
        f"{_marker(audit_id, rules[0])}</summary>\n\n" + "\n".join(lines) + "\n\n</details>"
    )
    return block, used


def _location_rows(index: _FactIndex, audit_id: int, collector: list[dict]) -> list[str]:
    """位置行：每条 location 一行，组合单元格只连接不改值（数字原样）。"""
    chromosomes = index.array("locations", "chromosome")
    if not chromosomes:
        return []
    starts = index.array("locations", "start")
    ends = index.array("locations", "end")
    strands = index.array("locations", "strand")
    loci = index.array("locations", "locus")
    rows = []
    for position in range(len(chromosomes)):
        parts: list[str] = []
        facts = [chromosomes[position]]
        if position < len(starts) and position < len(ends):
            span = f"{_fmt(_fact_value(starts[position]))}–{_fmt(_fact_value(ends[position]))}"
            if position < len(strands):
                span = f"{span}（{_fmt(_fact_value(strands[position]))}）"
                facts.append(strands[position])
            parts.append(span)
            facts.extend([starts[position], ends[position]])
        elif position < len(strands):
            parts.append(f"（{_fmt(_fact_value(strands[position]))}）")
            facts.append(strands[position])
        parts.insert(0, _fmt(_fact_value(chromosomes[position])))
        if position < len(loci):
            locus_value = _fmt(_fact_value(loci[position])).strip()
            # 上游存在空字符串 locus（golden 实测 RAP_DB 位置行）——空值不渲染
            if locus_value:
                parts.append(f"MSU `{locus_value}`")
                facts.append(loci[position])
        label = "位置" if position == 0 else "位置（续）"
        rows.append(_row(label, [" ".join(parts)], facts, audit_id))
        collector.extend(facts)
    return rows


def _render_profile(use: Any, facts: list[dict], all_facts: list[dict]) -> ProjectionSegment | None:
    index = _FactIndex(facts)
    audit_id = int((_value(use, "provenance") or {}).get("mcp_call_audit_id"))
    entity = index.field("identity", "entity_key")
    if entity is None:
        return None

    rows: list[str] = []
    used: list[dict] = []

    canonical = index.field("identity", "canonical_rap_id")
    if canonical is not None:
        rows.append(_row("规范 RAP ID", [f"`{_fmt(_fact_value(canonical))}`"], [canonical], audit_id))
        used.append(canonical)

    identifiers = index.array("identity", "matched_identifiers")
    namespaces = index.array("identity", "matched_namespaces")
    if identifiers:
        value = "、".join(f"`{_fmt(_fact_value(f))}`" for f in identifiers)
        if namespaces:
            ns = "、".join(_fmt(_fact_value(f)) for f in namespaces)
            value = f"{value}（{ns}）"
        rows.append(_row("匹配符号", [value], [*identifiers, *namespaces], audit_id))
        used.extend([*identifiers, *namespaces])

    description = index.field("identity", "description")
    if description is not None:
        rows.append(_row("描述", [_fmt(_fact_value(description))], [description], audit_id))
        used.append(description)

    # 别名（P6）：names/<NS>/{symbol,synonyms} → 主表行（值照抄）
    for label, cell_text, facts_for_row in _names_alias_rows(index, audit_id):
        rows.append(_row(label, [cell_text], facts_for_row, audit_id))
        used.extend(facts_for_row)

    rows.extend(_location_rows(index, audit_id, used))

    for field_key, label in (
        ("go", "GO 注释"),
        ("interpro", "InterPro 注释"),
        ("pfam", "Pfam 注释"),
        ("msu_annotation", "MSU 注释"),
        ("expression", "表达注释"),
    ):
        annotation = index.field("annotations", field_key)
        if annotation is not None:
            rows.append(_row(label, [_fmt(_fact_value(annotation))], [annotation], audit_id))
            used.append(annotation)

    databases = index.array("identity", "source_databases")
    record_count = index.field("identity", "source_record_count")
    coverage_facts = [*databases]
    if databases:
        value = "、".join(_fmt(_fact_value(f)) for f in databases)
        if record_count is not None:
            value = f"{value}（{_fmt(_fact_value(record_count))} 条源记录）"
            coverage_facts.append(record_count)
        rows.append(_row("收录情况", [value], coverage_facts, audit_id))
        used.extend(coverage_facts)

    transcripts = index.array("transcripts", "transcript_id")
    if transcripts:
        value = "、".join(f"`{_fmt(_fact_value(f))}`" for f in transcripts)
        rows.append(_row("转录本", [value], transcripts, audit_id))
        used.extend(transcripts)

    if not rows:
        return None

    boundary, boundary_facts, folded = _boundary_and_notes(index.array("contract_notes"), audit_id)
    blocks = ["\n".join(["| 项目 | 值 | 引用 |", "| --- | --- | --- |", *rows])]
    if boundary:
        blocks.append("> " + " ".join(boundary))
    notes_block = _render_notes_block(folded, audit_id, "契约说明（原样保留）")
    if notes_block:
        blocks.append(notes_block)

    references_block, references_used = _references_folded(index, audit_id)
    if references_block:
        blocks.append(references_block)
        used.extend(references_used)
    qc_block, qc_used = _qc_folded(index, audit_id)
    if qc_block:
        blocks.append(qc_block)
        used.extend(qc_used)

    coverage = _account_coverage(
        all_facts,
        main=used,
        boundary=boundary_facts,
        folded=[*[fact for _text, fact in folded], *references_used, *qc_used],
    )
    return ProjectionSegment(
        kind="profile",
        audit_id=audit_id,
        blocks="\n\n".join(blocks),
        used_fact_ids=tuple(str(fact.get("id")) for fact in used),
        coverage=coverage,
    )


def _render_sequence(uses: list[Any]) -> ProjectionSegment | None:
    rows: list[str] = []
    verify_lines: list[str] = []
    used: list[dict] = []
    first_audit_id: int | None = None
    for use in uses:
        facts = [f for f in _manifest_facts(use) if _fact_value(f) is not None]
        index = _FactIndex(facts)
        audit_id = int((_value(use, "provenance") or {}).get("mcp_call_audit_id"))
        if first_audit_id is None:
            first_audit_id = audit_id
        seq_id = index.field("sequence_id")
        if seq_id is None:
            continue
        seq_type_fact = index.field("sequence_type")
        seq_type = _fmt(_fact_value(seq_type_fact)) if seq_type_fact is not None else ""
        length = index.field("sequence_length")
        description = index.field("description")

        # v4 值视图：主表只有 序列/类型/长度/描述；sha256 与源表:行下沉折叠
        # "核验明细"（哈希是校验材料不是答案内容，完整性锚点由折叠层+产物清单块承担）。
        cells = [f"`{_fmt(_fact_value(seq_id))}`"]
        row_facts = [seq_id]
        if seq_type_fact is not None:
            cells.append(seq_type)
            row_facts.append(seq_type_fact)
        if length is not None:
            unit = "nt" if seq_type == "transcript" else "bp"
            cells.append(f"{_fmt(_fact_value(length))} {unit}")
            row_facts.append(length)
        if description is not None:
            cells.append(_fmt(_fact_value(description)))
            row_facts.append(description)
        rows.append(f"| {' | '.join(cells)} | {' '.join(_marker(audit_id, f) for f in row_facts)} |")
        used.extend(row_facts)

        sha = index.field("sequence_sha256")
        if sha is not None:
            verify_lines.append(f"- 序列 SHA256：`{_fmt(_fact_value(sha))}` {_marker(audit_id, sha)}")
            used.append(sha)
        table = index.field("source_table")
        record = index.field("source_record_id")
        if table is not None:
            cell = _fmt(_fact_value(table))
            table_facts = [table]
            if record is not None:
                cell = f"{cell}:{_fmt(_fact_value(record))}"
                table_facts.append(record)
            verify_lines.append(f"- 源表:行：`{cell}` {' '.join(_marker(audit_id, f) for f in table_facts)}")
            used.extend(table_facts)
    if not rows or first_audit_id is None:
        return None
    header = "| 序列 | 类型 | 长度 | 描述 | 引用 |"
    separator = "| --- | --- | --- | --- | --- |"
    blocks = ["\n".join([header, separator, *rows])]
    if verify_lines:
        footer_marker = _marker(first_audit_id, used[-1]) if used else ""
        blocks.append(
            f'<details class="yuxi-citations"><summary>核验明细 {footer_marker}'.rstrip()
            + "</summary>\n\n"
            + "\n".join(verify_lines)
            + "\n\n</details>"
        )
    # 完整 FASTA 的下载入口由产物清单块（全门禁之后追加）承载，不在投影内造
    # 散文指针——投影文本必须 100% 过事实门禁。
    return ProjectionSegment(
        kind="sequence",
        audit_id=first_audit_id,
        blocks="\n\n".join(blocks),
        used_fact_ids=tuple(str(fact.get("id")) for fact in used),
        coverage={"main": [str(f.get("id")) for f in used], "boundary": [], "folded": [], "skipped": []},
    )


def _account_coverage(
    all_facts: list[dict],
    *,
    main: list[dict],
    boundary: list[dict],
    folded: list[dict],
) -> dict[str, Any]:
    """四桶记账（P6）：{main, boundary, folded, skipped:[(id, reason)]}。

    并集恒等于 manifest 全集（含空值事实）：空值 → null_or_digest_only；
    已渲染/已折叠之外的非空事实按域注册表给显式跳过原因——装配器新增域
    未登记时按 no_render_sink_registered 计账，覆盖断言测试据此显红。
    """
    main_ids = {str(f.get("id")) for f in main}
    boundary_ids = {str(f.get("id")) for f in boundary}
    folded_ids = {str(f.get("id")) for f in folded}
    skipped: list[list[str]] = []
    for fact in all_facts:
        fact_id = str(fact.get("id"))
        if fact_id in main_ids or fact_id in boundary_ids or fact_id in folded_ids:
            continue
        if _fact_value(fact) is None:
            skipped.append([fact_id, "null_or_digest_only"])
            continue
        path = str(fact.get("path") or "").strip("/")
        parts = path.split("/")
        if parts and parts[0] == "data":
            parts = parts[1:]
        domain = parts[0] if parts else ""
        sink = _PROFILE_RENDER_SINKS.get(domain, _DEFAULT_RENDER_SINK)
        if sink.startswith("skip:"):
            skipped.append([fact_id, sink.removeprefix("skip:")])
        else:
            # 注册为主表/边界/折叠但当前渲染未消费（如 identity/entity_key 仅作触发）
            skipped.append([fact_id, f"registered_{sink}_but_unrendered"])
    return {
        "main": sorted(main_ids),
        "boundary": sorted(boundary_ids),
        "folded": sorted(folded_ids),
        "skipped": skipped,
    }


def project_data_plane(source_uses: list[Any] | None) -> DataPlaneProjection | None:
    """确定性触发：序列轮（≥1 次 sequence）与档案轮（恰一次 gene_profile）**合并渲染**。

    双工具轮两段都出（sequence 在前——更原子的用户请求形态；profile 在后——
    聚合档案）；单一工具命中时行为与历史一致；都不满足返回 None。
    """
    uses = list(source_uses or [])
    segments: list[ProjectionSegment] = []

    sequence_segment = _render_sequence([u for u in uses if _is_successful_adopted(u, SEQUENCE_TOOL)])
    if sequence_segment is not None:
        segments.append(sequence_segment)

    profile_uses = [u for u in uses if _is_successful_adopted(u, PROFILE_TOOL)]
    if len(profile_uses) == 1:
        facts = [f for f in _manifest_facts(profile_uses[0]) if _fact_value(f) is not None]
        entity_keys = {_fmt(_fact_value(f)) for f in facts if str(f.get("path") or "").endswith("identity/entity_key")}
        if len(facts) >= _MIN_PROFILE_FACTS and len(entity_keys) == 1:
            segment = _render_profile(profile_uses[0], facts, _manifest_facts(profile_uses[0]))
            if segment is not None:
                segments.append(segment)

    if not segments:
        return None
    return DataPlaneProjection(segments=tuple(segments))


__all__ = [
    "DataPlaneProjection",
    "ProjectionSegment",
    "PROFILE_TOOL",
    "SEQUENCE_TOOL",
    "project_data_plane",
]
