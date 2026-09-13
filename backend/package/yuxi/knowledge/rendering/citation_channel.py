"""确定性引用通道（citation channel）：页码/锚点由后端渲染，模型只写 [E#] 提议。

架构不变量（违反即事故，2026-09 三案例的教训）：

1. **用户可见页码 == 已验证证据的 anchor.page**。``[E#]`` 与模型手写芯片都只是
   *提议*；只有经过 :mod:`claim_evidence_resolver` 绑定验证（VERIFIED）的提议
   才展开为权威芯片，其余一律剥离——"真实但错误的页码"也绝不上屏。
2. **没有 VERIFIED 绑定 == 没有精确页码**。失败关闭，显示
   「当前无法可靠定位原文页码」；retrieval candidate 列表绝不倾倒给用户
   （候选页码都是真的，但都不回答"这句话在哪页"）。
3. **模型自由文本中的页码永远不是权威**。裸页码/锚点 ID 一律剥离；locator
   模式下替换为后端确定性定位芯片。
4. **展示通道 ≠ 模型通道**。渲染产物（芯片/附录）绝不回流进模型历史——
   :func:`sanitize_history_text` 把它们折叠为 ``[citation omitted]``，
   打断"模型模仿芯片 → 守卫剥离 → 用户看到乱码"的自腐蚀循环。
5. **血统一致性**。锚点页码必须落在载体 chunk 的页码范围内（跨页 chunk 合法，
   ``anchor.page ∈ chunk.pages``）；不一致 → ``locatable=False``。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select, tuple_

from yuxi.knowledge.contracts.citation_binding import CitationBindingCandidate
from yuxi.knowledge.evidence.document_partition import (
    PARTITION_APPENDIX,
    PARTITION_MAIN_TEXT,
    PARTITION_REFERENCES,
    PARTITION_SUPPORTING_INFO,
    effective_partition,
)
from yuxi.knowledge.rendering.claim_evidence_resolver import (
    BINDING_VERIFIED,
    extract_hard_constraints,
    normalize_for_match,
    resolve_binding,
)
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    KnowledgeFile,
    KnowledgeParseRevision,
)
from yuxi.utils import logger

CITATION_CHANNEL_VERSION = "citation_channel_v3"

MAX_CITATIONS = 12
MAX_ANCHORS_PER_CITATION = 4
CHIP_FILENAME_CHARS = 24
_QUOTE_FINGERPRINT_CHARS = 80
# 归一化完整引文（仅后端内部使用，永不进入模型 payload）
_QUOTE_NORM_CHARS = 4000

ZONE_MAIN_TEXT = PARTITION_MAIN_TEXT
ZONE_SUPPORTING_INFO = PARTITION_SUPPORTING_INFO
ZONE_LABELS = {
    ZONE_MAIN_TEXT: "正文",
    ZONE_SUPPORTING_INFO: "补充材料",
    PARTITION_REFERENCES: "参考文献",
    PARTITION_APPENDIX: "附录",
}

# 失败关闭标记：没有 VERIFIED 绑定时用户看到的全部信息
NARRATIVE_LOCATOR_MARKER = "〔当前无法可靠定位原文页码〕"
LEGACY_NARRATIVE_LOCATOR_MARKER = "〔页码与锚点以后端引用为准〕"
HISTORY_CITATION_PLACEHOLDER = "[citation omitted]"

_PLACEHOLDER_PATTERN = re.compile(r"\[(E\d{1,3})\]")
# 伪造芯片：模型模仿渲染产物手写的任何「〔…证据E#…〕」形态（含嵌套损坏形态）
_FABRICATED_CHIP_PATTERN = re.compile(r"〔[^〕]*证据\s*E\d{1,3}[^〕]*〕")
_BARE_PAGE_PATTERNS = (
    re.compile(r"第\s*[0-9]{1,4}\s*页"),
    re.compile(r"\bp\.\s*[0-9]{1,3}\b", flags=re.IGNORECASE),
    re.compile(r"\bpages?\s+[0-9]{1,3}\b", flags=re.IGNORECASE),
)
_BARE_LOCATOR_ID_PATTERN = re.compile(r"\b(?:ea|ev)_[0-9a-f]{16,40}\b")
_ANCHOR_FOOTER_PATTERN = re.compile(r"【证据锚点】([^\n]+)")
_PAGE_FOOTER_PATTERN = re.compile(r"【页码】([0-9]{1,4}(?:\s*[-–]\s*[0-9]{1,4})?)")
_SI_TITLE_PATTERN = re.compile(
    r"^\s*(?:support(?:ing)?|supplementary)\s+(?:information|materials?|data)\b", flags=re.IGNORECASE
)
_TOC_LINE_PATTERN = re.compile(r"^\s*(?:fig(?:ure)?\.?|table)\s*s?\s*\d+", flags=re.IGNORECASE)
_ADJACENT_LOCATOR_MARKERS = re.compile(
    rf"{re.escape(NARRATIVE_LOCATOR_MARKER)}"
    rf"(?:\s*(?:[,，;；、/]|与|和|and)\s*{re.escape(NARRATIVE_LOCATOR_MARKER)})+"
)
_APPENDIX_LINE_PATTERN = re.compile(r"^.*证据定位（后端权威渲染）.*$", flags=re.MULTILINE)
# 占位符/芯片周围的引文上下文窗口（供 resolver 做归属判定）
_CONTEXT_BEFORE_CHARS = 260
_CONTEXT_AFTER_CHARS = 140


def extract_anchor_ids_from_content(content: Any) -> list[str]:
    """从 chunk 正文的【证据锚点】脚注提取锚点 ID（分块器写入，与锚点库同源）。"""
    ids: list[str] = []
    for match in _ANCHOR_FOOTER_PATTERN.finditer(str(content or "")):
        for part in match.group(1).replace("、", ",").split(","):
            value = part.strip()
            if value and value not in ids:
                ids.append(value)
    return ids


def parse_chunk_pages(content: Any) -> list[int] | None:
    """解析 chunk 页码脚注为页码列表（跨页 chunk 返回区间展开）；无脚注返回 None。"""
    match = _PAGE_FOOTER_PATTERN.search(str(content or ""))
    if not match:
        return None
    raw = match.group(1)
    range_match = re.match(r"^([0-9]{1,4})\s*[-–]\s*([0-9]{1,4})$", raw)
    if range_match:
        start, end = int(range_match.group(1)), int(range_match.group(2))
        if 1 <= start <= end <= start + 30:
            return list(range(start, end + 1))
        return None
    return [int(raw)] if int(raw) >= 1 else None


def is_toc_line(
    quote: Any,
    *,
    evidence_type: str | None = None,
    partition: str | None = None,
) -> bool:
    """Distinguish a real Figure/Table caption from a list-of-figures row."""
    if str(evidence_type or "").casefold() == "toc_line":
        return True
    if str(partition or "").upper() in {"TOC", "FIGURE_LIST", "TABLE_LIST"}:
        return True
    return bool(
        re.match(
            r"^\s*(?:fig(?:ure)?\.?|table)\s*s?\s*\d+.*\.{2,}\s*\d+\s*$",
            str(quote or ""),
            flags=re.IGNORECASE,
        )
    )


def zone_of_page(page: int | None, si_start_page: int | None) -> str:
    """分区判定（迁移期启发式）：长期方向是 EvidenceAnchor 携带正式 DocumentPartition。"""
    if si_start_page is not None and page is not None and int(page) >= int(si_start_page):
        return ZONE_SUPPORTING_INFO
    return ZONE_MAIN_TEXT


def format_pages(pages: list[int]) -> str:
    ordered = sorted(set(int(page) for page in pages if page and int(page) >= 1))
    if not ordered:
        return ""
    segments = []
    run_start = previous = ordered[0]
    for page in ordered[1:]:
        if page == previous + 1:
            previous = page
            continue
        segments.append(f"{run_start}-{previous}" if previous > run_start else f"{run_start}")
        run_start = previous = page
    segments.append(f"{run_start}-{previous}" if previous > run_start else f"{run_start}")
    return "、".join(segments)


def _shorten_filename(filename: Any) -> str:
    text = str(filename or "").strip()
    if len(text) <= CHIP_FILENAME_CHARS:
        return text or "未知文件"
    return text[: CHIP_FILENAME_CHARS - 1].rstrip() + "…"


def render_citation_chip(citation: dict[str, Any]) -> str:
    """后端确定性渲染的引用芯片；页码来自锚点权威值，模型无法伪造。"""
    ref = citation.get("ref") or "?"
    if not citation.get("locatable"):
        return f"〔证据{ref}｜无法定位页码〕"
    zone_label = ZONE_LABELS.get(str(citation.get("zone")), str(citation.get("zone") or "正文"))
    parts = [f"证据{ref}", f"{zone_label}·第{format_pages(citation.get('page_numbers') or [])}页"]
    file_label = _shorten_filename(citation.get("filename"))
    if file_label:
        parts.append(file_label)
    secondary_of = citation.get("secondary_of")
    if secondary_of:
        parts.append(f"同句正文见{secondary_of}")
    return "〔" + "｜".join(parts) + "〕"


def render_locator_chip(locator: dict[str, Any]) -> str:
    """确定性定位路由（QUOTE_LOCATOR）的权威结果芯片。"""
    zone_label = ZONE_LABELS.get(str(locator.get("zone")), str(locator.get("zone") or "正文"))
    parts = ["引文定位", f"{zone_label}·第{locator.get('page')}页"]
    file_label = _shorten_filename(locator.get("filename"))
    if file_label:
        parts.append(file_label)
    return "〔" + "｜".join(parts) + "〕"


def _locator_citation_row(
    ref: str,
    *,
    page: int | None,
    zone: str | None,
    filename: str | None,
    file_id: str | None,
    kb_id: str | None,
    anchor_id: str | None,
    quote: str,
    evidence_id: str | None = None,
    toc_line: bool = False,
) -> dict[str, Any]:
    pages = [int(page)] if page and int(page) >= 1 else []
    return {
        "ref": ref,
        "evidence_id": evidence_id,
        "kb_id": kb_id,
        "file_id": file_id,
        "filename": str(filename or ""),
        "zone": zone or ZONE_MAIN_TEXT,
        "page_numbers": pages,
        "primary_page": pages[0] if pages else None,
        "quote_head": re.sub(r"\s+", " ", str(quote or "").strip())[:80],
        "anchor_ids": [str(anchor_id)] if anchor_id else [],
        "locatable": bool(pages),
        "toc_line": toc_line,
        "secondary_of": None,
        "_quote_norm": normalize_for_match(quote)[:_QUOTE_NORM_CHARS],
    }


def append_locator_citations(citations: list[dict[str, Any]], locator: dict[str, Any] | None) -> list[dict[str, Any]]:
    """把确定性定位结果（含图注反链）并入引用池，使解释部分的 [E#] 有据可引。

    纯定位流不经过本函数（无生成通道）；复合意图流在编排器尾部调用，
    定位锚点与反链作为追加引用行（ref 续号），页码同样来自锚点权威值。
    幂等去重：引用池已含同一物理证据（evidence_id 或 anchor_id 相同）时不
    重复追加——冻结定位行进入证据集后，citations 构建已覆盖该锚点。
    """
    rows = list(citations or [])
    if not isinstance(locator, dict) or locator.get("status") != "VERIFIED":
        return rows
    known_evidence_ids = {str(row.get("evidence_id")) for row in rows if row.get("evidence_id")}
    known_anchor_ids = {str(anchor_id) for row in rows for anchor_id in (row.get("anchor_ids") or []) if anchor_id}

    def _already_covered(*, evidence_id: Any, anchor_id: Any) -> bool:
        if evidence_id and str(evidence_id) in known_evidence_ids:
            return True
        return bool(anchor_id and str(anchor_id) in known_anchor_ids)

    if locator.get("page") and not _already_covered(
        evidence_id=locator.get("evidence_id"), anchor_id=locator.get("anchor_id")
    ):
        rows.append(
            _locator_citation_row(
                f"E{len(rows) + 1}",
                page=int(locator["page"]),
                zone=locator.get("zone"),
                filename=locator.get("filename"),
                file_id=locator.get("file_id"),
                kb_id=locator.get("kb_id"),
                anchor_id=locator.get("anchor_id"),
                quote=str(locator.get("quote") or locator.get("quote_head") or ""),
                evidence_id=locator.get("evidence_id"),
                toc_line=bool(_TOC_LINE_PATTERN.match(str(locator.get("quote") or ""))),
            )
        )
    for backlink in locator.get("backlinks") or []:
        if not isinstance(backlink, dict) or not backlink.get("page"):
            continue
        if _already_covered(evidence_id=backlink.get("evidence_id"), anchor_id=backlink.get("anchor_id")):
            continue
        rows.append(
            _locator_citation_row(
                f"E{len(rows) + 1}",
                page=int(backlink["page"]),
                zone=backlink.get("zone") or ZONE_MAIN_TEXT,
                filename=backlink.get("filename") or locator.get("filename"),
                file_id=backlink.get("file_id"),
                kb_id=backlink.get("kb_id"),
                anchor_id=backlink.get("anchor_id"),
                quote=str(backlink.get("quote") or backlink.get("quote_head") or ""),
            )
        )
    return rows


# 反向绑定：未引用但含硬约束的陈述句上限（防止长答案全量扫描）
_MAX_REVERSE_BIND_SENTENCES = 12
_UNCOVERED_SNIPPET_CHARS = 30
_MAX_UNCOVERED_NOTICES = 3
# 句末紧随的 fail-closed 标记并入同一片段：模型写标记 = 明确的引用意图
_SENTENCE_TAIL_MARKER = r"(?:[ \t]*" + re.escape(NARRATIVE_LOCATOR_MARKER) + ")?"
_SENTENCE_SPLIT_PATTERN = re.compile(r"[^.!?。！？\n]+[.!?。！？]?" + _SENTENCE_TAIL_MARKER)


# ---- Markdown 结构感知（P1）----
_CODE_FENCE_PATTERN = re.compile(r"^\s*(```|~~~)")
_TABLE_ROW_PATTERN = re.compile(r"^\s*\|")
_HEADING_PATTERN = re.compile(r"^\s*#{1,6}\s")
# 后端保留区块头（P4：引用平面模板化，模型不得自建）
_REFERENCES_SECTION_HEADER = "【证据引用】"
_UNCOVERED_NOTICE_PREFIX = "（注：以下结论未在原文中定位到对应依据"


def _iter_markdown_blocks(text: str) -> tuple[list[str], list[tuple[str, int, int]]]:
    """按行把文本切成 (kind, 起行, 止行) 块：codefence/table/heading/paragraph。

    只读不改写：调用方据此决定哪些块可插入芯片、哪些整块跳过。
    codefence 从开栏行到闭栏行（含）整块；未闭合围栏按代码块处理到文末。
    """
    lines = text.split("\n")
    blocks: list[tuple[str, int, int]] = []
    current_kind: str | None = None
    start = 0
    in_fence = False
    for index, line in enumerate(lines):
        if in_fence:
            if _CODE_FENCE_PATTERN.match(line):
                in_fence = False
                blocks.append(("codefence", start, index))
                current_kind = None
            continue
        if _CODE_FENCE_PATTERN.match(line):
            if current_kind is not None:
                blocks.append((current_kind, start, index - 1))
            in_fence = True
            start = index
            continue
        if _TABLE_ROW_PATTERN.match(line):
            kind = "table"
        elif _HEADING_PATTERN.match(line):
            kind = "heading"
        elif line.strip().startswith(_REFERENCES_SECTION_HEADER):
            kind = "references"
        else:
            kind = "paragraph"
        if current_kind == "references" and kind == "paragraph":
            stripped_line = line.strip()
            if not stripped_line or stripped_line.startswith(("-", "---")):
                kind = "references"  # 区块数据行/分隔线延续
        if kind != current_kind:
            if current_kind is not None:
                blocks.append((current_kind, start, index - 1))
            current_kind = kind
            start = index
    if in_fence:
        blocks.append(("codefence", start, len(lines) - 1))
    elif current_kind is not None:
        blocks.append((current_kind, start, len(lines) - 1))
    return lines, blocks


def _normalize_snippet(sentence: str) -> str:
    """未定位片段归一化：去 Markdown 符号与列表标记、按词边界截断（P3）。"""
    value = re.sub(r"[*_#>`|]+", " ", str(sentence or ""))
    value = re.sub(r"^\s*(?:[-*+]|\d+\.)\s+", "", value.strip())
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= _UNCOVERED_SNIPPET_CHARS:
        return value
    cut = value[:_UNCOVERED_SNIPPET_CHARS]
    boundary = cut.rfind(" ")
    if boundary >= _UNCOVERED_SNIPPET_CHARS // 2:
        cut = cut[:boundary]
    return cut.rstrip("，。、；,.;") + "…"


def reverse_bind_citations(
    text: str, citations: list[dict[str, Any]], *, partition_intent: str | None = None
) -> tuple[str, int, list[str]]:
    """反向绑定（引用完备性兜底，零 LLM，Markdown 结构感知）。

    P1 插入点规则：代码围栏整块不改写；表格行不内插芯片（绑定命中改为
    表格后的脚注行）；标题行不绑定；段落句末插芯片且**尾随空白（含换行）
    原位保留**——芯片在句号后、换行前，永不粘连下一段标题。
    模型模仿的 fail-closed 标记视为引用意图：绑定成功换芯片；失败的句子
    原样保留（标记由展示层终点规则统一剥除并计入文末提示）。
    """
    pool = _citation_pool(citations)
    if not pool:
        return str(text or ""), 0, []
    by_ref = {str(item.get("ref")): item for item in citations or []}
    lines, blocks = _iter_markdown_blocks(str(text or ""))

    bound_count = 0
    uncovered: list[str] = []
    processed = 0

    def _bind(sentence: str) -> str | None:
        """返回芯片文本；None = 保持原句（必要时记入 uncovered）。"""
        nonlocal processed
        clean = sentence.replace(" " + NARRATIVE_LOCATOR_MARKER, "").replace(NARRATIVE_LOCATOR_MARKER, "").strip()
        if not clean or "证据E" in sentence or "引文定位" in sentence:
            return None
        hard = extract_hard_constraints(clean)
        if not (hard["numbers"] or hard["identifiers"]):
            return None
        processed += 1
        if processed > _MAX_REVERSE_BIND_SENTENCES:
            return None
        binding = resolve_binding(
            claim_context=clean, proposed_ref=None, citations=pool, partition_intent=partition_intent
        )
        if binding.get("status") == BINDING_VERIFIED:
            resolved = by_ref.get(str(binding.get("ref")))
            if resolved:
                return render_citation_chip(resolved)
        snippet = _normalize_snippet(clean)
        if snippet and snippet not in uncovered:
            uncovered.append(snippet)
        return None

    output_lines: list[str] = []
    for kind, start, end in blocks:
        if kind in ("codefence", "heading", "references"):
            output_lines.extend(lines[start : end + 1])
            continue
        if kind == "table":
            # 幂等防重：紧随本表的脚注行（上一轮绑定产物）存在时整表跳过，
            # 避免守卫+落库双重应用给同一表追加第二组脚注。
            already_annotated = any(
                lines[peek].strip().startswith("> 表格依据：") for peek in range(end + 1, min(end + 3, len(lines)))
            )
            footnotes: list[str] = []
            for row_index in range(start, end + 1):
                row = lines[row_index]
                if already_annotated:
                    output_lines.append(row)
                    continue
                chip = _bind(row.replace("|", " "))
                if chip:
                    footnotes.append("> 表格依据：" + chip)
                output_lines.append(row)
            if footnotes:
                output_lines.extend(footnotes)
                bound_count += len(footnotes)
            continue
        # 段落：逐行、句内绑定，尾随空白原位保留。
        # 行级芯片防重（幂等）：本行已含后端芯片（上一轮绑定产物）时整行
        # 跳过，避免守卫+落库双重应用时给同一句追加第二枚芯片。
        for line_index in range(start, end + 1):
            line = lines[line_index]
            trailing = ""
            body = line
            tail = re.search(r"(\s*)$", line)
            if tail and tail.group(1):
                trailing = tail.group(1)
                body = line[: tail.start()]
            if "〔证据E" in body or "〔引文定位" in body:
                output_lines.append(line)
                continue
            rebuilt = []
            for sentence_match in _SENTENCE_SPLIT_PATTERN.finditer(body):
                sentence = sentence_match.group(0)
                if not sentence.strip():
                    rebuilt.append(sentence)
                    continue
                chip = _bind(sentence)
                if chip:
                    bound_count += 1
                    rebuilt.append(sentence.rstrip() + " " + chip)
                else:
                    rebuilt.append(sentence)
            output_lines.append("".join(rebuilt) + trailing)
    return "\n".join(output_lines), bound_count, uncovered[:_MAX_UNCOVERED_NOTICES]


_LOCATOR_LINE_INLINE_PATTERN = re.compile(r"已可靠定位到原文：〔引文定位｜[^〕]*〕")
_LOCATOR_KEEP_TOKEN = "\x00LOCATOR_KEEP\x00"


def _ensure_locator_line(text: str, locator_chip: str) -> str:
    """定位行保障：locator 已验证时，「已可靠定位到原文：芯片」必须存在且唯一。

    模型没写（复合流常见）→ 后端确定性前置；重复出现（模型模仿+后端前置、
    多次 guard 叠加、或定位行末尾紧跟正文）→ 非锚定匹配只保留第一处，
    并把第一处归一为后端权威行（容忍模型复述时芯片文本的微小差异）。
    """
    line = f"已可靠定位到原文：{locator_chip}"
    matches = list(_LOCATOR_LINE_INLINE_PATTERN.finditer(text))
    if not matches:
        return f"{line}\n\n{text.lstrip()}"
    if len(matches) == 1 and matches[0].group(0) == line:
        return text
    first = matches[0]
    replaced = text[: first.start()] + line + text[first.end() :]
    replaced = replaced.replace(line, _LOCATOR_KEEP_TOKEN, 1)
    replaced = _LOCATOR_LINE_INLINE_PATTERN.sub("", replaced)
    replaced = replaced.replace(_LOCATOR_KEEP_TOKEN, line)
    replaced = re.sub(r"[ \t]*\n[ \t]*\n[ \t]*\n+", "\n\n", replaced)
    return replaced


def _strip_display_placeholders(text: str) -> tuple[str, int, int]:
    """展示层占位符终点规则（P3）：模型通道专用占位符绝不上屏。

    - ``[citation omitted]``（历史净化占位符）一律剥除；
    - 模型仿写的【证据引用】区块头剥除（该区块由后端确定性重渲染）；
    - 行内 fail-closed 标记全部剥除（≤1 次语义由文末统一提示承担）；
    返回 (文本, 剥除的标记数, 剥除的占位符数)。
    """
    result = str(text or "")
    placeholder_count = result.count(HISTORY_CITATION_PLACEHOLDER)
    result = result.replace(HISTORY_CITATION_PLACEHOLDER, "")
    # 整块剥除模型仿写/上一轮渲染的引用区块（头行 + 连续数据行/分隔线/空行）
    stripped_lines: list[str] = []
    skipping_references = False
    header_count = 0
    for line in result.split("\n"):
        if line.strip().startswith(_REFERENCES_SECTION_HEADER):
            skipping_references = True
            header_count += 1
            continue
        if skipping_references:
            stripped_line = line.strip()
            if not stripped_line or stripped_line.startswith(("-", "---")):
                continue
            skipping_references = False
        stripped_lines.append(line)
    result = "\n".join(stripped_lines)
    result = re.sub(r"\n{3,}", "\n\n", result)
    marker_count = result.count(NARRATIVE_LOCATOR_MARKER)
    result = result.replace(NARRATIVE_LOCATOR_MARKER, "")
    # 剥离残留清理：标点前多余空格、纯空白行收敛（不动表格对齐空白）
    result = re.sub(r"[ \t]+([,，.。;；、])", r"\1", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result, marker_count, placeholder_count + header_count


def _render_references_section(text: str, citations: list[dict[str, Any]]) -> tuple[str, bool]:
    """P4：引用平面模板化——【证据引用】区块完全由后端从已验证引用数据渲染。

    模型不得也不需要自建参考文献章节；区块含页码与锚点 ID（可查询可解释），
    幂等（已存在区块头则不重复渲染）。
    """
    if _REFERENCES_SECTION_HEADER in text:
        return text, False
    seen_refs: list[str] = []
    for match in re.finditer(r"〔证据E(\d{1,3})｜[^〕]*〕", text):
        ref = "E" + match.group(1)  # citations 键形如 E1（捕获组不含 E 前缀）
        if ref not in seen_refs:
            seen_refs.append(ref)
    if not seen_refs:
        return text, False
    by_ref = {str(item.get("ref")): item for item in citations or []}
    rows = []
    for ref in seen_refs:
        citation = by_ref.get(ref)
        if not citation:
            continue
        zone_label = ZONE_LABELS.get(str(citation.get("zone")), str(citation.get("zone") or "正文"))
        pages = format_pages(citation.get("page_numbers") or [])
        anchor_id = (citation.get("anchor_ids") or ["—"])[0]
        rows.append(
            "- "
            + ref
            + "｜"
            + zone_label
            + "·第"
            + pages
            + "页｜"
            + _shorten_filename(citation.get("filename"))
            + "｜"
            + str(anchor_id)
        )
    if not rows:
        return text, False
    block = "\n" + _REFERENCES_SECTION_HEADER + "（后端渲染，页码来自证据锚点）\n" + "\n".join(rows)
    return text.rstrip() + block, True


def _claim_context_around(text: str, start: int, end: int) -> str:
    """Take the nearest claim-sized sentence around a citation proposal."""
    before = text[max(0, start - _CONTEXT_BEFORE_CHARS) : start].rstrip()
    # A citation usually follows sentence punctuation. Keep that completed
    # sentence instead of returning the empty suffix after its final period.
    search_before = before.rstrip("。！？!?；;.,， ") if before else ""
    boundary = max(search_before.rfind(mark) for mark in ("\n", "。", "！", "？", "!", "?", "；", ";"))
    before_claim = search_before[boundary + 1 :]
    after = text[end : end + _CONTEXT_AFTER_CHARS]
    end_positions = [
        position for mark in ("\n", "。", "！", "？", "!", "?", "；", ";") if (position := after.find(mark)) >= 0
    ]
    after_claim = after[: min(end_positions) + 1] if end_positions else after
    return f"{before_claim} {after_claim}".strip()


def _citation_pool(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [citation for citation in citations or [] if citation.get("locatable") and citation.get("_quote_norm")]


def strip_bare_locators(text: str, *, replacement: str | None = None) -> tuple[str, dict[str, Any]]:
    """剥离模型自写的页码/锚点 ID（失败关闭）。

    replacement 缺省为失败关闭标记；locator 已验证时调用方传权威定位芯片，
    使模型自写的页码被确定性纠正为后端解析结果。
    """
    source = str(text or "")
    replacement = replacement or NARRATIVE_LOCATOR_MARKER
    detected_pages: list[str] = []
    for pattern in _BARE_PAGE_PATTERNS:
        detected_pages.extend(match.group(0) for match in pattern.finditer(source))
    detected_ids = _BARE_LOCATOR_ID_PATTERN.findall(source)
    if not detected_pages and not detected_ids:
        return source, {"status": "PASS", "detected_bare_pages": [], "detected_locator_ids": []}

    stripped = source
    for pattern in (*_BARE_PAGE_PATTERNS, _BARE_LOCATOR_ID_PATTERN):
        stripped = pattern.sub(replacement, stripped)
    stripped = _ADJACENT_LOCATOR_MARKERS.sub(replacement, stripped)
    return stripped, {
        "status": "SANITIZED",
        "detected_bare_pages": list(dict.fromkeys(detected_pages)),
        "detected_locator_ids": list(dict.fromkeys(detected_ids)),
    }


def expand_placeholders(
    text: str, citations: list[dict[str, Any]], *, partition_intent: str | None = None
) -> tuple[str, int, list[dict[str, Any]]]:
    """``[E#]`` 提议 → resolver 验证 → 权威芯片；验证失败剥离为失败关闭标记。

    返回 (文本, 展开数, 绑定审计)。
    """
    pool = _citation_pool(citations)
    by_ref = {str(item.get("ref")): item for item in citations or []}
    expanded_count = 0
    bindings: list[dict[str, Any]] = []

    def _substitute(match: re.Match) -> str:
        nonlocal expanded_count
        ref = match.group(1)
        proposed = by_ref.get(ref)
        binding = resolve_binding(
            claim_context=_claim_context_around(text, match.start(), match.end()),
            proposed_ref=ref if proposed else None,
            citations=pool,
            partition_intent=partition_intent,
        )
        bindings.append({"placeholder": ref, **binding})
        resolved = by_ref.get(str(binding.get("ref") or ""))
        if binding.get("status") == BINDING_VERIFIED and resolved:
            expanded_count += 1
            return render_citation_chip(resolved)
        return NARRATIVE_LOCATOR_MARKER

    return _PLACEHOLDER_PATTERN.sub(_substitute, str(text or "")), expanded_count, bindings


def _rewrite_fabricated_chips(
    text: str,
    citations: list[dict[str, Any]],
    *,
    partition_intent: str | None = None,
    locator_chip: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """伪造芯片处理：模型模仿渲染产物手写的芯片不是权威。

    绝不"ref 存在就信 ref"——先进入 resolver 验证该证据是否真的支持附近文本；
    VERIFIED → 重写为真芯片；否则整块剥离（locator 已验证时替换为定位芯片）。
    幂等防线：与当前引用池渲染产物**完全一致**的芯片是后端上一轮生成的合法
    芯片（守卫+落库双重应用场景），原样保留，不进入伪造判定。
    """
    fallback = locator_chip or ""
    rewritten = 0
    removed = 0
    legitimate_chips = {render_citation_chip(citation) for citation in _citation_pool(citations)}
    if locator_chip:
        legitimate_chips.add(locator_chip)

    def _substitute(match: re.Match) -> str:
        nonlocal rewritten, removed
        chip = match.group(0)
        if chip in legitimate_chips:
            return chip
        ref_match = re.search(r"E\d{1,3}", chip)
        binding = resolve_binding(
            claim_context=_claim_context_around(text, match.start(), match.end()),
            proposed_ref=ref_match.group(0) if ref_match else None,
            citations=_citation_pool(citations),
            partition_intent=partition_intent,
        )
        if binding.get("status") == BINDING_VERIFIED:
            rewritten += 1
            proposed = next((c for c in citations if str(c.get("ref")) == binding["ref"]), None)
            return render_citation_chip(proposed) if proposed else fallback or NARRATIVE_LOCATOR_MARKER
        removed += 1
        return fallback

    rewritten_text = _FABRICATED_CHIP_PATTERN.sub(_substitute, str(text or ""))
    return rewritten_text, {"fabricated_rewritten": rewritten, "fabricated_removed": removed}


def sanitize_history_text(text: str) -> str:
    """模型历史投影净化（展示通道 → 模型通道）。

    渲染产物绝不回流：芯片、失败关闭标记与附录行折叠为 ``[citation omitted]``。
    注意不折叠回 ``[E#]``——ref 是单次 retrieval run 的局部编号，回流会让模型
    把它当稳定引用标识，制造下一轮错绑。
    """
    value = str(text or "")
    if not value:
        return value
    value = _APPENDIX_LINE_PATTERN.sub("", value)
    value = _FABRICATED_CHIP_PATTERN.sub(HISTORY_CITATION_PLACEHOLDER, value)
    value = value.replace(NARRATIVE_LOCATOR_MARKER, HISTORY_CITATION_PLACEHOLDER)
    value = value.replace(LEGACY_NARRATIVE_LOCATOR_MARKER, HISTORY_CITATION_PLACEHOLDER)
    value = re.sub(rf"(?:\s*{re.escape(HISTORY_CITATION_PLACEHOLDER)})+", HISTORY_CITATION_PLACEHOLDER, value)
    return value


def apply_citation_channel(
    text: str,
    citations: list[dict[str, Any]],
    *,
    locator: dict[str, Any] | None = None,
    partition_intent: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """输出门禁：剥离 → 伪造重写 → 提议验证展开。页码只可能经权威路径上屏。

    locator（QUOTE_LOCATOR 确定性解析结果）已 VERIFIED 时：模型自写的页码被
    确定性纠正为后端解析芯片，而非失败关闭标记。
    """
    original = str(text or "")
    locator_chip = render_locator_chip(locator) if locator and locator.get("status") == "VERIFIED" else None
    source = original.replace(
        LEGACY_NARRATIVE_LOCATOR_MARKER,
        locator_chip or NARRATIVE_LOCATOR_MARKER,
    )

    # 1) 伪造芯片先进入 resolver。若先剥裸页码，芯片会被破坏成嵌套乱码。
    rewritten, fabrication = _rewrite_fabricated_chips(
        source, citations or [], partition_intent=partition_intent, locator_chip=locator_chip
    )

    # 2) 暂时保护刚由后端生成的权威芯片，再剥离剩余裸页码/锚点。
    protected: dict[str, str] = {}

    def _protect(match: re.Match) -> str:
        token = f"__YUXI_VERIFIED_CITATION_{len(protected)}__"
        protected[token] = match.group(0)
        return token

    authoritative_pattern = re.compile(r"〔(?:证据E\d{1,3}|引文定位)｜[^〕]+〕")
    protected_text = authoritative_pattern.sub(_protect, rewritten)
    stripped, locator_validation = strip_bare_locators(protected_text, replacement=locator_chip)
    for token, chip in protected.items():
        stripped = stripped.replace(token, chip)

    # 3) [E#] 提议：resolver 验证后展开（Legacy Citation Repair 兼容层）
    expanded, expanded_count, bindings = expand_placeholders(
        stripped, citations or [], partition_intent=partition_intent
    )
    expanded = _ADJACENT_LOCATOR_MARKERS.sub(lambda _: locator_chip or NARRATIVE_LOCATOR_MARKER, expanded)

    # 4) 反向绑定（引用完备性兜底）：模型漏写 [E#] 的硬约束句自动补权威芯片
    reverse_bound = 0
    uncovered: list[str] = []
    if _citation_pool(citations or []):
        expanded, reverse_bound, uncovered = reverse_bind_citations(
            expanded, citations or [], partition_intent=partition_intent
        )

    # 5) 展示层占位符终点（P3）：模型通道占位符/仿写区块头/行内标记剥除。
    #    必须先于文末提示——被剥除的标记语义由提示统一承担，且提示本身幂等。
    expanded, markers_stripped, placeholders_stripped = _strip_display_placeholders(expanded)

    # 6) 未定位依据明示（P2 幂等：提示前缀已存在则不重复追加）
    uncovered = uncovered[:_MAX_UNCOVERED_NOTICES]
    notice_needed = bool(uncovered) or markers_stripped > 0
    if notice_needed and _UNCOVERED_NOTICE_PREFIX not in expanded:
        if uncovered:
            detail = "、" + "、".join(uncovered)
        else:
            detail = ""
        expanded = (
            expanded.rstrip()
            + "\n\n"
            + _UNCOVERED_NOTICE_PREFIX
            + "，请谨慎采信"
            + ("以下结论" if uncovered else "个别结论")
            + detail
            + "）"
        )

    # 7) P4 引用平面模板化：已验证引用渲染为后端拥有的【证据引用】区块
    expanded, references_rendered = _render_references_section(expanded, citations or [])

    # 8) 定位行保障：复合意图流中模型可能漏写定位行，后端确定性补齐并去重
    locator_line_prepended = False
    if locator_chip and f"已可靠定位到原文：{locator_chip}" not in expanded:
        locator_line_prepended = True
    expanded = _ensure_locator_line(expanded, locator_chip) if locator_chip else expanded

    validation = {
        "version": CITATION_CHANNEL_VERSION,
        "locator": locator_validation,
        "fabrication": fabrication,
        "placeholder_expanded": expanded_count,
        "reverse_bound": reverse_bound,
        "uncovered_claims": uncovered,
        "markers_stripped": markers_stripped,
        "display_placeholders_stripped": placeholders_stripped,
        "references_rendered": references_rendered,
        "locator_line_prepended": locator_line_prepended,
        "bindings": bindings[:8],
        "citation_count": len(citations or []),
        "changed": expanded != original,
    }
    return expanded, validation


def _quote_fingerprint(quote: Any) -> str:
    return re.sub(r"\s+", " ", str(quote or "").casefold()).strip()[:_QUOTE_FINGERPRINT_CHARS]


def build_citation_rows(
    evidence_rows: list[dict[str, Any]],
    *,
    anchor_index: dict[tuple[str, str], Any],
    si_start_by_file: dict[str, int],
    filename_by_file: dict[str, str],
) -> list[dict[str, Any]]:
    """按证据行顺序构建 E1..En 引用（纯函数，便于单测）。

    - 每个 citation 只绑定一个物理锚点和一页；主引用跳过图目录行；同句正文优先（SI 引用标
      ``secondary_of``）；
    - **血统校验**：锚点页码必须落在载体 chunk 页码范围内（跨页合法）；VERBATIM
      行的 span 页码与锚点页码不一致 → 丢弃该锚点；全部失效 → 不可定位；
    - ``_quote_norm`` 为归一化完整引文（resolver 内部用，绝不进模型 payload）。
    """
    citations: list[dict[str, Any]] = []
    fingerprints: dict[str, str] = {}
    seen_anchor_keys: set[tuple[str, str]] = set()
    for row in evidence_rows or []:
        if len(citations) >= MAX_CITATIONS:
            break
        anchor_ids: list[str] = []
        explicit_anchor = str(row.get("anchor_id") or "")
        if explicit_anchor:
            anchor_ids.append(explicit_anchor)
        anchor_ids.extend(str(value) for value in row.get("anchor_ids") or [] if str(value) not in anchor_ids)
        anchor_ids.extend(
            anchor_id
            for anchor_id in extract_anchor_ids_from_content(row.get("evidence_quote") or row.get("content"))
            if anchor_id not in anchor_ids
        )
        if not anchor_ids:
            continue
        file_id = str(row.get("file_id") or "")
        parse_revision_id = str(row.get("_expected_parse_revision_id") or row.get("parse_revision_id") or "")
        si_start = si_start_by_file.get(file_id)
        # 血统基线：chunk 页码脚注（跨页区间）或 VERBATIM 行的 span 页码
        chunk_pages = parse_chunk_pages(row.get("evidence_quote") or row.get("content"))
        span_page = row.get("page_number")
        anchors = []
        dropped_by_lineage = 0
        for anchor_id in anchor_ids[:MAX_ANCHORS_PER_CITATION]:
            anchor = anchor_index.get((parse_revision_id, anchor_id))
            if anchor is None:
                continue
            page = int(anchor.page) if anchor.page else 0
            if chunk_pages is not None and page not in chunk_pages:
                dropped_by_lineage += 1
                continue
            if span_page is not None and int(span_page) >= 1 and page != int(span_page):
                dropped_by_lineage += 1
                continue
            if not bool(getattr(anchor, "locatable", False)):
                dropped_by_lineage += 1
                continue
            anchors.append(anchor)
        if not anchors:
            # 锚点失效或血统不一致：失败关闭，保留占位但不可定位
            citations.append(
                {
                    "ref": f"E{len(citations) + 1}",
                    "evidence_id": row.get("evidence_id"),
                    "kb_id": row.get("kb_id"),
                    "file_id": file_id,
                    "_parse_revision_id": parse_revision_id,
                    "_index_revision_id": row.get("_active_index_revision_id"),
                    "_source_sha256": row.get("_source_sha256"),
                    "filename": filename_by_file.get(file_id, ""),
                    "zone": ZONE_MAIN_TEXT,
                    "page_numbers": [],
                    "primary_page": None,
                    "anchor_ids": anchor_ids[:MAX_ANCHORS_PER_CITATION],
                    "locatable": False,
                    "toc_line": False,
                    "secondary_of": None,
                    "_lineage_dropped": dropped_by_lineage,
                }
            )
            continue

        row_evidence_type = row.get("evidence_type") or row.get("span_evidence_type")
        row_partition = row.get("document_partition")
        non_toc_anchors = [
            anchor
            for anchor in anchors
            if not is_toc_line(
                anchor.quote,
                evidence_type=row_evidence_type,
                partition=row_partition,
            )
        ]
        if non_toc_anchors:
            anchors = non_toc_anchors
        for anchor in anchors:
            if len(citations) >= MAX_CITATIONS:
                break
            anchor_key = (parse_revision_id, str(anchor.anchor_id))
            if anchor_key in seen_anchor_keys:
                continue
            seen_anchor_keys.add(anchor_key)
            page = int(anchor.page)
            zone = effective_partition(
                getattr(anchor, "document_partition", None),
                page=page,
                si_start_page=si_start,
            )
            toc_line = is_toc_line(
                anchor.quote,
                evidence_type=row_evidence_type,
                partition=row_partition,
            )
            fingerprint = _quote_fingerprint(anchor.quote)
            ref = f"E{len(citations) + 1}"
            secondary_of = None
            if zone == ZONE_SUPPORTING_INFO and fingerprint and fingerprint in fingerprints:
                secondary_of = fingerprints[fingerprint]
            elif zone == ZONE_MAIN_TEXT and fingerprint:
                fingerprints.setdefault(fingerprint, ref)
            primary_quote = re.sub(r"\s+", " ", str(anchor.quote or "").strip())
            citations.append(
                CitationBindingCandidate(
                    ref=ref,
                    retrieval_evidence_id=row.get("evidence_id"),
                    physical_evidence_id=row.get("physical_evidence_id") or row.get("evidence_id"),
                    span_evidence_id=row.get("span_evidence_id") or row.get("verbatim_evidence_id"),
                    kb_id=row.get("kb_id"),
                    file_id=file_id,
                    filename=filename_by_file.get(file_id, ""),
                    zone=zone,
                    page_numbers=[page],
                    quote_head=primary_quote[:80],
                    anchor_id=str(anchor.anchor_id),
                    span_id=row.get("span_id"),
                    parse_revision_id=parse_revision_id,
                    index_revision_id=row.get("_active_index_revision_id"),
                    source_sha256=row.get("_source_sha256"),
                    retrieval_channel=row.get("retrieval_channel") or row.get("source_type"),
                    quote=primary_quote,
                    quote_norm=normalize_for_match(anchor.quote)[:_QUOTE_NORM_CHARS],
                    locatable=not toc_line,
                    toc_line=toc_line,
                    secondary_of=secondary_of,
                    lineage_dropped=dropped_by_lineage,
                ).to_legacy_dict()
            )
    return citations


def public_citations(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """模型可见白名单：绝不携带页码、锚点或解析版本。"""
    return [CitationBindingCandidate.from_legacy_dict(citation).public_view() for citation in citations or []]


async def build_citations_for_contract(db, evidence_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从证据行构建权威引用（单次 DB 往返取锚点/文件名/SI 分区阈值；失败返回 [] 走失败关闭）。"""
    rows = [row for row in evidence_rows or [] if isinstance(row, dict)]
    anchor_ids_by_row: list[list[str]] = []
    file_ids: set[str] = set()
    for row in rows:
        row_anchor_ids: list[str] = []
        explicit_anchor = str(row.get("anchor_id") or "")
        if explicit_anchor:
            row_anchor_ids.append(explicit_anchor)
        row_anchor_ids.extend(str(value) for value in row.get("anchor_ids") or [] if str(value) not in row_anchor_ids)
        row_anchor_ids.extend(
            value
            for value in extract_anchor_ids_from_content(row.get("evidence_quote") or row.get("content"))
            if value not in row_anchor_ids
        )
        anchor_ids_by_row.append(row_anchor_ids)
        if row.get("file_id"):
            file_ids.add(str(row["file_id"]))
    if not any(anchor_ids_by_row):
        return []
    try:
        file_records = list(
            (await db.execute(select(KnowledgeFile).where(KnowledgeFile.file_id.in_(sorted(file_ids))))).scalars().all()
        )
        file_by_id = {str(record.file_id): record for record in file_records}
        enriched_rows: list[dict[str, Any]] = []
        anchor_keys: set[tuple[str, str]] = set()
        for row, row_anchor_ids in zip(rows, anchor_ids_by_row):
            file_id = str(row.get("file_id") or "")
            file_record = file_by_id.get(file_id)
            expected_parse = str(
                row.get("parse_revision_id") or getattr(file_record, "active_parse_revision_id", "") or ""
            )
            expected_index = str(row.get("index_revision_id") or "")
            active_index = str(getattr(file_record, "active_index_revision_id", "") or "")
            lineage_valid = bool(
                file_record
                and expected_parse
                and expected_parse == str(file_record.active_parse_revision_id or "")
                and (not expected_index or expected_index == active_index)
            )
            enriched_rows.append(
                {
                    **row,
                    "anchor_ids": row_anchor_ids,
                    "_expected_parse_revision_id": expected_parse,
                    "_active_index_revision_id": active_index,
                    "_lineage_valid": lineage_valid,
                }
            )
            if lineage_valid:
                anchor_keys.update((expected_parse, anchor_id) for anchor_id in row_anchor_ids)
        anchor_records = (
            list(
                (
                    await db.execute(
                        select(EvidenceAnchorRecord).where(
                            tuple_(EvidenceAnchorRecord.parse_revision_id, EvidenceAnchorRecord.anchor_id).in_(
                                list(anchor_keys)[:200]
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
            if anchor_keys
            else []
        )
        anchor_index = {(str(record.parse_revision_id), str(record.anchor_id)): record for record in anchor_records}
        revision_ids = {key[0] for key in anchor_keys}
        revisions = (
            list(
                (
                    await db.execute(
                        select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id.in_(revision_ids))
                    )
                )
                .scalars()
                .all()
            )
            if revision_ids
            else []
        )
        revision_by_id = {str(revision.revision_id): revision for revision in revisions}
        for row in enriched_rows:
            revision = revision_by_id.get(str(row.get("_expected_parse_revision_id") or ""))
            if (
                not revision
                or str(revision.file_id) != str(row.get("file_id") or "")
                or str(revision.kb_id) != str(row.get("kb_id") or "")
            ):
                row["_lineage_valid"] = False
                for anchor_id in row.get("anchor_ids") or []:
                    anchor_index.pop((str(row.get("_expected_parse_revision_id") or ""), str(anchor_id)), None)
            else:
                row["_source_sha256"] = str(revision.source_sha256 or "")
        filename_by_file = {str(record.file_id): str(record.filename or "") for record in file_records}
        return build_citation_rows(
            enriched_rows,
            anchor_index=anchor_index,
            si_start_by_file={},
            filename_by_file=filename_by_file,
        )
    except Exception as exc:  # noqa: BLE001
        # 失败关闭：引用构建失败时不给模型任何可引用页码，输出门禁仍会剥离裸页码
        logger.error(f"citation channel build failed (fail-closed): {exc}")
        return []


async def build_citations_for_rows(evidence_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """独立会话版本（工具/测试用）：内部开一次性 DB 会话。"""
    async with pg_manager.get_async_session_context() as session:
        return await build_citations_for_contract(session, evidence_rows)
