"""L1/L2/L3 编译产物渲染器（语义移植自 PaperAgent ``paperkb/compile.py`` 渲染段）。

产物形态与上游一致：_note（L1：one_liner + 六维 + 核心概念）、_wiki（L2：三节
深度分析）、_relations（L3：概念关系图）。Obsidian 兼容 frontmatter 保留
（P3 导入器按此形态识别回灌）。
"""

from __future__ import annotations

import re

from yuxi.literature.identity.rid import doi_to_dirname


def _six(data: dict, key: str) -> str:
    item = data.get(key) or {}
    if isinstance(item, str):
        return item
    text = str(item.get("text") or "")
    paras = item.get("paras") or []
    return text + (" " + " ".join(f"[{p}]" for p in paras if p) if paras else "")


def render_note(meta: dict, data: dict) -> str:
    """渲染 L1 笔记（`_note`）。上游教训：概念必须带定义（L3 拿不到定义锚点=死链）。"""
    tags = [str(t) for t in (data.get("tags") or [])[:8]]
    lines = [
        f"---\ntype: paper-note\ndoi: {meta.get('doi', '')}\n"
        + (f"tags: [{', '.join(tags)}]\n" if tags else "")
        + "---\n",
        f"# {meta.get('title', '')}\n",
        f"**一句话贡献**：{data.get('one_liner', '')}\n",
    ]
    for key, label in (
        ("background", "背景"),
        ("method", "方法"),
        ("result", "结果"),
        ("conclusion", "结论"),
        ("innovation", "创新"),
        ("limitation", "局限"),
    ):
        content = _six(data, key)
        if content:
            lines.append(f"## {label}\n{content}\n")
    concepts = data.get("concepts") or []
    if concepts:
        lines.append("## 核心概念")
        for c in concepts:
            if isinstance(c, dict) and c.get("name"):
                lines.append(f"- **{c['name']}**：{c.get('definition', '')}")
        lines.append("")
    if data.get("ai_value") is not None:
        lines.append(f"## AI 评分\nai_value: {data.get('ai_value')}\n")
    return "\n".join(lines)


def render_wiki(meta: dict, data: dict) -> str:
    """渲染 L2 深度卡（`_wiki`）：方法论批判 / 可复现性 / 应用转化。"""
    lines = [
        f"---\ntype: paper-wiki\ndoi: {meta.get('doi', '')}\n---\n",
        f"# 深度分析：{meta.get('title', '')}\n",
        str(data.get("wiki") or ""),
    ]
    return "\n".join(lines) + "\n"


def render_relations(meta: dict, data: dict, related_ctxs: list[dict]) -> str:
    """渲染 L3 关系卡（`_relations`）。引用目标=目录名（DOI 的 "/" → "_"，Obsidian 活链）。"""
    self_dir = doi_to_dirname(meta.get("doi", ""))

    def _ref(p: str) -> str:
        s = (p or "").strip()
        m = re.search(r"(10\.\d{4,9}/[^\s（）()]+)", s)
        if m:
            return doi_to_dirname(m.group(1))
        if s.startswith("本文"):
            return self_dir
        return doi_to_dirname(s)

    lines = [
        f"---\ntype: paper-relations\ndoi: {meta.get('doi', '')}\n---\n",
        f"# 概念关系分析：{meta.get('title', '')}\n",
        f"## 研究簇概述\n{data.get('summary', '')}\n",
    ]
    for cm in data.get("concept_map") or []:
        papers = ", ".join(f"[[{_ref(p)}/_note]]" for p in (cm.get("papers") or [])[:5])
        lines.append(f"### {cm.get('concept', '')}\n- 相关文献：{papers}\n- 演进：{cm.get('evolution', '')}\n")
    for mc in data.get("methodology_connections") or []:
        lines.append(
            f"- [[{_ref(mc.get('from', ''))}/_note]] → [[{_ref(mc.get('to', ''))}/_note]]：{mc.get('relation', '')}"
        )
    if data.get("research_trajectory"):
        lines.append(f"\n## 研究趋势\n{data['research_trajectory']}\n")
    lines.append("\n## 相关文献")
    for i, r in enumerate(related_ctxs, 1):
        lines.append(f"{i}. [[{doi_to_dirname(r['doi'])}/_note]]（{r.get('connection', '')}）")
    return "\n".join(lines) + "\n"
