"""输出模板注册表（P1，ADR-0010 派生物边界）。

对标上游 PaperAgent ``packages/paperparse/templates/*.md.j2`` 的三种用户可选
模板（obsidian_bilingual / obsidian_bilingual_alt / plain），固化其**精确输出格式**
：

===========  ==============================  =========================
模板          段落渲染                         空参考文献
===========  ==============================  =========================
bilingual     EN 空行 ZH（无标记）             （参考文献未解析）
bilingual_alt ``**EN**`` / ``**ZH**`` 标记    无兜底
plain         仅 EN（纯英文，不出译文）          无兜底
===========  ==============================  =========================

与上游的两处**刻意差异**：

1. **纯渲染，不调 LLM。** 上游 ``rerender_paper`` 重渲染会重新走翻译链；Yuxi 的
   模板只是把**已有**的 para_id 对齐原文/译文做排版，因此切换模板零 token 成本、
   不产生新副作用，纯函数、可重复（ADR-0010 要求派生物不得反哺证据）。
2. **不经 vendor 网关。** 上游模板是 Jinja2 文件，本模块是原生 Python 实现——
   保持"paperparse 只经 gateway.py 唯一入口"的 ADR-0010 L0-1 门禁。

安全边界：模板产物是**派生物**，只进 NAVIGATION_ONLY 阅读通道，绝不进入
``EvidenceEnvelope``（ADR-0010 决议 2/3）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

#: 模板稳定身份（进配置契约；重渲染不消耗 token，故可安全改选）
TEMPLATE_BILINGUAL = "obsidian_bilingual"
TEMPLATE_BILINGUAL_ALT = "obsidian_bilingual_alt"
TEMPLATE_PLAIN = "plain"

DEFAULT_TEMPLATE = TEMPLATE_BILINGUAL

#: 空参考文献兜底文案（仅 bilingual 有；上游 alt/plain 无兜底，见模板表）
EMPTY_REFERENCES_PLACEHOLDER = "（参考文献未解析）"


@dataclass(frozen=True)
class TemplateSpec:
    """模板的稳定身份与展示元数据（进配置契约，前端据此渲染选择器）。"""

    key: str
    label: str
    description: str
    bilingual: bool


TEMPLATES: dict[str, TemplateSpec] = {
    spec.key: spec
    for spec in (
        TemplateSpec(
            key=TEMPLATE_BILINGUAL,
            label="双语对照（默认）",
            description="英文在上、中文在下，段落间空行分隔；参考文献缺失时给出提示。",
            bilingual=True,
        ),
        TemplateSpec(
            key=TEMPLATE_BILINGUAL_ALT,
            label="双语对照（带 EN/ZH 标记）",
            description="每段显式标注 **EN** / **ZH**，适合快速扫读区分语种。",
            bilingual=True,
        ),
        TemplateSpec(
            key=TEMPLATE_PLAIN,
            label="仅英文",
            description="只输出原文，不含译文；用于校对与二次编辑。",
            bilingual=False,
        ),
    )
}


def is_known_template(key: str) -> bool:
    return str(key or "") in TEMPLATES


def resolve_template(key: str | None) -> TemplateSpec:
    """未知模板 fail-closed 回落到默认模板（不静默套用近似模板产生错排版）。"""
    return TEMPLATES.get(str(key or "").strip(), TEMPLATES[DEFAULT_TEMPLATE])


def list_templates() -> list[dict[str, Any]]:
    """供前端选择器消费的模板清单（含默认标记）。"""
    return [{**spec.__dict__, "default": spec.key == DEFAULT_TEMPLATE} for spec in TEMPLATES.values()]


def alignment_items(alignment: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把持久化的 alignment 段（original/translated）转成模板可渲染的段落列表。

    这是**重渲染零 LLM 成本**的关键：alignment.json 已由翻译任务落盘，
    切换模板只需重新排版这份既有对齐结果，不再调用模型。
    """
    items: list[dict[str, Any]] = []
    for row in alignment or []:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "type": "paragraph",
                "text_en": str(row.get("original") or ""),
                "text_zh": str(row.get("translated") or ""),
            }
        )
    return items


def rerender_from_alignment(
    alignment: list[dict[str, Any]],
    *,
    template: str | None = None,
    title: str = "",
) -> str:
    """用已落盘的段落对齐结果按新模板重排——**纯函数，不触碰 LLM**。

    对标上游 ``rerender_paper``：上游重渲染会重跑翻译链（重新计费），Yuxi 只重排版。
    """
    return render_markdown(
        alignment_items(alignment), references=None, template=template, title=title
    )


def _para_text(item: dict[str, Any]) -> tuple[str, str]:
    """取 (原文, 译文)；兼容 Yuxi 的 paragraph 行结构与上游 items 结构。"""
    text_en = str(item.get("text_en") or item.get("source_text") or item.get("text") or "")
    text_zh = str(item.get("text_zh") or item.get("translated_text") or "")
    return text_en, text_zh


def _render_paragraphs(items: list[dict[str, Any]], *, bilingual: bool, labeled: bool) -> list[str]:
    """按模板风格渲染段落块（heading / figure / 正文三类）。"""
    lines: list[str] = []
    for item in items:
        kind = str(item.get("type") or "")
        if kind == "heading":
            lines.append(f"## {item.get('text') or item.get('text_en') or ''}")
            continue
        if kind == "figure":
            lines.append(f"![]({item.get('file') or ''})")
            continue
        text_en, text_zh = _para_text(item)
        if not text_en:
            # 纯译文行（无原文）只在双语模板下有意义
            if bilingual and text_zh:
                lines.append(f"{'**ZH** ' if labeled else ''}{text_zh}")
            continue
        if not bilingual:
            lines.append(text_en)
        elif labeled:
            lines.append(f"**EN** {text_en}")
            if text_zh:
                lines.append(f"**ZH** {text_zh}")
        else:
            lines.append(text_en)
            if text_zh:
                lines.append(text_zh)
    return lines


def _render_references(references: list[Any], *, placeholder: str) -> list[str]:
    """参考文献段；``placeholder`` 为空时无兜底（对齐上游 alt/plain 行为）。"""
    lines = ["## References"]
    if not references:
        if placeholder:
            lines.append(placeholder)
        return lines
    for index, ref in enumerate(references, 1):
        if isinstance(ref, dict):
            number = ref.get("number") or index
            text = str(ref.get("raw_text") or ref.get("text") or "").strip()
        else:
            number, text = index, str(ref).strip()
        lines.append(f"{number}. {text}")
    return lines


_RENDERERS: dict[str, Callable[[list[dict[str, Any]], list[Any]], list[str]]] = {
    TEMPLATE_BILINGUAL: lambda items, refs: _render_paragraphs(items, bilingual=True, labeled=False)
    + _render_references(refs, placeholder=EMPTY_REFERENCES_PLACEHOLDER),
    TEMPLATE_BILINGUAL_ALT: lambda items, refs: _render_paragraphs(items, bilingual=True, labeled=True)
    + _render_references(refs, placeholder=""),
    TEMPLATE_PLAIN: lambda items, refs: _render_paragraphs(items, bilingual=False, labeled=False)
    + _render_references(refs, placeholder=""),
}


def render_markdown(
    items: list[dict[str, Any]],
    *,
    references: list[Any] | None = None,
    template: str | None = None,
    title: str = "",
    frontmatter: str = "",
) -> str:
    """按模板渲染双语/纯英文 Markdown——**纯函数，零 LLM 调用**。

    ``items`` 为已对齐的段落列表（``text_en``/``text_zh`` 或 ``text``/
    ``translated_text``）。切换模板只是重新排版同一份内容，不重新翻译，
    因此不产生 token 消耗与副作用，可在 UI 上反复切换。
    """
    spec = resolve_template(template)
    renderer = _RENDERERS[spec.key]
    blocks: list[str] = [f"# {title or 'Untitled'}"]
    body = renderer([item for item in items if isinstance(item, dict)], list(references or []))
    blocks.extend(body)
    text = "\n\n".join(block.strip("\n") for block in blocks if str(block).strip())
    prefix = f"{frontmatter.rstrip()}\n" if frontmatter.strip() else ""
    return f"{prefix}{text}\n"