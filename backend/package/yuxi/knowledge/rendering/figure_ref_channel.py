"""图表引用锚点通道（figure reference channel，ADR-0008 P1）。

回答正文中的 ``Figure N`` / ``Table N`` 提及只是**候选**，不可信；只有能确定性
绑定到本轮冻结证据（引用池中的题注型 E# 行、locator Binding 的图表身份）时，
后端才签发权威锚点芯片 ``〔图表F1｜Figure 2〕`` 原地替换提及（签发即替代，
决策 2）。与 ``[E#] → resolver → 权威芯片`` 完全同构：

1. **注册表 = 本轮可信作用域**（:func:`build_caption_registry`）：只从
   ``contract.citations`` + ``locator_resolution`` 构建——题注型引用行的
   句首编号即自身编号（caption span 天然血统），locator Binding 是最强通道。
   模型上下文之外的全库同名 Figure 永不进入（几乎每篇论文都有 Figure 2）。
2. **绑定唯一键** = ``(file_id, parse_revision_id, canonical_key)``；同键跨
   文件/跨 revision → ``ambiguous``，不签发（失败关闭，绝不猜"最近的那张"）。
3. ``F#`` 是 run 内局部编号（与 ``E#`` 同语义，跨轮不可复用）；芯片短形态
   ``〔图表F1｜Figure 2〕``（页码/文件名留给卡片与悬浮 title，决策 5）。
4. **panel 字母不参与实体解析**（``figure 2a`` 的锚定键是 ``figure 2``），
   保留在展示层供 P3 panel 级联动。
5. 预算：每轮最多解析 :data:`MAX_FIGURE_REF_KEYS` 个规范键（防长文本查询放大）。

本模块纯函数、零 DB；mention 通道的图卡投影（七道门）在
``figure_asset_projection`` 扩展，SSE 发布在 chat 层（与 ``figure_card_enabled``
双闸）。签发集成在 ``citation_channel.apply_citation_channel`` 步骤 1b
（伪造重写之后、权威保护之前），受 ``figure_ref_anchor_enabled`` 开关管控。
"""

from __future__ import annotations

import re
from typing import Any

from yuxi.knowledge.contracts.locator_binding import authoritative_locator_projection
from yuxi.knowledge.evidence.caption_locator import (
    canonical_figure_label,
    extract_figure_label,
    iter_figure_labels,
    score_caption_text,
)
from yuxi.knowledge.evidence.sentence_splitter import split_caption_sequence
from yuxi.knowledge.rendering.authority_markers import parse_authority_markers

FIGURE_REF_CHANNEL_VERSION = "figure_ref_v1"

# 每轮解析的规范键上限：恶意/超长文本的查询放大护栏（决策 4：跨组总预算另在投影层）
MAX_FIGURE_REF_KEYS = 8

_FIGURE_REF_HEAD = "图表"
# 芯片回读（chat 层构建 figure_refs 载荷时从终态文本解析已签发锚点）
_FIGURE_REF_CHIP_PARSE = re.compile(r"〔图表(F\d{1,3})｜([^〕]+)〕")

# 未解析原因闭合枚举（trace attribute 与 validation 共用词表）
UNRESOLVED_NO_REGISTRY = "no_registry_match"
UNRESOLVED_AMBIGUOUS_SCOPE = "ambiguous_scope"
UNRESOLVED_BUDGET = "budget_exceeded"

# 对外 ref 条目的稳定形态（白名单）：注册表 entry 的内部字段（scopes 集合、
# ambiguous/binding_authority 标记）只服务解析逻辑，**永不进入任何返回值**——
# 否则未来消费点整包 json.dumps 会炸（scopes 是 set），或把内部裁决标记当
# 发布数据。chat 层载荷另有自己的 12 键白名单（纵深防御）。
_PUBLIC_REF_KEYS = (
    "key",
    "kind",
    "source",
    "label",
    "quote_head",
    "kb_id",
    "file_id",
    "revision_id",
    "anchor_id",
    "evidence_id",
    "page",
    "filename",
    "citation_ref",
)


def _public_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry.get(key) for key in _PUBLIC_REF_KEYS}


def render_figure_ref_chip(ref: str | int, label: str) -> str:
    """后端签发的图表锚点芯片：〔图表F1｜Figure 2〕（短形态）。

    ``ref`` 接受 ``"F1"`` 或 ``1``（统一输出 ``图表F1`` 头，与
    ``authority_markers`` 的形态族严格一致）。
    """
    ref_text = str(ref)
    if not ref_text.startswith("F"):
        ref_text = f"F{ref_text}"
    return f"〔{_FIGURE_REF_HEAD}{ref_text}｜{label}〕"


def _row_quote(row: dict[str, Any]) -> str:
    return str(row.get("_quote") or row.get("quote_head") or "")


def _row_anchor_id(row: dict[str, Any]) -> str:
    explicit = str(row.get("_anchor_id") or "")
    if explicit:
        return explicit
    anchors = row.get("anchor_ids") or []
    return str(anchors[0]) if anchors else ""


def _row_revision_id(row: dict[str, Any]) -> str:
    return str(row.get("_parse_revision_id") or "")


def _caption_label_of(quote: str, evidence_type: Any) -> str | None:
    """题注判定：句首编号即自身编号（caption span 血统），或显式 caption 类型。

    正文证据行即使提及 ``Figure 2``（句中引用），句首不是编号 → 不是题注，
    不构成注册表来源——锚定必须落到题注实体，不能落到"提到图的一句正文"。
    """
    label = extract_figure_label(quote)
    if label is not None and quote.strip().lower().startswith(label.lower()):
        return label
    if str(evidence_type or "").casefold() == "caption":
        return label
    return None


def _registry_entry(
    *,
    key: str,
    kind: str,
    source: str,
    quote_head: str,
    label: str,
    kb_id: str,
    file_id: str,
    revision_id: str,
    anchor_id: str,
    evidence_id: str,
    page: int | None,
    filename: str,
    citation_ref: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "kind": kind,
        "source": source,
        "label": re.sub(r"\s+", " ", label).strip(),
        "quote_head": str(quote_head or "")[:80],
        # Full caption is internal-only semantic-validation material.  It is
        # deliberately absent from _PUBLIC_REF_KEYS and therefore never enters
        # figure_refs/SSE payloads.
        "_caption_quote": str(quote_head or ""),
        "kb_id": kb_id,
        "file_id": file_id,
        "revision_id": revision_id,
        "anchor_id": anchor_id,
        "evidence_id": evidence_id,
        "page": int(page) if page else None,
        "filename": str(filename or ""),
        "citation_ref": citation_ref,
    }


def build_caption_registry(
    citations: list[dict[str, Any]] | None,
    locator: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """本轮可信图表注册表：题注型引用行 + locator Binding 图表身份。

    返回 ``{base_key: entry}``；entry 含 ``scopes``（唯一键集合）与
    ``ambiguous`` / ``binding_authority`` 标记：

    - locator Binding 是本轮最强验证通道：其键不被其他文献的同名题注
      稀释成歧义（用户明确问的就是这篇文献的这张图）；
    - 引用行之间同键跨 ``(file_id, revision_id)`` → ``ambiguous=True``，
      调用方不得签发。
    """
    registry: dict[str, dict[str, Any]] = {}

    def _register(entry: dict[str, Any], *, binding_authority: bool = False) -> None:
        key = entry["key"]
        scope = (entry["file_id"], entry["revision_id"])
        existing = registry.get(key)
        if existing is None:
            registry[key] = {
                **entry,
                "scopes": {scope},
                "ambiguous": False,
                "binding_authority": binding_authority,
            }
            return
        if binding_authority and not existing.get("binding_authority"):
            # Binding 后到但权威更高：以 Binding 覆盖，同键其他文献题注不再构成歧义
            registry[key] = {
                **entry,
                "scopes": {scope},
                "ambiguous": False,
                "binding_authority": True,
            }
            return
        if existing.get("binding_authority"):
            return  # 键已由 Binding 锁定，引用行不覆盖、不制造歧义
        existing["scopes"].add(scope)
        if len(existing["scopes"]) > 1:
            existing["ambiguous"] = True
            return
        # Same scope duplicates are common when several parser sources emit the
        # same caption.  Collapse them deterministically and retain the most
        # informative, physically locatable carrier instead of depending on DB
        # row order.  This prevents a truncated/null-location duplicate from
        # becoming the semantic authority for the label.
        existing_rank = (
            bool(existing.get("anchor_id")),
            bool(existing.get("page")),
            score_caption_text(existing.get("_caption_quote")),
            len(str(existing.get("_caption_quote") or "")),
        )
        candidate_rank = (
            bool(entry.get("anchor_id")),
            bool(entry.get("page")),
            score_caption_text(entry.get("_caption_quote")),
            len(str(entry.get("_caption_quote") or "")),
        )
        if candidate_rank > existing_rank:
            scopes = existing["scopes"]
            registry[key] = {
                **entry,
                "scopes": scopes,
                "ambiguous": False,
                "binding_authority": False,
            }

    authoritative = authoritative_locator_projection((locator or {}).get("binding")) if locator else None
    if authoritative:
        quote_head = str(authoritative.get("quote_head") or "")
        label = _caption_label_of(quote_head, None)
        if label is not None:
            key = canonical_figure_label(label)
            if key is not None:
                _register(
                    _registry_entry(
                        key=key,
                        kind="table" if key.startswith("table") else "figure",
                        source="binding",
                        quote_head=quote_head,
                        label=label,
                        kb_id=str(authoritative.get("kb_id") or ""),
                        file_id=str(authoritative.get("file_id") or ""),
                        revision_id=str(authoritative.get("parse_revision_id") or ""),
                        anchor_id=str(authoritative.get("anchor_id") or ""),
                        evidence_id=str(authoritative.get("evidence_id") or ""),
                        page=authoritative.get("page"),
                        filename=str(authoritative.get("filename") or ""),
                        citation_ref="",
                    ),
                    binding_authority=True,
                )

    for row in citations or []:
        if not isinstance(row, dict) or row.get("toc_line"):
            continue
        quote = _row_quote(row)
        if not quote:
            continue
        # Legacy/current parser rows may carry two consecutive captions in one
        # physical anchor.  Register every deterministic child label against
        # that same anchor/page instead of silently treating the tail as part
        # of the first figure's meaning.
        for caption_quote in split_caption_sequence(quote):
            label = _caption_label_of(caption_quote, row.get("_evidence_type"))
            if label is None:
                continue
            key = canonical_figure_label(label)
            base_key = re.sub(r"(?<=\d)[A-Za-z]$", "", key) if key is not None else None
            if base_key is None:
                continue
            anchor_id = _row_anchor_id(row)
            evidence_id = str(row.get("_physical_evidence_id") or row.get("evidence_id") or "")
            if not anchor_id and not evidence_id:
                continue
            _register(
                _registry_entry(
                    key=base_key,
                    kind="table" if base_key.startswith("table") else "figure",
                    source="caption",
                    quote_head=caption_quote,
                    label=label,
                    kb_id=str(row.get("kb_id") or ""),
                    file_id=str(row.get("file_id") or ""),
                    revision_id=_row_revision_id(row),
                    anchor_id=anchor_id,
                    evidence_id=evidence_id,
                    page=(row.get("page_numbers") or [None])[0],
                    filename=str(row.get("filename") or ""),
                    citation_ref=str(row.get("ref") or ""),
                )
            )
    return registry


def _mask_authority_spans(text: str) -> list[tuple[int, int]]:
    return [(m["start"], m["end"]) for m in parse_authority_markers(text)]


def resolve_figure_refs(
    text: str,
    citations: list[dict[str, Any]] | None,
    locator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """解析正文提及 → 签发计划（纯函数；签发本身在 citation_channel 步骤 1b）。

    返回 ``{"refs": [entry…], "stats": {...}}``：

    - ``refs`` 按正文首次出现顺序排列，每项是注册表 entry + 提及信息
      （``mention_raw`` / ``occurrences``），**未编号**——F# 由签发步骤按
      实际替换顺序分配（表格/代码块内的提及不签发也不占用编号）；
    - ``stats.unresolved`` 是 ``{reason: count}`` 闭合枚举；
    - 幂等：已签发芯片（authority marker）覆盖区间内的提及不再解析。
    """
    source = str(text or "")
    stats: dict[str, Any] = {
        "mentions_total": 0,
        "resolved": 0,
        "unresolved": {UNRESOLVED_NO_REGISTRY: 0, UNRESOLVED_AMBIGUOUS_SCOPE: 0, UNRESOLVED_BUDGET: 0},
    }
    registry = build_caption_registry(citations, locator)
    if not registry or not source:
        return {"refs": [], "stats": stats}

    masked = _mask_authority_spans(source)

    def _in_marker(start: int, end: int) -> bool:
        return any(start < mend and end > mstart for mstart, mend in masked)

    seen: dict[str, dict[str, Any]] = {}
    for mention in iter_figure_labels(source):
        stats["mentions_total"] += 1
        if _in_marker(mention["start"], mention["end"]):
            continue  # 芯片载荷内的编号（上一轮签发产物），幂等跳过
        key = mention["canonical"]
        base_key = mention["base_key"]
        lookup = key if key in registry else base_key
        entry = registry.get(lookup)
        if entry is None:
            stats["unresolved"][UNRESOLVED_NO_REGISTRY] += 1
            continue
        if entry.get("ambiguous"):
            stats["unresolved"][UNRESOLVED_AMBIGUOUS_SCOPE] += 1
            continue
        existing = seen.get(entry["key"])
        if existing is not None:
            existing["occurrences"] += 1
            continue
        if len(seen) >= MAX_FIGURE_REF_KEYS:
            stats["unresolved"][UNRESOLVED_BUDGET] += 1
            continue
        seen[entry["key"]] = {
            **_public_entry(entry),
            "mention_raw": mention["raw"],
            "mention_key": key,
            "occurrences": 1,
        }
    refs = list(seen.values())
    stats["resolved"] = len(refs)
    return {"refs": refs, "stats": stats}


def parse_figure_ref_chips(text: str) -> list[dict[str, Any]]:
    """终态文本回读已签发锚点芯片（chat 层构建 figure_refs 载荷的唯一入口）。

    芯片在文本里 = 传输载体（与 ``E#`` 芯片 + ``citation_ready.citation``
    的关系同构）；这里只解析 ``(ref, label)``，身份/页码/血统由
    :func:`match_chips_to_registry` 与注册表 join 补全——**绝不信芯片自带的
    任何身份字段**（芯片只有编号与展示标签，无可伪造面）。
    """
    chips: list[dict[str, Any]] = []
    for match in _FIGURE_REF_CHIP_PARSE.finditer(str(text or "")):
        label = re.sub(r"\s+", " ", match.group(2)).strip()
        if not label:
            continue
        chips.append({"ref": match.group(1), "label": label})
    return chips


def match_chips_to_registry(
    text: str,
    citations: list[dict[str, Any]] | None,
    locator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """终态芯片 ↔ 注册表 join：chat 层据此构建 ``figure_refs[]`` 载荷与 trace。

    返回 ``{"refs": [entry…], "stats": {...}}``——entry 为**公开形态白名单**
    （:data:`_PUBLIC_REF_KEYS`）+ 芯片 ``(ref, label)``，内部字段（scopes 集合等）
    已剥离，返回值可直接 json 序列化；解析不到注册表的芯片（理论上不存在：
    签发即来自注册表；防御历史文本被手工改写的情况）标记 ``orphan=True``，
    不发布。
    """
    chips = parse_figure_ref_chips(text)
    registry = build_caption_registry(citations, locator)
    stats = {"chips": len(chips), "matched": 0, "orphan": 0}
    refs: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for chip in chips:
        if chip["ref"] in seen_refs:
            continue  # 同键多次提及共享同一芯片编号：载荷一条（E2E 实测回归锁）
        canonical = canonical_figure_label(chip["label"])
        base_key = re.sub(r"(?<=\d)[A-Za-z]$", "", canonical) if canonical is not None else None
        entry = registry.get(base_key) if base_key is not None else None
        if entry is None or entry.get("ambiguous"):
            stats["orphan"] += 1
            continue
        stats["matched"] += 1
        seen_refs.add(chip["ref"])
        refs.append({**_public_entry(entry), "ref": chip["ref"], "label": chip["label"]})
    return {"refs": refs, "stats": stats}


__all__ = [
    "FIGURE_REF_CHANNEL_VERSION",
    "MAX_FIGURE_REF_KEYS",
    "UNRESOLVED_AMBIGUOUS_SCOPE",
    "UNRESOLVED_BUDGET",
    "UNRESOLVED_NO_REGISTRY",
    "build_caption_registry",
    "match_chips_to_registry",
    "parse_figure_ref_chips",
    "render_figure_ref_chip",
    "resolve_figure_refs",
]
