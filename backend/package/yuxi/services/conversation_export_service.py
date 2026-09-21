"""会话 HTML 导出：把一段对话的问答渲染为自包含、零脚本、打印友好的 HTML 文件。

设计约束（docs/vibe/2026-09-21-conversation-html-export.md，V1b）：
- 服务端统一渲染，Web 与桌面端拿到字节一致；
- 零 JS、零外部资源（内联 CSS、系统字体、自带 CSP），离线可开，打印成 PDF 排版正确；
- XSS 三层防线：MarkdownIt 以 html=False 渲染（原始 HTML 一律转义为文本）→
  a[href]/img[src] 协议白名单（其余降级为纯文本）→ 文档级 CSP 兜底；
- 公式走 dollarmath 结构化呈现（.math-inline/.math-block，不引入 KaTeX 字体）；
- 代码块走 pygments 服务端高亮（自定义浅色样式对齐 web base.css 色阶）。
"""

import json
import re
from datetime import datetime, timedelta, timezone, UTC
from html import escape as escape_html
from string import Template
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_by_name
from pygments.style import Style
from pygments.token import Comment, Generic, Keyword, Name, Number, Operator, String
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.services.conversation_service import get_thread_history_view
from yuxi.services.operation_log_service import log_operation
from yuxi.storage.postgres.models_business import User

_CN_TZ = timezone(timedelta(hours=8))
_ALLOWED_HREF_SCHEMES = {"http", "https", "mailto"}


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
    exported_at = datetime.now(_CN_TZ)
    html = render_conversation_html(
        title=conversation.title,
        agent_slug=conversation.agent_id,
        thread_id=conversation.thread_id,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        history=history,
        exported_at=exported_at,
    )
    filename = build_filename(conversation.title, exported_at)
    await log_operation(
        db,
        current_user.id,
        "导出会话HTML",
        json.dumps({"thread_id": thread_id, "messages": len(history)}, ensure_ascii=False),
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
    return f"语析对话_{cleaned}_{moment:%Y%m%d-%H%M}.html"


def render_conversation_html(
    *,
    title: str | None,
    agent_slug: str,
    thread_id: str,
    created_at: datetime | None,
    updated_at: datetime | None,
    history: list[dict],
    exported_at: datetime,
) -> str:
    """把会话元数据与 history 视图渲染为完整 HTML 文档（纯函数，不碰 DB/IO）。"""
    rounds = _pair_rounds(history)
    cards = [card for card in (_render_qa_card(index, entry) for index, entry in enumerate(rounds, 1)) if card]
    toc = _render_toc(rounds)
    display_title = (title or "").strip() or "未命名会话"
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
    footer = "由语析（Yuxi）导出 · 零脚本自包含文档（无外部资源，可离线查看）· 打印（Ctrl+P）可另存为排版正确的 PDF"
    return _TEMPLATE.safe_substitute(
        title=escape_html(display_title),
        subtitle=f"共 {len(cards)} 轮问答 · {len(history)} 条消息 · 时间均为 UTC+8",
        meta_rows=meta_rows,
        toc=toc,
        cards="\n".join(cards),
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


def _render_qa_card(index: int, entry: dict) -> str:
    question = entry["question"]
    question_html = ""
    if question is not None:
        question_text = escape_html(str(question.get("content") or "")).replace("\n", "<br>")
        meta_bits = []
        stamp = _fmt_timestamp(question.get("created_at"))
        if stamp:
            meta_bits.append(stamp)
        if question.get("image_content"):
            meta_bits.append("含图片消息（图片不内联）")
        meta_bits.extend(f"附件 {name}（{size}）" for name, size in _attachment_summaries(question))
        meta_html = f'<p class="qa-meta">{escape_html(" · ".join(meta_bits))}</p>' if meta_bits else ""
        question_html = (
            '<div class="qa-q">'
            f'<span class="qa-label">问题 {index:02d}</span>'
            f'<p class="qa-q-text">{question_text or "（空提问）"}</p>'
            f"{meta_html}"
            "</div>"
        )

    answer_parts = []
    for answer in entry["answers"]:
        content = str(answer.get("content") or "")
        if content.strip():
            answer_parts.append(_render_markdown(content))
        else:
            error = str(answer.get("error_message") or "").strip()
            if error:
                answer_parts.append(f'<p class="answer-error">本轮回答生成失败：{escape_html(error)}</p>')
    if not answer_parts:
        return ""
    return (
        f'<section class="qa" id="qa-{index}">{question_html}<div class="qa-a">{"".join(answer_parts)}</div></section>'
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
    tokens = _MD.parse(text)
    _sanitize_tokens(tokens)
    return _MD.renderer.render(tokens, _MD.options, {})


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
    """html=False 已挡住原始 HTML 注入；这里再收敛 a/img 的协议面。"""
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
            if not _safe_href(str(token.attrs.get("src") or "")):
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
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
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
  <div class="doc-footer">$footer</div>
</div>
</body>
</html>
"""
)
