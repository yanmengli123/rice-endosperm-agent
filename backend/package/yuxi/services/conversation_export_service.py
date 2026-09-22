"""会话 HTML 导出：把一段对话的问答渲染为自包含、零脚本、打印友好的 HTML 文件。

设计约束（docs/vibe/2026-09-21-conversation-html-export.md，V1b）：
- 服务端统一渲染，Web 与桌面端拿到字节一致；
- 零 JS、零外部资源（内联 CSS、系统字体、自带 CSP），离线可开，打印成 PDF 排版正确；
  图片一律内嵌 data URI（预算化归一化，超限降级为占位卡）；
- XSS 三层防线：MarkdownIt 以 html=False 渲染（原始 HTML 一律转义为文本）→
  a[href]/img[src] 协议白名单（链接与图片分开收敛，图片允许 data:image/*）→
  文档级 CSP 兜底；
- 公式走 dollarmath 结构化呈现（.math-inline/.math-block，不引入 KaTeX 字体）；
- 代码块走 pygments 服务端高亮（自定义浅色样式对齐 web base.css 色阶）；
- 协议标记注册表：权威芯片形态从 authority_markers 单点取用，MCP-F 事实引用
  脚注化并在文末生成溯源附录（MCPCallAudit 审计记录）。
"""

import asyncio
import base64
import io
import json
import re
from datetime import datetime, timedelta, timezone, UTC
from html import escape as escape_html
from string import Template
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from PIL import Image
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.style import Style
from pygments.token import Comment, Generic, Keyword, Name, Number, Operator, String
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.rendering.authority_markers import authority_marker_pattern
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.services.conversation_service import get_thread_history_view
from yuxi.services.knowledge_asset_service import KnowledgeAssetError, resolve_asset
from yuxi.services.operation_log_service import log_operation
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import MCPCallAudit, User
from yuxi.storage.postgres.models_knowledge import KnowledgeFile

_CN_TZ = timezone(timedelta(hours=8))
_ALLOWED_HREF_SCHEMES = {"http", "https", "mailto"}
_ALLOWED_IMG_SRC_SCHEMES = _ALLOWED_HREF_SCHEMES | {"data"}

# 图片内嵌预算：归一化后单图上限与全文总预算（base64 前的原始字节计）。
_MAX_IMAGE_EDGE = 1600
_PER_IMAGE_BYTES = int(1.5 * 1024 * 1024)
_TOTAL_IMAGE_BUDGET = 20 * 1024 * 1024
_MAX_ASSET_READ_BYTES = 32 * 1024 * 1024

# MCP-F 事实引用：形态与 source_output_guard._FACT_MARKER / fact_ledger.citation_format
# 对齐（该文件正处于在途改动的发布链路上，稳定后应上移到 authority_markers 单点维护，
# 测试 test_mcp_f_pattern_matches_canonical 锁定两者不漂移）。
_MCP_FACT_MARKER = re.compile(r"\[MCP-F:(\d+):(f_[0-9a-f]{16})\]", re.I)
_MCP_FACT_RUN = re.compile(r"\[MCP-F:\d+:f_[0-9a-f]{16}\](?:\s*\[MCP-F:\d+:f_[0-9a-f]{16}\])*", re.I)
# 事实账本目录块只应存在于工具观察中；出现在导出内容时整体剥离（保险层）。
_LEDGER_BLOCK_RE = re.compile(r"<YUXI_MCP_FACT_LEDGER>.*?</YUXI_MCP_FACT_LEDGER>", re.S)
# kbasset://{file_id}/{revision_id}/{asset_name}（与 web kbasset_contract 同构）。
_KBASSET_URI_RE = re.compile(r"kbasset://([A-Za-z0-9._:-]+)/([A-Za-z0-9._:-]+)/([A-Za-z0-9._%+-]+)")


async def export_thread_html_view(
    *,
    thread_id: str,
    current_user: User,
    db: AsyncSession,
    request: Request,
) -> tuple[str, str]:
    """导出指定会话的问答 HTML；归属校验与 /history 一致，并写一条导出审计。"""
    conv_repo = ConversationRepository(db)
    conversation = await conv_repo.get_conversation_by_thread_id(thread_id, uid=str(current_user.uid))
    if not conversation or conversation.uid != str(current_user.uid) or conversation.status == "deleted":
        raise HTTPException(status_code=404, detail="对话线程不存在")

    history_payload = await get_thread_history_view(thread_id=thread_id, current_uid=str(current_user.uid), db=db)
    history = history_payload.get("history") or []
    question_images, kbassets = await _collect_embedded_images(history, current_user)
    fact_records = await _collect_fact_records(history, current_user)
    exported_at = datetime.now(_CN_TZ)
    html = render_conversation_html(
        title=conversation.title,
        agent_slug=conversation.agent_id,
        thread_id=conversation.thread_id,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        history=history,
        exported_at=exported_at,
        question_images=question_images,
        kbassets=kbassets,
        fact_records=fact_records,
    )
    filename = build_filename(conversation.title, exported_at)
    await log_operation(
        db,
        current_user.id,
        "导出会话HTML",
        json.dumps(
            {
                "thread_id": thread_id,
                "messages": len(history),
                "images_embedded": sum(1 for v in {**question_images, **kbassets}.values() if v),
            },
            ensure_ascii=False,
        ),
        request=request,
    )
    return html, filename


def build_filename(title: str | None, moment: datetime) -> str:
    """按「语析对话_{标题清洗}_{时间}.html」构造下载文件名，剥掉 Windows/Linux 非法字符。"""
    if moment.tzinfo is not None:
        moment = moment.astimezone(_CN_TZ)
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n\t]", " ", title or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")[:60].strip()
    if not cleaned:
        cleaned = "未命名会话"
    return f"语析对话_{cleaned}_{moment:%Y%m%d-%H%M%S}.html"


def render_conversation_html(
    *,
    title: str | None,
    agent_slug: str,
    thread_id: str,
    created_at: datetime | None,
    updated_at: datetime | None,
    history: list[dict],
    exported_at: datetime,
    question_images: dict[int, str | None] | None = None,
    kbassets: dict[str, str | None] | None = None,
    fact_records: dict[int, dict] | None = None,
) -> str:
    """把会话元数据与 history 视图渲染为完整 HTML 文档（纯函数，不碰 DB/IO）。

    ``question_images``：消息 id → 用户图片的 data URI（None=嵌入失败）；
    ``kbassets``：kbasset URI → data URI（None=资产不可用/超预算）；
    ``fact_records``：MCP-F 审计 id → {server_slug, capability_name, status, facts}。
    三者由编排层异步预取后注入，保持本函数可纯注入测试。
    """
    question_images = question_images or {}
    kbassets = kbassets or {}
    fact_records = fact_records or {}
    rounds = _pair_rounds(history)
    cards = [
        card
        for card in (
            _render_qa_card(index=index, entry=entry, question_images=question_images, kbassets=kbassets)
            for index, entry in enumerate(rounds, 1)
        )
        if card
    ]
    toc = _render_toc(rounds)
    appendix = _render_fact_appendix(history, fact_records)
    display_title = (title or "").strip() or "未命名会话"
    embedded = sum(1 for value in {**question_images, **kbassets}.values() if value)
    skipped = len(question_images) + len(kbassets) - embedded
    image_stats = f" · 已内嵌图片 {embedded} 张" + (
        f"（{skipped} 张因资产不可用或超预算降级为占位）" if skipped else ""
    )
    meta_rows = "".join(
        f"<tr><th>{label}</th><td>{value}</td></tr>"
        for label, value in [
            ("智能体", escape_html(str(agent_slug or "—"))),
            ("会话标识", escape_html(str(thread_id))),
            ("创建时间", _fmt_datetime(created_at)),
            ("最近活动", _fmt_datetime(updated_at)),
            ("问答轮数", str(len(cards))),
            ("消息总数", str(len(history))),
            ("导出时间", _fmt_datetime(exported_at)),
        ]
    )
    footer = (
        "由语析（Yuxi）导出 · 零脚本自包含文档（无外部资源，可离线查看）· "
        "打印（Ctrl+P）可另存为排版正确的 PDF" + image_stats
    )
    return _TEMPLATE.safe_substitute(
        title=escape_html(display_title),
        subtitle=f"共 {len(cards)} 轮问答 · {len(history)} 条消息 · 时间均为 UTC+8",
        meta_rows=meta_rows,
        toc=toc,
        cards="\n".join(cards),
        appendix=appendix,
        footer=footer,
        css=_CSS,
    )


# ── QA 配对与卡片渲染 ────────────────────────────────────────────────────────


def _pair_rounds(history: list[dict]) -> list[dict]:
    """human 开新一轮、ai 归入当前轮；system/tool 消息不进导出。"""
    rounds: list[dict] = []
    for msg in history:
        msg_type = msg.get("type")
        if msg_type == "human":
            rounds.append({"question": msg, "answers": []})
        elif msg_type == "ai":
            if not rounds:
                rounds.append({"question": None, "answers": []})
            rounds[-1]["answers"].append(msg)
    return rounds


def _render_qa_card(*, index: int, entry: dict, question_images: dict, kbassets: dict) -> str:
    question = entry["question"]
    question_html = ""
    if question is not None:
        question_text = escape_html(str(question.get("content") or "")).replace("\n", "<br>")
        meta_bits = []
        stamp = _fmt_timestamp(question.get("created_at"))
        if stamp:
            meta_bits.append(stamp)
        question_image_uri = question_images.get(question.get("id"))
        if question.get("image_content") and not question_image_uri:
            meta_bits.append("含图片消息（图片未内联）")
        meta_bits.extend(f"附件 {name}（{size}）" for name, size in _attachment_summaries(question))
        meta_html = f'<p class="qa-meta">{escape_html(" · ".join(meta_bits))}</p>' if meta_bits else ""
        image_html = (
            f'<img class="question-image" src="{question_image_uri}" alt="用户图片" />' if question_image_uri else ""
        )
        question_html = (
            '<div class="qa-q">'
            f'<span class="qa-label">问题 {index:02d}</span>'
            f'<p class="qa-q-text">{question_text or "（空提问）"}</p>'
            f"{image_html}"
            f"{meta_html}"
            "</div>"
        )

    answer_parts = []
    for answer in entry["answers"]:
        content = str(answer.get("content") or "")
        if content.strip():
            answer_parts.append(_render_markdown(_inline_kbassets(content, kbassets)))
        else:
            error = str(answer.get("error_message") or "").strip()
            if error:
                answer_parts.append(f'<p class="answer-error">本轮回答生成失败：{escape_html(error)}</p>')
    figure_cards = _render_figure_cards(entry, kbassets)
    if figure_cards:
        answer_parts.append(figure_cards)
    if not answer_parts:
        return ""
    return (
        f'<section class="qa" id="qa-{index}">{question_html}<div class="qa-a">{"".join(answer_parts)}</div></section>'
    )


def _inline_kbassets(content: str, kbassets: dict) -> str:
    """把正文中可解析的 kbasset:// 引用替换为内嵌 data URI；不可用的保留原样（由图片白名单降级为 alt 文本）。"""

    def replace(match) -> str:  # noqa: ANN001
        return kbassets.get(match.group(0)) or match.group(0)

    return _KBASSET_URI_RE.sub(replace, content)


# ── 图卡（citation_ready.figures，取数口径与站内 inlineFiguresForMessage 一致） ─


def _figure_asset_uri(figure: dict) -> str:
    """`kbasset://{file_id}/{revision_id}/{asset_name}`；与站内 figureAssetUri 同构，缺段即空。"""
    file_id = str(figure.get("file_id") or "").strip()
    revision_id = str(figure.get("revision_id") or "").strip()
    asset_name = str(figure.get("asset_name") or "").strip()
    if not (file_id and revision_id and asset_name and str(figure.get("kb_id") or "").strip()):
        return ""
    return f"kbasset://{file_id}/{revision_id}/{asset_name}"


def _verified_figures(entry: dict) -> list[dict]:
    """该轮最后一条携带 citation_ready 载荷的 assistant 消息的图卡（只收能组出合法 URI 的条目）。"""
    for answer in reversed(entry["answers"]):
        payload = ((answer.get("extra_metadata") or {}).get("citation_ready") or {}).get("figures")
        if isinstance(payload, list) and payload:
            return [figure for figure in payload if isinstance(figure, dict) and _figure_asset_uri(figure)]
    return []


def _as_int(value, default: int = 0) -> int:  # noqa: ANN001
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _figure_caption(figure: dict) -> str:
    # 标题回退链与站内 figureCardTitle 一致：题注 → 编号 → 中性文案（禁用任何模型文本）
    page = _as_int(figure.get("page"))
    return (
        str(figure.get("caption") or "").strip()
        or str(figure.get("figure_label") or "").strip()
        or (f"图 · 第{page}页" if page >= 1 else "图")
    )


def _figure_page_badge(figure: dict) -> str:
    """页码徽章：跨页图表（题注页/图页不同）显示两段，相同只显示一个（契约既有口径）。"""
    page = _as_int(figure.get("page"))
    asset_page = _as_int(figure.get("asset_page"))
    if page < 1:
        return ""
    if asset_page >= 1 and asset_page != page:
        return f'<span class="figure-page">题注第{page}页 · 图第{asset_page}页</span>'
    return f'<span class="figure-page">第{page}页</span>'


def _group_figures(figures: list[dict]) -> list[dict]:
    """按 binding_id 分组（站内 FigureCardGroup 同款分组键；旧载荷无该字段时按题注文本归组）。

    组内 primary 取 role=="primary"（缺失回退首条），panels 按 group_index 阅读序；
    题注/页码在组级只输出一次，子图只带 panel_label 角标。
    """
    groups: list[dict] = []
    by_key: dict[str, dict] = {}
    for figure in figures:
        binding_id = str(figure.get("binding_id") or "").strip()
        caption_key = f"{str(figure.get('caption') or '').strip()}|{str(figure.get('figure_label') or '').strip()}"
        key = f"b:{binding_id}" if binding_id else f"c:{caption_key}"
        group = by_key.get(key)
        if group is None:
            group = {"members": []}
            by_key[key] = group
            groups.append(group)
        group["members"].append(figure)
    for group in groups:
        members = group["members"]
        primary = next((member for member in members if member.get("role") == "primary"), members[0])
        panels = sorted(
            (member for member in members if member is not primary),
            key=lambda member: _as_int(member.get("group_index")),
        )
        group["primary"] = primary
        group["panels"] = panels
    return groups


def _render_figure_panels(panels: list[dict], kbassets: dict) -> str:
    items = []
    for panel in panels:
        data_uri = kbassets.get(_figure_asset_uri(panel))
        if not data_uri:
            continue  # 子图失败静默跳过：主图承载语义，不逐个占位
        label = str(panel.get("panel_label") or "").strip()
        label_html = f"<i>{escape_html(label)}</i>" if label else ""
        items.append(f'<span class="figure-panel"><img src="{data_uri}" alt="" />{label_html}</span>')
    if not items:
        return ""
    return f'<div class="figure-panels">{"".join(items)}</div>'


def _render_figure_cards(entry: dict, kbassets: dict) -> str:
    cards = []
    for group in _group_figures(_verified_figures(entry)):
        primary = group["primary"]
        caption = _figure_caption(primary)
        page_html = _figure_page_badge(primary)
        panels_html = _render_figure_panels(group["panels"], kbassets)
        data_uri = kbassets.get(_figure_asset_uri(primary))
        if data_uri:
            cards.append(
                f'<figure class="figure-card"><img src="{data_uri}" alt="{escape_html(caption)}" />'
                f"<figcaption>{escape_html(caption)}{page_html}</figcaption>{panels_html}</figure>"
            )
        elif panels_html:
            # 主图不可用但子图可用：题注仍只出一次，子图网格降级呈现
            cards.append(
                f'<figure class="figure-card figure-primary-missing">'
                f"<figcaption>{escape_html(caption)}{page_html}</figcaption>{panels_html}</figure>"
            )
        else:
            cards.append(
                f'<figure class="figure-card figure-missing"><figcaption>{escape_html(caption)}{page_html}'
                '<span class="figure-missing-note">图片资产不可用或超预算，未内联</span></figcaption></figure>'
            )
    return "".join(cards)


# ── MCP-F 溯源附录（答案中的 [MCP-F] 引用收敛为上标，审计记录回查 MCPCallAudit） ─


def _collect_audit_ids(history: list[dict]) -> list[int]:
    ids: list[int] = []
    for msg in history:
        if msg.get("type") != "ai":
            continue
        for match in _MCP_FACT_MARKER.finditer(str(msg.get("content") or "")):
            audit_id = int(match.group(1))
            if audit_id not in ids:
                ids.append(audit_id)
    return ids


def _render_fact_appendix(history: list[dict], fact_records: dict[int, dict]) -> str:
    audit_ids = _collect_audit_ids(history)
    if not audit_ids:
        return ""
    rows = []
    for audit_id in audit_ids:
        record = fact_records.get(audit_id)
        if not record:
            rows.append(f'<tr><td>M{audit_id}</td><td colspan="3">审计记录不可用</td></tr>')
            continue
        facts = [fact for fact in (record.get("facts") or []) if isinstance(fact, dict)]
        fact_bits = []
        for fact in facts[:6]:
            path = str(fact.get("path") or "/")
            value = fact.get("numeric_value")
            if value is None:
                value = fact.get("string_value")
            digest = str(fact.get("value_digest") or "").removeprefix("sha256:")[:12]
            cell = escape_html(path)
            if value is not None:
                cell += f" = {escape_html(str(value))}"
            cell += f' <span class="fact-digest">#{digest}</span>'
            fact_bits.append(f"<li>{cell}</li>")
        if len(facts) > 6:
            fact_bits.append(f"<li>… 共 {len(facts)} 项</li>")
        if not fact_bits:
            fact_bits.append("<li>（无抽取事实）</li>")
        tool = f"{record.get('server_slug') or '—'} · {record.get('capability_name') or ''}".strip(" ·")
        rows.append(
            f"<tr><td>M{audit_id}</td><td>{escape_html(tool)}</td>"
            f"<td>{escape_html(str(record.get('status') or '—'))}</td>"
            f'<td><ul class="fact-list">{"".join(fact_bits)}</ul></td></tr>'
        )
    return (
        '<section class="appendix"><h2 class="appendix-title">附录 · 事实核验记录（MCP-F）</h2>'
        '<table class="appendix-table"><thead><tr><th>引用</th><th>工具</th><th>状态</th>'
        "<th>抽取事实（路径 = 值 # 摘要）</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        '<p class="appendix-note">答案中的 [MCP-F] 标记已收敛为上标引用；'
        "本表为对应的服务端审计记录，供归档溯源。</p></section>"
    )


def _render_toc(rounds: list[dict]) -> str:
    items = []
    for index, entry in enumerate(rounds, 1):
        preview = _question_preview(entry["question"])
        if preview:
            items.append(f'<li><a href="#qa-{index}">{escape_html(preview)}</a></li>')
    if len(items) < 2:
        return ""
    return f'<nav class="toc"><p class="toc-title">目录</p><ol>{"".join(items)}</ol></nav>'


def _question_preview(question: dict | None) -> str:
    if question is None:
        return ""
    first_line = (
        str(question.get("content") or "").strip().splitlines()[0] if str(question.get("content") or "").strip() else ""
    )
    preview = re.sub(r"\s+", " ", first_line).strip()
    if len(preview) > 42:
        preview = preview[:42] + "…"
    return preview


def _attachment_summaries(question: dict) -> list[tuple[str, str]]:
    attachments = (question.get("extra_metadata") or {}).get("attachments") or []
    summaries = []
    for record in attachments[:5]:
        if not isinstance(record, dict):
            continue
        summaries.append((str(record.get("file_name") or "未命名附件"), _fmt_size(record.get("file_size"))))
    return summaries


# ── Markdown 渲染（XSS 防线 1/2 在此收口） ──────────────────────────────────


def _render_markdown(text: str) -> str:
    source = _LEDGER_BLOCK_RE.sub("", text)  # 事实账本目录块只应存在于工具观察，导出整体剥离
    repaired = _repair_flattened_fences(source)
    repaired = _repair_flattened_tables(repaired)
    tokens = _MD.parse(repaired)
    _sanitize_tokens(tokens)
    html = _MD.renderer.render(tokens, _MD.options, {})
    return _stylize_evidence_markers(html)


def _render_fence(self, tokens, idx, options, env) -> str:  # noqa: ANN001
    token = tokens[idx]
    info = (token.info or "").strip()
    lang = info.split()[0] if info else ""
    highlighted = highlight(token.content, _resolve_lexer(lang), _FORMATTER)
    label = f'<span class="code-lang">{escape_html(lang)}</span>' if lang else ""
    return f"<pre><code>{highlighted}</code>{label}</pre>\n"


def _render_math_inline(self, tokens, idx, options, env) -> str:  # noqa: ANN001
    return f'<span class="math-inline">{escape_html(tokens[idx].content)}</span>'


def _render_math_block(self, tokens, idx, options, env) -> str:  # noqa: ANN001
    return f'<div class="math-block">{escape_html(tokens[idx].content)}</div>\n'


def _resolve_lexer(lang: str):
    if not lang:
        return TextLexer()
    try:
        return get_lexer_by_name(lang)
    except Exception:  # noqa: BLE001 —— 未知语言标注按纯文本渲染，不做猜测
        return TextLexer()


def _sanitize_tokens(tokens) -> None:  # noqa: ANN001
    """html=False 已挡住原始 HTML 注入；这里再收敛 a/img 的协议面（链接与图片分开白名单）。"""
    for idx, token in enumerate(tokens):
        if token.children:
            _sanitize_tokens(token.children)
        if token.type == "link_open":
            if _safe_href(str(token.attrs.get("href") or "")):
                token.attrSet("rel", "noopener noreferrer")
            else:
                close = _matching_link_close(tokens, idx)
                _turn_into_text(token, "")
                if close is not None:
                    _turn_into_text(tokens[close], "")
        elif token.type == "image":
            if not _safe_img_src(str(token.attrs.get("src") or "")):
                _turn_into_text(token, token.content or "")


def _matching_link_close(tokens, open_idx: int) -> int | None:  # noqa: ANN001
    depth = 0
    for j in range(open_idx + 1, len(tokens)):
        if tokens[j].type == "link_open":
            depth += 1
        elif tokens[j].type == "link_close":
            if depth == 0:
                return j
            depth -= 1
    return None


def _turn_into_text(token, content: str) -> None:  # noqa: ANN001
    token.type = "text"
    token.tag = ""
    token.nesting = 0
    token.attrs = {}
    token.children = None
    token.content = content


def _safe_href(value: str) -> bool:
    candidate = value.strip()
    if not candidate or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        return False
    scheme = urlparse(candidate).scheme.lower()
    return scheme in _ALLOWED_HREF_SCHEMES or scheme == ""


def _safe_img_src(value: str) -> bool:
    """图片源白名单：在链接白名单之上放行 data:image/*（自包含内嵌的唯一通道）。

    markdown-it validateLink 在词法层已把 data: 限制到 image/(gif|png|jpeg|webp)，
    这里是第二层收敛。
    """
    candidate = value.strip()
    if not candidate or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        return False
    if candidate[:11].lower() == "data:image/":
        return True
    scheme = urlparse(candidate).scheme.lower()
    return scheme in _ALLOWED_IMG_SRC_SCHEMES or scheme == ""


# ── 上游保真修复（幂等：规范多行结构不触发，只修被问答流水线压扁的形态） ────
# 实测（docs/vibe/2026-09-21）：answer 流水线会把表格/代码块的换行 join 成单行，
# 段落间换行保留。上游根治前，导出侧按 GFM 语义重排；上游修复后本层自动变 no-op。


_FLATTENED_FENCE_RE = re.compile(r"^([^`]*)```([\w+#.-]*)\s+(.+?)\s*```([^`]*)$")
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SEPARATOR_CELL_RE = re.compile(r"^:?\s*(?:-+|–+|—+)\s*:?$")


def _repair_flattened_fences(content: str) -> str:
    """把压扁在一行的 ```lang … ``` 重排为标准围栏代码块。"""
    lines = []
    for line in content.split("\n"):
        match = _FLATTENED_FENCE_RE.match(line)
        if not match:
            lines.append(line)
            continue
        pre, lang, code, post = match.groups()
        if pre.strip():
            lines.append(pre.rstrip())
            lines.append("")
        lines.append(f"```{lang}")
        lines.append(code)
        lines.append("```")
        if post.strip():
            lines.append("")
            lines.append(post.lstrip())
    return "\n".join(lines)


def _fence_mask(lines) -> list[bool]:  # noqa: ANN001
    """标记真实围栏（```/~~~）覆盖的行，围栏内容不参与表格修复。"""
    masked = [False] * len(lines)
    active_char = ""
    for idx, line in enumerate(lines):
        match = _FENCE_OPEN_RE.match(line)
        masked[idx] = bool(active_char) or match is not None
        if match and not active_char:
            active_char = match.group(1)[0]
        elif match and active_char == match.group(1)[0] and not line[match.end() :].strip():
            active_char = ""
    return masked


def _split_pipe_row(text: str) -> list[str]:
    """按竖线（含全角｜）切分单元格；反引号代码跨度、转义竖线与〔〕证据标记内的竖线不切。"""
    source = text.strip()
    cells = []
    current = ""
    backtick_run = 0
    bracket_depth = 0
    index = 0
    while index < len(source):
        char = source[index]
        if char == "\\" and index + 1 < len(source) and source[index + 1] in "|｜":
            current += char + source[index + 1]
            index += 2
            continue
        if char == "`":
            end = index
            while end < len(source) and source[end] == "`":
                end += 1
            run = end - index
            if backtick_run == 0:
                backtick_run = run
            elif backtick_run == run:
                backtick_run = 0
            current += source[index:end]
            index = end
            continue
        if char == "〔":
            bracket_depth += 1
        elif char == "〕" and bracket_depth > 0:
            bracket_depth -= 1
        if char in "|｜" and backtick_run == 0 and bracket_depth == 0:
            cells.append(current.strip())
            current = ""
        else:
            current += char
        index += 1
    cells.append(current.strip())
    if cells and cells[0] == "":
        cells.pop(0)
    if cells and cells[-1] == "":
        cells.pop()
    return cells


def _repair_flattened_tables(content: str) -> str:
    """把压扁成单行的 GFM 表格（表头|分隔行|数据行全在一行，行间以空单元格分隔）重排为多行。

    仅在「同一行内出现 ≥2 个连续分隔单元格（---）且有表头与数据」这一无歧义形态下触发；
    规范的多行表格每行只含一行数据，永不匹配（幂等）。
    """
    lines = content.split("\n")
    masked = _fence_mask(lines)
    rebuilt = []
    for idx, line in enumerate(lines):
        if masked[idx] or ("|" not in line and "｜" not in line):
            rebuilt.append(line)
            continue
        rebuilt.append(_rebuild_single_line_table(line))
    return "\n".join(rebuilt)


def _rebuild_single_line_table(line: str) -> str:
    cells = _split_pipe_row(line)
    separator_run = None
    index = 0
    while index < len(cells):
        if _SEPARATOR_CELL_RE.match(cells[index]):
            end = index
            while end < len(cells) and _SEPARATOR_CELL_RE.match(cells[end]):
                end += 1
            if end - index >= 2 and separator_run is None:
                separator_run = (index, end)
            index = end
        else:
            index += 1
    if separator_run is None:
        return line
    sep_start, sep_end = separator_run
    header = cells[:sep_start]
    while header and header[-1] == "":
        header.pop()
    separators = cells[sep_start:sep_end]
    if len(header) < 2:
        return line

    rows = []
    current = []
    for cell in cells[sep_end:]:
        if cell == "":
            if current:
                rows.append(current)
                current = []
        else:
            current.append(cell)
    if current:
        rows.append(current)
    if not rows:
        return line

    width = max([len(header), len(separators), 2] + [len(row) for row in rows])

    def render_row(row: list[str], fill: str = "") -> str:
        padded = row[:width] + [fill] * (width - len(row))
        return "| " + " | ".join(padded) + " |"

    rebuilt = [render_row(header), render_row(separators, "---")]
    rebuilt.extend(render_row(row) for row in rows)
    return "\n".join(rebuilt)


# ── 协议标记注册表（对已渲染 HTML 后处理，内容已转义，不再二次转义） ──────────
# 权威芯片形态从 authority_markers 单点取用（〔证据E#｜…〕/〔引文定位｜…〕）；
# MCP-F 事实引用收敛为上标，审计明细由文末溯源附录承载。


_EVIDENCE_LIST_LABEL = "【证据引用】（后端渲染，页码来自证据锚点）"
_NOTE_RE = re.compile(r"（注：[^）]{0,400}）")


def _authority_chip(match) -> str:  # noqa: ANN001
    head = match.group(1)
    payload = match.group(0)[len(f"〔{head}｜") : -len("〕")]
    parts = payload.split("｜")
    locator = parts[0].strip() if parts else ""
    description = "｜".join(parts[1:]).strip() if len(parts) > 1 else ""
    title = f' title="{description}"' if description else ""
    label = f"{head} · {locator}" if locator else head
    return f'<span class="evidence-chip"{title}>{label}</span>'


def _fact_ref_chip(match) -> str:  # noqa: ANN001
    audit_ids: list[str] = []
    for audit_match in _MCP_FACT_MARKER.finditer(match.group(0)):
        audit_id = audit_match.group(1)
        if audit_id not in audit_ids:
            audit_ids.append(audit_id)
    labels = " ".join(f"M{audit_id}" for audit_id in audit_ids)
    return f'<sup class="fact-ref">{labels}</sup>'


def _stylize_evidence_markers(html: str) -> str:
    html = _MCP_FACT_RUN.sub(_fact_ref_chip, html)
    html = authority_marker_pattern().sub(_authority_chip, html)
    html = html.replace(_EVIDENCE_LIST_LABEL, '<span class="evidence-list-label">证据引用</span>')

    def note(match) -> str:  # noqa: ANN001
        inner = match.group(0)
        if "未在原文中定位" in inner or "请谨慎采信" in inner:
            return f'<span class="answer-note">{inner}</span>'
        return inner

    return _NOTE_RE.sub(note, html)


# ── 时间与尺寸格式化 ─────────────────────────────────────────────────────────


def _fmt_datetime(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_CN_TZ).strftime("%Y-%m-%d %H:%M")


def _fmt_timestamp(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _fmt_datetime(datetime.fromisoformat(value))
    except ValueError:
        return ""


def _fmt_size(value) -> str:  # noqa: ANN001
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        size = 0
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# ── 图片内嵌与审计回查（编排层异步预取；渲染层只消费注入的映射） ─────────────


def _normalize_image_bytes(data: bytes) -> tuple[str, bytes] | None:
    """归一化为内嵌友好的图片字节：限边、限质、限单图体积；不可解析返回 None。

    带透明的图保留 PNG，其余转 JPEG（科研 PDF 切图多为无透明 PNG，转 JPEG 收益显著）；
    归一化后仍超单图上限的，降一档质量重试，再超即放弃。
    """
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception:  # noqa: BLE001 —— 非图片字节不内嵌
        return None
    if getattr(image, "is_animated", False):
        return ("image/gif", data) if (image.format or "").upper() == "GIF" else None
    if max(image.size) > _MAX_IMAGE_EDGE:
        image.thumbnail((_MAX_IMAGE_EDGE, _MAX_IMAGE_EDGE))
    has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
    buffer = io.BytesIO()
    if has_alpha:
        image.save(buffer, format="PNG", optimize=True)
        mime, payload = "image/png", buffer.getvalue()
    else:
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.save(buffer, format="JPEG", quality=85, optimize=True)
        mime, payload = "image/jpeg", buffer.getvalue()
    if len(payload) > _PER_IMAGE_BYTES:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="JPEG", quality=70, optimize=True)
        mime, payload = "image/jpeg", buffer.getvalue()
    if len(payload) > _PER_IMAGE_BYTES:
        return None
    return mime, payload


async def _read_asset_bytes(resolved: dict) -> bytes | None:  # noqa: ANN001
    """按 stream_asset 同款读取方式把 MinIO 对象读全；超过读取上限返回 None。"""
    client = get_minio_client()
    stream = await client.adownload_response(resolved["bucket"], resolved["object_key"])
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = await asyncio.to_thread(stream.read, 1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_ASSET_READ_BYTES:
                return None
            chunks.append(chunk)
    finally:
        try:
            stream.close()
            stream.release_conn()
        except Exception:  # noqa: BLE001 —— 尽力释放连接
            pass
    return b"".join(chunks)


async def _lookup_kb_id(file_id: str) -> str | None:
    """内联 kbasset URI 不携带 kb_id，按 file_id 回查所属知识库（与 resolve_asset 内部同口径）。"""
    async with pg_manager.get_async_session_context() as session:
        return (
            (await session.execute(select(KnowledgeFile.kb_id).where(KnowledgeFile.file_id == file_id)))
            .scalars()
            .one_or_none()
        )


async def _kbasset_data_uri(target: dict, current_user: User) -> str | None:
    """鉴权解析 + 读取 + 归一化，产出 data URI；任一步失败返回 None（单图软降级）。"""
    kb_id = target.get("kb_id") or await _lookup_kb_id(target["file_id"])
    if not kb_id:
        return None
    try:
        resolved = await resolve_asset(
            kb_id=kb_id,
            file_id=target["file_id"],
            revision_id=target["revision_id"],
            asset_name=target["asset_name"],
            user=current_user,
        )
    except KnowledgeAssetError:
        return None
    data = await _read_asset_bytes(resolved)
    if data is None:
        return None
    normalized = _normalize_image_bytes(data)
    if normalized is None:
        return None
    mime, payload = normalized
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def _ordered_asset_targets(history: list[dict]) -> dict[str, dict]:
    """收集 kbasset 解析目标并按预算优先级排序：图组 primary → 子图 → 正文内联引用。

    预算不足时子图先降级（主图承载语义），同优先级保持载荷出现顺序（阅读序）。
    """
    figure_targets: list[tuple[int, str, dict]] = []
    inline_targets: list[tuple[str, dict]] = []
    for msg in history:
        if msg.get("type") == "human":
            continue
        for figure in ((msg.get("extra_metadata") or {}).get("citation_ready") or {}).get("figures") or []:
            if not isinstance(figure, dict):
                continue
            uri = _figure_asset_uri(figure)
            if not uri:
                continue
            file_id, revision_id, asset_name = _KBASSET_URI_RE.match(uri).groups()
            figure_targets.append(
                (
                    0 if figure.get("role") == "primary" else 1,
                    uri,
                    {
                        "kb_id": str(figure.get("kb_id") or "").strip(),
                        "file_id": file_id,
                        "revision_id": revision_id,
                        "asset_name": asset_name,
                    },
                )
            )
        for match in _KBASSET_URI_RE.finditer(str(msg.get("content") or "")):
            uri = match.group(0)
            file_id, revision_id, asset_name = match.groups()
            inline_targets.append(
                (uri, {"kb_id": "", "file_id": file_id, "revision_id": revision_id, "asset_name": asset_name})
            )

    asset_targets: dict[str, dict] = {}
    for _, uri, target in sorted(figure_targets, key=lambda item: item[0]):
        asset_targets.setdefault(uri, target)
    for uri, target in inline_targets:
        asset_targets.setdefault(uri, target)
    return asset_targets


async def _collect_embedded_images(history: list[dict], current_user: User) -> tuple[dict, dict]:
    """收集本会话全部可内嵌图片：用户消息图（base64）+ 图卡与正文 kbasset 资产。

    返回 (question_images: 消息id → data URI|None, kbassets: kbasset URI → data URI|None)；
    失败也保留键（值为 None），渲染层据此降级为占位并计入页脚统计。
    """
    question_images: dict[int, str | None] = {}
    question_sources: dict[int, str] = {}
    asset_targets = _ordered_asset_targets(history)
    for msg in history:
        if msg.get("type") == "human" and msg.get("image_content"):
            question_images[msg.get("id")] = None
            question_sources[msg.get("id")] = str(msg["image_content"])

    budget_left = _TOTAL_IMAGE_BUDGET
    kbassets: dict[str, str | None] = {}
    for uri, target in asset_targets.items():
        if budget_left <= 0:
            kbassets[uri] = None
            continue
        data_uri = await _kbasset_data_uri(target, current_user)
        if data_uri is None:
            kbassets[uri] = None
            continue
        encoded_bytes = (len(data_uri) * 3) // 4  # 预算按归一化后的原始字节计
        if encoded_bytes > budget_left:
            kbassets[uri] = None
            continue
        kbassets[uri] = data_uri
        budget_left -= encoded_bytes

    for msg_id, image_content in question_sources.items():
        try:
            raw = base64.b64decode(image_content, validate=False)
        except Exception:  # noqa: BLE001 —— 非法 base64 不内嵌
            continue
        normalized = _normalize_image_bytes(raw)
        if normalized is None or budget_left < len(normalized[1]):
            continue
        mime, payload = normalized
        budget_left -= len(payload)
        question_images[msg_id] = f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"
    return question_images, kbassets


async def _collect_fact_records(history: list[dict], current_user: User) -> dict[int, dict]:
    """按答案中出现的 MCP-F 审计 id 回查 MCPCallAudit（限定本人调用），供溯源附录渲染。"""
    audit_ids = _collect_audit_ids(history)
    if not audit_ids:
        return {}
    async with pg_manager.get_async_session_context() as session:
        rows = (
            (
                await session.execute(
                    select(MCPCallAudit).where(
                        MCPCallAudit.id.in_(audit_ids), MCPCallAudit.uid == str(current_user.uid)
                    )
                )
            )
            .scalars()
            .all()
        )
    records: dict[int, dict] = {}
    for row in rows:
        provenance = row.provenance if isinstance(row.provenance, dict) else {}
        facts = provenance.get("facts") if isinstance(provenance.get("facts"), list) else []
        records[int(row.id)] = {
            "server_slug": row.server_slug,
            "capability_name": row.capability_name,
            "status": row.status,
            "facts": facts,
        }
    return records


# ── pygments 浅色高亮（不覆盖代码块背景色） ─────────────────────────────────


class _ExportCodeStyle(Style):
    styles = {
        Comment: "italic #979999",
        Keyword: "bold #046a82",
        String: "#389e0d",
        Number: "#d48806",
        Name.Function: "#035065",
        Name.Class: "bold #035065",
        Name.Tag: "#046a82",
        Name.Attribute: "#035065",
        Operator: "#697070",
        Generic: "#4c4d4d",
    }


_FORMATTER = HtmlFormatter(style=_ExportCodeStyle, nowrap=True)
_PYGMENTS_CSS = "\n".join(
    line for line in _FORMATTER.get_style_defs(".qa-a pre code").splitlines() if "background" not in line
)

_MD = (
    MarkdownIt("commonmark", {"html": False, "breaks": True})
    .enable("table")
    .use(dollarmath_plugin, allow_space=True, allow_digits=True)
)
_MD.add_render_rule("fence", _render_fence)
_MD.add_render_rule("math_inline", _render_math_inline)
_MD.add_render_rule("math_block", _render_math_block)


# ── 文档模板（样式源自工作区样稿 styles.css，token 对齐 web base.css） ───────

_CSS = (
    """
:root {
  --ink: #1e1f1f;
  --ink-secondary: #4c4d4d;
  --muted: #697070;
  --paper: #ffffff;
  --page: #f6f9fa;
  --rule: #e4e6e6;
  --rule-soft: #eef0f0;
  --accent: #046a82;
  --accent-soft: #e1f6fb;
  --accent-border: #a3d8e8;
  --code-bg: #f5f7f7;
  --code-border: #e4e6e6;
  --warn-bg: #fffbe6;
  --warn-text: #ad6800;
  --font-sans: -apple-system, BlinkMacSystemFont, 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei',
    'HarmonyOS Sans SC', 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
  --font-mono: ui-monospace, 'Cascadia Code', 'SF Mono', Consolas, 'Liberation Mono', Menlo, monospace;
}

*, *::before, *::after { box-sizing: border-box; }

html { -webkit-text-size-adjust: 100%; }

body {
  margin: 0;
  background: var(--page);
  color: var(--ink);
  font-family: var(--font-sans);
  font-size: 15px;
  line-height: 1.65;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
}

.doc {
  max-width: 800px;
  margin: 0 auto;
  padding: 32px 28px 64px;
  background: var(--paper);
  box-shadow: 0 1px 0 rgba(30, 31, 31, 0.04), 0 8px 28px rgba(30, 31, 31, 0.04);
}

.brand-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 16px;
  border-bottom: 2px solid var(--accent);
  margin-bottom: 20px;
}

.brand-name {
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--accent);
}

.brand-badge {
  font-size: 12px;
  color: var(--muted);
  font-family: var(--font-mono);
}

.thread-title {
  margin: 0 0 8px;
  font-size: 24px;
  font-weight: 600;
  line-height: 1.35;
  color: var(--ink);
  letter-spacing: 0.01em;
}

.thread-subtitle {
  margin: 0 0 20px;
  font-size: 14px;
  color: var(--ink-secondary);
}

.meta-table {
  width: 100%;
  border-collapse: collapse;
  margin: 0 0 28px;
  font-size: 13px;
}

.meta-table th, .meta-table td {
  padding: 8px 12px;
  border: 1px solid var(--rule);
  text-align: left;
  vertical-align: top;
}

.meta-table th {
  width: 96px;
  background: var(--page);
  color: var(--muted);
  font-weight: 500;
  white-space: nowrap;
}

.meta-table td {
  color: var(--ink-secondary);
  font-family: var(--font-mono);
  font-size: 12px;
  word-break: break-all;
}

.toc {
  margin: 0 0 32px;
  padding: 14px 16px;
  background: var(--page);
  border: 1px solid var(--rule-soft);
  border-radius: 8px;
}

.toc-title {
  margin: 0 0 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
  letter-spacing: 0.06em;
}

.toc ol {
  margin: 0;
  padding-left: 1.25em;
  color: var(--ink-secondary);
  font-size: 13px;
  line-height: 1.8;
}

.toc a { color: var(--accent); text-decoration: none; }
.toc a:hover { text-decoration: underline; }

.qa-list {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.qa {
  break-inside: avoid;
  page-break-inside: avoid;
  border: 1px solid var(--rule);
  border-radius: 10px;
  overflow: hidden;
  background: var(--paper);
}

.qa-q {
  padding: 14px 18px 14px 16px;
  background: linear-gradient(90deg, var(--accent-soft) 0%, #f7fbfd 100%);
  border-bottom: 1px solid var(--accent-border);
  border-left: 4px solid var(--accent);
}

.qa-label {
  display: block;
  margin-bottom: 4px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.08em;
  color: var(--accent);
}

.qa-q-text {
  margin: 0;
  font-size: 15px;
  font-weight: 500;
  color: var(--ink);
  line-height: 1.55;
}

.qa-meta {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--muted);
  font-family: var(--font-mono);
}

.qa-a { padding: 16px 18px 18px; }
.qa-a > :first-child { margin-top: 0; }
.qa-a > :last-child { margin-bottom: 0; }

.qa-a p { margin: 0 0 12px; color: var(--ink); }

.qa-a h1, .qa-a h2, .qa-a h3, .qa-a h4 {
  margin: 20px 0 8px;
  font-weight: 600;
  color: var(--ink);
  line-height: 1.35;
}

.qa-a h1 { font-size: 18px; }
.qa-a h2 { font-size: 17px; }
.qa-a h3 { font-size: 16px; }
.qa-a h4 { font-size: 14px; color: var(--ink-secondary); }

.qa-a ul, .qa-a ol {
  margin: 0 0 12px;
  padding-left: 1.35em;
  color: var(--ink);
}

.qa-a li { margin-bottom: 4px; }
.qa-a li::marker { color: var(--muted); }

.qa-a blockquote {
  margin: 0 0 12px;
  padding: 8px 14px;
  border-left: 3px solid var(--accent-border);
  background: var(--page);
  color: var(--ink-secondary);
  font-size: 14px;
}

.qa-a blockquote p { margin: 0; color: inherit; }

.qa-a a {
  color: var(--accent);
  text-decoration: underline;
  text-underline-offset: 2px;
}

.qa-a hr { border: 0; border-top: 1px solid var(--rule); margin: 16px 0; }

.qa-a table {
  width: 100%;
  border-collapse: collapse;
  margin: 0 0 14px;
  font-size: 13px;
}

.qa-a th, .qa-a td {
  padding: 8px 10px;
  border: 1px solid var(--rule);
  text-align: left;
  vertical-align: top;
}

.qa-a thead th {
  background: var(--page);
  font-weight: 600;
  color: var(--ink-secondary);
  font-size: 12px;
}

.qa-a tbody tr:nth-child(even) td { background: #fafcfd; }

.qa-a code {
  font-family: var(--font-mono);
  font-size: 0.9em;
  padding: 0.1em 0.35em;
  background: var(--code-bg);
  border: 1px solid var(--rule-soft);
  border-radius: 4px;
  color: var(--ink-secondary);
}

.qa-a pre {
  position: relative;
  margin: 0 0 14px;
  padding: 14px 16px;
  background: var(--code-bg);
  border: 1px solid var(--code-border);
  border-radius: 8px;
  overflow-x: auto;
  break-inside: avoid;
  page-break-inside: avoid;
}

.qa-a pre code {
  display: block;
  padding: 0;
  background: none;
  border: 0;
  border-radius: 0;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--ink);
  white-space: pre;
}

.code-lang {
  position: absolute;
  top: 8px;
  right: 10px;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--muted);
  letter-spacing: 0.04em;
  user-select: none;
}

.math-inline {
  font-family: var(--font-mono);
  font-size: 0.92em;
  padding: 0.05em 0.3em;
  background: var(--accent-soft);
  border-radius: 3px;
  color: var(--ink);
  white-space: nowrap;
}

.math-block {
  margin: 0 0 14px;
  padding: 12px 14px;
  text-align: center;
  background: var(--accent-soft);
  border: 1px solid var(--accent-border);
  border-radius: 8px;
  font-family: var(--font-mono);
  font-size: 14px;
  color: var(--ink);
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-x: auto;
  break-inside: avoid;
}

.math-block::before {
  content: '公式';
  display: block;
  margin-bottom: 6px;
  font-family: var(--font-sans);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.08em;
  color: var(--accent);
}

.answer-error {
  margin: 0 0 12px;
  padding: 10px 12px;
  background: var(--warn-bg);
  border: 1px solid #ffe58f;
  border-radius: 6px;
  font-size: 13px;
  color: var(--warn-text);
}

.evidence-chip {
  display: inline-block;
  margin: 0 2px;
  padding: 0 6px;
  border: 1px solid var(--accent-border);
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 12px;
  font-family: var(--font-mono);
  white-space: nowrap;
}

.evidence-list-label {
  display: inline-block;
  margin: 2px 0;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--ink-secondary);
}

.answer-note {
  display: block;
  margin: 4px 0;
  padding: 8px 12px;
  background: var(--warn-bg);
  border: 1px solid #ffe58f;
  border-radius: 6px;
  font-size: 12.5px;
  color: var(--warn-text);
}

.question-image {
  display: block;
  max-width: 360px;
  max-height: 240px;
  margin-top: 8px;
  border: 1px solid var(--rule);
  border-radius: 8px;
}

.figure-card {
  margin: 14px 0;
  text-align: center;
}

.figure-card img {
  max-width: 100%;
  max-height: 520px;
  border: 1px solid var(--rule);
  border-radius: 8px;
  background: var(--paper);
}

.figure-card figcaption {
  margin-top: 6px;
  font-size: 12.5px;
  color: var(--ink-secondary);
}

.figure-page {
  display: inline-block;
  margin-left: 8px;
  padding: 0 6px;
  border: 1px solid var(--accent-border);
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: 11px;
}

.figure-missing {
  padding: 14px 16px;
  border: 1px dashed var(--rule);
  border-radius: 8px;
  background: var(--page);
}

.figure-missing-note {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  color: var(--muted);
}

.figure-panels {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
  margin-top: 10px;
}

.figure-panel {
  position: relative;
  display: inline-flex;
}

.figure-panel img {
  display: block;
  max-width: 200px;
  max-height: 140px;
  border: 1px solid var(--rule);
  border-radius: 6px;
  background: var(--paper);
}

.figure-panel i {
  position: absolute;
  top: 4px;
  left: 4px;
  padding: 0 5px;
  border-radius: 4px;
  background: rgba(30, 31, 31, 0.66);
  color: #ffffff;
  font-family: var(--font-mono);
  font-size: 10.5px;
  font-style: normal;
  line-height: 1.5;
}

.figure-primary-missing figcaption {
  margin-bottom: 8px;
  text-align: center;
}

.fact-ref {
  margin: 0 1px;
  padding: 0 4px;
  border-radius: 3px;
  background: var(--accent-soft);
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: 10.5px;
  vertical-align: super;
  line-height: 1;
  white-space: nowrap;
}

.appendix {
  margin-top: 36px;
}

.appendix-title {
  margin: 0 0 12px;
  padding-top: 16px;
  border-top: 1px solid var(--rule);
  font-size: 16px;
  font-weight: 600;
  color: var(--ink);
}

.appendix-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}

.appendix-table th,
.appendix-table td {
  padding: 8px 10px;
  border: 1px solid var(--rule);
  text-align: left;
  vertical-align: top;
}

.appendix-table thead th {
  background: var(--page);
  font-weight: 600;
  color: var(--ink-secondary);
  font-size: 12px;
  white-space: nowrap;
}

.appendix-table td:first-child {
  font-family: var(--font-mono);
  white-space: nowrap;
}

.fact-list {
  margin: 0;
  padding-left: 1.2em;
}

.fact-list li {
  margin-bottom: 2px;
  font-family: var(--font-mono);
  font-size: 11.5px;
  word-break: break-all;
}

.fact-digest {
  color: var(--muted);
}

.appendix-note {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--muted);
}

.doc-footer {
  margin-top: 36px;
  padding-top: 16px;
  border-top: 1px solid var(--rule);
  font-size: 12px;
  color: var(--muted);
  line-height: 1.7;
}

@media print {
  body { background: #fff; }
  .doc { max-width: none; margin: 0; padding: 0; box-shadow: none; }
  .toc { break-inside: avoid; }
  .toc a { color: var(--ink-secondary); text-decoration: none; }
  .qa { border-color: #ccc; }
  .qa-q {
    background: #f3f7f8 !important;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
  }
  .qa-a pre, .math-block { break-inside: avoid; page-break-inside: avoid; }
  .qa-a a { color: var(--ink); text-decoration: underline; }
}

@media (max-width: 840px) {
  .doc { padding: 20px 16px 40px; }
  .thread-title { font-size: 20px; }
  .meta-table th, .meta-table td { display: block; width: 100%; }
  .meta-table th { border-bottom: 0; padding-bottom: 2px; }
  .meta-table td { border-top: 0; padding-top: 2px; }
}
"""
    + "\n"
    + _PYGMENTS_CSS
)


_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
<meta name="generator" content="Yuxi conversation-export/1.0">
<title>$title</title>
<style>
$css
</style>
</head>
<body>
<div class="doc">
  <div class="brand-bar">
    <span class="brand-name">语析 · 对话导出</span>
    <span class="brand-badge">CONVERSATION EXPORT</span>
  </div>
  <h1 class="thread-title">$title</h1>
  <p class="thread-subtitle">$subtitle</p>
  <table class="meta-table">
    $meta_rows
  </table>
  $toc
  <div class="qa-list">
$cards
  </div>
$appendix
  <div class="doc-footer">$footer</div>
</div>
</body>
</html>
"""
)
