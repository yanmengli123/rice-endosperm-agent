"""翻译提示词（语义移植自 PaperAgent ``paperkb/translate/pipeline.py`` + ``context.py``）。

保真要点（ADR-0010 决议 5b）：[[MATHn]] 标签原样指代公式（不展开不改写）、
严格 JSON 输出、共享全文前缀 + TASK_MARK 分界（与 notes.prompts 同机制）。
"""

from __future__ import annotations

from yuxi.literature.notes.prompts import TASK_MARK, split_task, with_task

__all__ = ["TASK_MARK", "build_shared_prefix_with_math", "split_task", "translate_task", "with_task"]

CTX_HEADER = (
    "你将处理一篇学术论文。以下是论文全文上下文（标题 + 章节 + 干净正文，已跳过 References）。\n"
    "正文中的公式已用 [[MATHn]] 标签占位（公式对照附后，翻译时**用标签原样指代、绝不展开改写**）；"
    "段落以 [Pxxx]（para_id）标识，引用段落一律使用 [Pxxx]。\n\n"
    "## 论文全文\n"
)


def build_shared_prefix_with_math(paragraphs: list[str]) -> tuple[str, list[str]]:
    """共享全文前缀：段落逐条 [Pxxx] 编号，公式经 latextap 打 [[MATHn]] 标签。

    段内局部编号（[[MATH0..k]]）**单趟正则**重映射为全局编号（跨段连续、与
    math_list 一一对应）——顺序 replace 会发生碰撞（[[MATH0]]→[[MATH1]] 后
    被下一个局部标签的替换再次命中），test_translation 钉死此契约。
    """
    lines = [CTX_HEADER]
    labeled, math_list = label_paragraphs(paragraphs)
    lines.extend(f"[P{i:03d}] {para}" for i, para in enumerate(labeled, 1))
    return "\n".join(lines), math_list


def label_paragraphs(paragraphs: list[str]) -> tuple[list[str], list[str]]:
    """One global formula numbering shared by context and bounded batches."""
    import re

    from yuxi.literature.translate.latextap import split_text

    result = []
    math_list: list[str] = []
    for para in paragraphs:
        labeled, tags = split_text(para)
        offset = len(math_list)
        labeled = re.sub(r"\[\[MATH(\d+)\]\]", lambda m: f"[[MATH{offset + int(m.group(1))}]]", labeled)
        math_list.extend(tags)
        result.append(labeled)
    return result, math_list


def translate_task(para_ids: list[str]) -> str:
    """译文任务后缀（上游 _translate_task 逐字移植；D16：译文阶段不生成总结）。"""
    ids = ", ".join(str(p) for p in para_ids if p)
    return (
        "## 翻译任务\n"
        "请将上方论文全文中的以下段落翻译为中文。译文里**用 [[MATHn]] 标签原样指代公式**，"
        "绝不展开、绝不改写 LaTeX；保留该段所有 [[MATHn]] 标签（顺序一致）。\n"
        "题注段按图/表题注译（Figure N.→图 N.）。\n"
        f"待译段落（para_id）：{ids}\n"
        "输出严格 JSON：\n"
        '{"translations": [{"para_id": "P001", "zh": "..."}]}\n'
        "若某公式疑似有误，在对应 zh 末尾加 [[NOTE]] 说明，不要改标签。"
    )
