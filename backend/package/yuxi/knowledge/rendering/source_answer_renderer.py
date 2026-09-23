"""读取边界的确定性答案渲染变换（零模型调用、信息保损可逆）。

架构公理（与门禁体系共用，违反任何一条即事故）：

A1 只处理已过终态门禁的文本（PASSED/DEGRADED）；渲染发生在**读取边界**
   （get_thread_history_view），发布时只发 trace 事件记录渲染器可用性，
   DB 永远存带 marker 的已验证原文——渲染产物绝不回流模型历史。
A2 变换零生成：纯函数（正则/字典投影），绝不调用模型、绝不新增内容。
A3 信息保损可逆：T1 上标↔marker 双射，引用清单（L1 级）恒含全部原标记；
   L1 不依赖任何外部数据，任何情况下可完整还原原文。
A5 血缘单一：L2 增强（事实路径→人类字段名 + 值摘要）只来自 run runtime
   的 source_manifest 投影，查询失败静默退回 L1。
A6 失败回退：任何异常原样返回输入文本——格式层绝不阻断历史回看。
A7 禁止服务端代引：不为任何行补标记、不猜测未知 path 的字段名。

不做红线：不删行、不改值、不并句、不总结、不改数字格式。
溯源 token（T3）只做零截断包裹（前端可按 class 收纳样式），字节原样保留。
"""

from __future__ import annotations

import re
from typing import Any

from yuxi.utils import logger

RENDERER_VERSION = "source-answer-renderer.v2"

# 与 source_output_guard._FACT_MARKER 同口径（自持副本，避免渲染层↔门禁层耦合）
_FACT_MARKER = re.compile(r"\[MCP-F:(\d+):(f_[0-9a-f]{16})\]", re.I)
_SUP_REF = re.compile(r'<sup class="yuxi-ref">\[(\d+)\]</sup>')
_APPENDIX_HEADER = "**引用清单**"

# T6：SOURCE-ONLY 声明行（允许标题前缀；错位时归位到首行）
_SOURCE_ONLY_DECL_LINE = re.compile(r"^[ \t]*#*[ \t]*数据模式[ \t]*[：:][ \t]*SOURCE-ONLY[ \t]*$", re.I)

# T3：溯源类 token（零截断包裹）
_HASH_TOKEN = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{64}(?![0-9a-fA-F])")
_IMPORT_RUN_TOKEN = re.compile(r"\bsource-databases-v1-[0-9a-f]{16}\b")
_PROVENANCE_REF_TOKEN = re.compile(r"\bsource_[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+:\d+\b")

# T5：FASTA 碱基行（≥40 连续碱基；同一 run 内 ≥2 行连续才折叠）
_FASTA_LINE = re.compile(r"^[ACGTUNacgtn]{40,}$")

# T2/T7：事实路径 → 人类字段名（最长后缀匹配；未命中返回 None，禁止猜测）
_PATH_LABELS: dict[str, str] = {
    "identity/entity_key": "实体键",
    "identity/canonical_rap_id": "规范 RAP ID",
    "identity/description": "描述",
    "identity/matched_identifiers": "命中标识符",
    "identity/matched_namespaces": "命中命名空间",
    "identity/source_databases": "收录数据库",
    "identity/source_database_count": "收录库数",
    "identity/source_record_count": "源记录数",
    "identity/source_coverage_score": "源覆盖度",
    "identity/source_coverage_note": "覆盖度口径说明",
    "primaryAccession": "UniProt accession",
    "uniProtkbId": "UniProtKB 条目",
    "geneName/value": "基因符号",
    "organism/scientificName": "物种",
    "organism/taxonId": "NCBI Taxonomy ID",
    "gene/gene_id": "NCBI Gene ID",
    "gene_id": "NCBI Gene ID",
    "reports/symbol": "NCBI 基因符号",
    "symbol_resolution/gene_ids": "解析到的 NCBI Gene ID",
    "gene/symbol": "NCBI 基因符号",
    "gene/description": "NCBI 基因描述",
    "proteinDescription/recommendedName/fullName/value": "推荐蛋白名称",
    "sequence/length": "蛋白长度",
    "locations/chromosome": "染色体",
    "locations/start": "起始坐标",
    "locations/end": "终止坐标",
    "locations/strand": "链方向",
    "locations/locus": "位点",
    "locations/coordinate_system": "坐标系",
    "locations/assembly": "组装版本",
    "transcripts/transcript_id": "转录本 ID",
    "annotations/go": "GO 注释",
    "annotations/interpro": "InterPro 注释",
    "annotations/pfam": "Pfam 注释",
    "annotations/msu_annotation": "MSU 注释",
    "annotations/expression": "表达注释",
    "annotations/subcellular_location": "亚细胞定位",
    "compare/agreements/field": "跨源一致字段",
    "compare/agreements/value": "跨源一致值",
    "compare/agreements/sources": "跨源一致来源",
    "compare/conflicts": "跨源冲突",
    "qc/": "QC 派生值",
    "evidence_refs/provenance_ids": "溯源行",
    "evidence_refs/total": "溯源行数",
    "contract_notes": "契约说明",
    "names/": "名称与别名",
    "xrefs/": "跨库对照",
    "sequence_id": "序列 ID",
    "sequence_type": "序列类型",
    "sequence_length": "序列长度",
    "sequence_sha256": "序列 SHA256",
    "source_table": "源表",
    "source_database": "来源库",
    "source_schema": "来源 schema",
    "source_record_id": "源记录号",
    "content_hash": "内容哈希",
    "provenance_id": "溯源 ID",
    "source_import_run_id": "导入批次",
    "description": "描述",
    "status": "状态",
}

# T7：降级表分组的业务域顺序（path 前缀 → 域；越小越靠前）
_DOMAIN_ORDER: dict[str, int] = {
    "identity": 0,
    "names": 1,
    "locations": 2,
    "transcripts": 3,
    "annotations": 4,
    "qc": 5,
    "compare": 6,
    "xrefs": 7,
    "evidence_refs": 8,
    "sequence": 9,
    "contract_notes": 10,
    "provenance": 11,
}

_DOMAIN_TITLES: dict[int, str] = {
    0: "标识与收录",
    1: "名称与别名",
    2: "坐标",
    3: "转录本",
    4: "功能注释",
    5: "QC 派生值",
    6: "跨源比对",
    7: "跨库对照",
    8: "行级溯源",
    9: "序列摘要",
    10: "契约说明",
    11: "溯源元信息",
    99: "其他事实",
}


def label_for_path(path: str) -> str | None:
    """事实路径 → 人类字段名（最长后缀匹配）；未命中返回 None（禁止猜测）。"""
    normalized = "/" + str(path or "").strip("/")
    without_indexes = "/" + "/".join(part for part in normalized.strip("/").split("/") if not part.isdigit())
    candidates = (normalized, without_indexes) if without_indexes != normalized else (normalized,)
    best: tuple[int, str] | None = None
    for suffix, label in _PATH_LABELS.items():
        key = "/" + suffix
        for candidate in candidates:
            if suffix.endswith("/"):
                # 前缀型键（如 "qc/"）：匹配任意该前缀下的路径
                if candidate.startswith(key) and (best is None or len(key) > best[0]):
                    best = (len(key), label)
            elif (candidate == key or candidate.endswith(key)) and (best is None or len(key) > best[0]):
                best = (len(key), label)
    return best[1] if best else None


def domain_key_for_path(path: str) -> int:
    """事实路径 → 降级表分组域序号（T7 排序键；路径的 data/ 信封前缀剥除后匹配）。"""
    normalized = str(path or "").strip("/")
    if normalized.startswith("data/"):
        normalized = normalized[len("data/") :]
    without_indexes = "/".join(part for part in normalized.split("/") if not part.isdigit())
    for prefix, order in _DOMAIN_ORDER.items():
        if normalized.startswith(prefix):
            return order
    # 官方 REST 原始载荷保留供应商 schema（通常位于 data/results/N 下），
    # 不强行改写成内部 identity 信封；这里只做确定性路径分类，确保 accession、
    # Gene ID、物种等权威标识在降级视图中优先出现。
    raw_identity_suffixes = (
        "primaryAccession",
        "uniProtkbId",
        "geneName/value",
        "organism/scientificName",
        "organism/taxonId",
        "gene/gene_id",
        "gene_id",
        "reports/symbol",
        "symbol_resolution/gene_ids",
        "gene/symbol",
        "gene/description",
        "proteinDescription/recommendedName/fullName/value",
        "sequence/length",
    )
    if without_indexes.endswith(raw_identity_suffixes):
        return _DOMAIN_ORDER["identity"]
    return 99


def domain_title(domain: int) -> str:
    return _DOMAIN_TITLES.get(domain, _DOMAIN_TITLES[99])


# ---------------------------------------------------------------- T6 声明归位


def _normalize_declaration_position(text: str) -> str:
    lines = text.split("\n")
    match_indexes = [i for i, line in enumerate(lines) if _SOURCE_ONLY_DECL_LINE.match(line)]
    if not match_indexes:
        return text
    first_non_empty = next((i for i, line in enumerate(lines) if line.strip()), 0)
    if match_indexes[0] == first_non_empty and len(match_indexes) == 1:
        return text
    # 纯位置变换：移除所有声明行，把规范声明插到首个非空行之前（值不变）。
    kept = [line for i, line in enumerate(lines) if i not in set(match_indexes)]
    insert_at = next((i for i, line in enumerate(kept) if line.strip()), 0)
    kept.insert(insert_at, "数据模式：SOURCE-ONLY")
    return "\n".join(kept)


# ---------------------------------------------------------------- T1 上标脚注


def _superscript_markers(text: str) -> tuple[str, list[str]]:
    order: list[str] = []
    index: dict[str, int] = {}

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        key = raw.lower()
        if key not in index:
            index[key] = len(order) + 1
            order.append(raw)
        return f'<sup class="yuxi-ref">[{index[key]}]</sup>'

    return _FACT_MARKER.sub(_replace, text), order


def _value_snippet(value: Any, limit: int = 48) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return text if len(text) <= limit else text[:limit] + "…"


def _render_citation_appendix(order: list[str], fact_notes: dict[Any, Any] | None) -> str:
    """引用清单整体折叠进 details（默认视图零引用装置）；L1 恒含完整原标记，
    L2 尽力补充（路径标签 + 值摘要），缺失即退回 L1。"""
    lines = ["---", "", _APPENDIX_HEADER, ""]
    notes = fact_notes or {}
    for number, raw_marker in enumerate(order, start=1):
        entry = f"- [{number}] `{raw_marker}`"
        match = _FACT_MARKER.fullmatch(raw_marker)
        if match:
            note = notes.get((int(match.group(1)), match.group(2).lower()))
            if isinstance(note, dict):
                path = str(note.get("path") or "")
                label = label_for_path(path) if path else None
                where = f"（`{path}`）" if path else ""
                if label:
                    where = f"{label}{where}"
                snippet = _value_snippet(note.get("value"))
                value_part = f" = {snippet}" if snippet else ""
                if where or value_part:
                    entry = f"- [{number}] `{raw_marker}` — {where}{value_part}".rstrip()
        lines.append(entry)
    body = "\n".join(lines)
    return (
        f'<details class="yuxi-citations">'
        f"<summary>✓ 已核验 {len(order)} 条事实 · 展开引用清单</summary>\n\n"
        f"{body}\n\n</details>"
    )


# ---------------------------------------------------------------- T3 溯源包裹


def _wrap_line_outside_code_spans(line: str) -> str:
    """对反引号代码span之外的溯源 token 做零截断包裹（字节原样保留）。"""
    parts = line.split("`")
    if len(parts) == 1:
        return _wrap_provenance_tokens(line)
    rebuilt: list[str] = []
    for position, part in enumerate(parts):
        rebuilt.append(part if position % 2 == 1 else _wrap_provenance_tokens(part))
    return "`".join(rebuilt)


def _wrap_provenance_tokens(fragment: str) -> str:
    fragment = _HASH_TOKEN.sub(lambda m: f'<span class="yuxi-prov">{m.group(0)}</span>', fragment)
    fragment = _IMPORT_RUN_TOKEN.sub(lambda m: f'<span class="yuxi-prov">{m.group(0)}</span>', fragment)
    return _PROVENANCE_REF_TOKEN.sub(lambda m: f'<span class="yuxi-prov">{m.group(0)}</span>', fragment)


# ---------------------------------------------------------------- T5 FASTA 折叠（围栏感知）


def _fasta_details(block: list[str]) -> list[str]:
    return [
        f'<details class="yuxi-fasta"><summary>展开序列（{len(block)} 行）</summary>',
        "",
        "```",
        *block,
        "```",
        "",
        "</details>",
    ]


def _fold_fenced_block(block: list[str]) -> list[str]:
    """围栏级折叠：围栏内 ≥2 行碱基或碱基总量 ≥240 时整个围栏包进 details。

    golden 实测教训：在围栏**内**逐行折叠会产生嵌套围栏错乱与悬挂行；v2 以
    围栏为原子单位处理，围栏内容字节不动。
    """
    body = block[1:-1] if len(block) >= 2 and block[-1].lstrip().startswith(("```", "~~~")) else block[1:]
    fasta_lines = [line for line in body if _FASTA_LINE.match(line.strip())]
    total_bases = sum(len(line.strip()) for line in fasta_lines)
    if len(fasta_lines) >= 2 or total_bases >= 240:
        return [
            f'<details class="yuxi-fasta"><summary>展开序列（{len(body)} 行）</summary>',
            "",
            *block,
            "",
            "</details>",
        ]
    return block


def _process_body_lines(text: str) -> str:
    """围栏感知的正文处理：围栏外做 T3 包裹与裸碱基折叠；围栏内字节不动，
    只在围栏闭合时做围栏级折叠决策。"""
    lines = text.split("\n")
    output: list[str] = []
    fence: list[str] | None = None
    bare: list[str] = []

    def _flush_bare() -> None:
        nonlocal bare
        if len(bare) >= 2 or (len(bare) == 1 and len(bare[0].strip()) >= 120):
            output.extend(_fasta_details(bare))
        else:
            output.extend(bare)
        bare = []

    for line in lines:
        stripped = line.lstrip()
        if fence is not None:
            fence.append(line)
            if stripped.startswith(("```", "~~~")):
                output.extend(_fold_fenced_block(fence))
                fence = None
            continue
        if stripped.startswith(("```", "~~~")):
            _flush_bare()
            fence = [line]
            continue
        if _FASTA_LINE.match(stripped):
            bare.append(line)
            continue
        _flush_bare()
        output.append(_wrap_line_outside_code_spans(line))
    if fence is not None:
        # 未闭合围栏：保守原样输出（不折叠、不包裹）。
        output.extend(fence)
    _flush_bare()
    return "\n".join(output)


# ---------------------------------------------------------------- 公共 API


def has_renderable_content(text: str) -> bool:
    """渲染资格：含 MCP-F 标记、FASTA 块或错位声明之一才有变换意义。"""
    source = str(text or "")
    if not source.strip():
        return False
    if _FACT_MARKER.search(source):
        return True
    if any(_FASTA_LINE.match(line.strip()) for line in source.split("\n")):
        return True
    lines = source.split("\n")
    first_non_empty = next((i for i, line in enumerate(lines) if line.strip()), None)
    declaration_indexes = [i for i, line in enumerate(lines) if _SOURCE_ONLY_DECL_LINE.match(line)]
    return bool(first_non_empty is not None and declaration_indexes and declaration_indexes[0] != first_non_empty)


def render_report(text: str) -> dict[str, Any]:
    """发布边界 trace 用的只读盘点（不做任何变换）。"""
    source = str(text or "")
    return {
        "renderer_version": RENDERER_VERSION,
        "eligible": has_renderable_content(source),
        "marker_count": len(_FACT_MARKER.findall(source)),
        "fasta_block": any(_FASTA_LINE.match(line.strip()) for line in source.split("\n")),
    }


def render_source_answer(
    text: str,
    *,
    fact_notes: dict[Any, Any] | None = None,
) -> str:
    """应用 T6→T1→围栏感知的 T3/T5 并附折叠引用清单；任何异常原样返回输入（A6）。"""
    original = str(text or "")
    try:
        if not original.strip():
            return text
        source = _normalize_declaration_position(original)
        source, order = _superscript_markers(source)
        source = _process_body_lines(source)
        if order:
            source = source.rstrip() + "\n\n" + _render_citation_appendix(order, fact_notes)
        return source
    except Exception as error:  # noqa: BLE001 —— A6：格式层绝不阻断历史回看
        logger.warning(f"Source answer render fell back to original text: {type(error).__name__}: {error}")
        return text


__all__ = [
    "RENDERER_VERSION",
    "domain_key_for_path",
    "domain_title",
    "has_renderable_content",
    "label_for_path",
    "render_report",
    "render_source_answer",
]
