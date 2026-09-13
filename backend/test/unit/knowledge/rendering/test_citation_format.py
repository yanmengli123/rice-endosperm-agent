"""格式保全回归（P5/P6）：三份真实错乱答案的黄金语料 + 幂等 + 结构不变量。

2026-09 格式错乱事故的四个病灶：
1. 反向绑定 rstrip 吞换行 → 标题粘连（〔证据E6…〕## 文献与位置）；
2. 芯片不感知 Markdown 结构 → 表格/标题被破坏；
3. 占位符泄漏（[citation omitted] 上屏）与 fail-closed 标记刷屏；
4. 未定位提示裸 30 字符截断 + 重复追加（不幂等）。
"""

from yuxi.knowledge.rendering.citation_channel import (
    HISTORY_CITATION_PLACEHOLDER,
    NARRATIVE_LOCATOR_MARKER,
    apply_citation_channel,
    reverse_bind_citations,
)

SANT_QUOTE = (
    "Rice endosperm starch biosynthesis is a critical factor. The structure of OsMYB73 "
    "protein was also predicted and the results revealed that it has two typical SANT "
    "domains between 115-164 and 167-215 amino acids."
)
GEL_QUOTE = "The wild-type and the two mutants were having soft gel consistency."


def _citation(ref: str, page: int, quote: str, *, zone: str = "MAIN_TEXT") -> dict:
    import unicodedata

    def _norm(text: str) -> str:
        value = unicodedata.normalize("NFKC", text)
        import re

        return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", value.casefold())).strip()

    return {
        "ref": ref,
        "evidence_id": f"ev-{ref}",
        "kb_id": "kb-1",
        "file_id": "file-1",
        "filename": "paper.pdf",
        "zone": zone,
        "page_numbers": [page],
        "primary_page": page,
        "quote_head": quote[:80],
        "anchor_ids": [f"ea-{ref}"],
        "locatable": True,
        "toc_line": False,
        "secondary_of": None,
        "_quote_norm": _norm(quote),
    }


CITATIONS = [_citation("E1", 3, SANT_QUOTE), _citation("E2", 6, GEL_QUOTE)]


# ---------- 病灶 1：换行保全，标题永不粘连 ----------


def test_golden_no_heading_gluing_from_reverse_binding():
    """事故原样：句末换行 + 下一行标题。芯片必须在换行前，标题行首完整保留。"""
    text = "OsMYB73 含两个 SANT 结构域，位于 115-164 与 167-215 氨基酸。\n## 文献与位置\n- 文献：Liu 等 (2024)"
    guarded, validation = apply_citation_channel(text, CITATIONS)
    lines = guarded.split("\n")
    # 标题行完整且在独立行（事故中它被粘成 〔证据E6…〕## 文献与位置）
    assert any(line.strip() == "## 文献与位置" for line in lines), guarded
    chip_line = next(line for line in lines if "〔证据E1" in line)
    assert chip_line.rstrip().endswith("〕")
    assert not chip_line.startswith("##")
    assert validation["reverse_bound"] == 1


def test_golden_paragraph_gap_preserved():
    text = "OsMYB73 的 SANT 结构域位于 115-164 aa。\n\n下一段落开始。"
    guarded, _ = apply_citation_channel(text, CITATIONS)
    # 段落间空行保留（事故中被 rstrip 吞掉导致两段粘连）
    assert "\n\n" in guarded
    assert guarded.split("\n\n")[0].rstrip().endswith("〕")


# ---------- 病灶 2：Markdown 结构保全 ----------


def test_golden_table_rows_never_get_inline_chips():
    text = "| 结构域序号 | 氨基酸位置区间 |\n| --- | --- |\n| SANT 结构域 1 | 115-164 |\n| SANT 结构域 2 | 167-215 |\n"
    guarded, validation = apply_citation_channel(text, CITATIONS)
    table_lines = [line for line in guarded.split("\n") if line.strip().startswith("|")]
    assert len(table_lines) == 4  # 表格行数不变
    assert all("〔证据" not in line for line in table_lines)  # 行内不插芯片
    footnote = [line for line in guarded.split("\n") if line.startswith("> 表格依据：")]
    assert footnote and "〔证据E1" in footnote[0]  # 脚注在表后
    assert validation["reverse_bound"] >= 1


def test_golden_code_fence_untouched():
    text = "OsMYB73 的结构域见下：\n\n```python\n# OsMYB73 SANT domains 115-164\nprint(167-215)\n```\n"
    guarded, _ = apply_citation_channel(text, CITATIONS)
    assert "# OsMYB73 SANT domains 115-164" in guarded
    assert "print(167-215)" in guarded
    fence_lines = [line for line in guarded.split("\n") if line.strip().startswith("```")]
    assert len(fence_lines) == 2
    assert all("〔证据" not in line for line in guarded.split("\n") if "print(" in line)


def test_golden_heading_lines_never_bound():
    text = "## OsMYB73 的 115-164 与 167-215 结构域\n正文内容。"
    guarded, _ = apply_citation_channel(text, CITATIONS)
    assert guarded.split("\n")[0].startswith("## ")
    assert "〔证据" not in guarded.split("\n")[0]


# ---------- 病灶 3：占位符终点 ----------


def test_golden_citation_omitted_never_reaches_display():
    text = f"这是正文描述 {HISTORY_CITATION_PLACEHOLDER}，继续叙述。\n另一句也带 {HISTORY_CITATION_PLACEHOLDER}。"
    guarded, validation = apply_citation_channel(text, CITATIONS)
    assert HISTORY_CITATION_PLACEHOLDER not in guarded
    assert validation["display_placeholders_stripped"] == 2


def test_golden_marker_spam_collapsed_to_end_notice():
    text = (
        f"句子一 {NARRATIVE_LOCATOR_MARKER}。句子二 {NARRATIVE_LOCATOR_MARKER}。\n句子三 {NARRATIVE_LOCATOR_MARKER}。"
    )
    guarded, validation = apply_citation_channel(text, CITATIONS)
    assert guarded.count(NARRATIVE_LOCATOR_MARKER) == 0  # 行内零残留
    assert "未在原文中定位到对应依据" in guarded  # 文末统一提示
    assert guarded.count("未在原文中定位到对应依据") == 1  # 至多一段
    assert validation["markers_stripped"] == 3


def test_golden_model_forged_references_header_removed_and_rewritten():
    text = "OsMYB73 含两个 SANT 结构域，位于 115-164 aa。\n\n【证据引用】（模型仿写）\n- 模型编造的引用行"
    guarded, _ = apply_citation_channel(text, CITATIONS)
    assert "模型仿写" not in guarded  # 仿写区块头被剥除
    assert "【证据引用】（后端渲染，页码来自证据锚点）" in guarded  # 后端重渲染


# ---------- 病灶 4：提示归一化 + 幂等（P2/P6） ----------


def test_golden_uncovered_snippets_normalized():
    text = "OsMYB73 定位于细胞核并具有激酶活性 9999 位点，参与 downstream signaling pathway 调控。"
    guarded, validation = apply_citation_channel(text, CITATIONS)
    assert "未在原文中定位到对应依据" in guarded
    snippet = validation["uncovered_claims"][0]
    assert "*" not in snippet and "|" not in snippet
    assert snippet.endswith("…") or len(snippet) <= 30


def test_idempotency_double_application_stable():
    """P2/P6 不变量：守卫与落库路径双重应用，结果必须一致（幂等）。"""
    corpus = [
        "OsMYB73 含两个 SANT 结构域，位于 115-164 与 167-215 氨基酸。\n\n## 文献与位置\n- 文献：Liu 等 (2024)",
        f"句子一 {NARRATIVE_LOCATOR_MARKER}。参见 [E99]。\n\n```python\n# code 115-164\n```",
        f"引用占位 {HISTORY_CITATION_PLACEHOLDER} 与表格：\n| a | 115-164 |\n| b | 167-215 |",
        "完全干净的软句子，无任何标记。",
    ]
    for text in corpus:
        once, _ = apply_citation_channel(text, CITATIONS)
        twice, _ = apply_citation_channel(once, CITATIONS)
        assert once == twice, f"non-idempotent for: {text[:40]}"


def test_structure_preservation_invariants():
    """P5 不变量：标题/表格/围栏行数与应用前一致；页码语义不变。"""
    text = (
        "# 标题一\n\nOsMYB73 的 SANT 结构域位于 115-164 aa。\n\n"
        "| 列A | 列B |\n| --- | --- |\n| 115-164 | 167-215 |\n\n"
        "```text\nblock 115-164\n```\n"
    )

    def structure_signature(value: str) -> tuple:
        lines = value.split("\n")
        return (
            sum(1 for line in lines if line.strip().startswith("#")),
            sum(1 for line in lines if line.strip().startswith("|")),
            sum(1 for line in lines if line.strip().startswith("```")),
        )

    guarded, _ = apply_citation_channel(text, CITATIONS)
    assert structure_signature(guarded) == structure_signature(text)
    # 页码语义：芯片仍指向第 3 页权威锚（格式修复不回退页码正确性）
    assert "〔证据E1｜正文·第3页｜paper.pdf〕" in guarded


def test_reverse_binding_returns_chip_before_trailing_whitespace():
    """P1 单元级：芯片插入在句号后、尾随空白（含行尾空白）之前。"""
    text = "OsMYB73 的 SANT 结构域位于 115-164 aa。  \n下一行"
    bound, count, _ = reverse_bind_citations(text, CITATIONS)
    assert count == 1
    chip_line = bound.split("\n")[0]
    assert chip_line.endswith("。 〔证据E1｜正文·第3页｜paper.pdf〕  ")
    assert bound.split("\n")[1] == "下一行"
