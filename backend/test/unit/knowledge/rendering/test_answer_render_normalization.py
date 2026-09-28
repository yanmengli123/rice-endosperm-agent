"""渲染质量终态归一回归（D1/D2/D3，2026-09-26 修复）。

夹具全部取自真实落库消息（``test/fixtures/render_normalization/``）：
- ``message_3860``（相邻同号 E1 芯片 ×2 处）→ 折叠归零；
- ``message_3863``（行尾双句号 ×5 处）→ 折叠归零；
- ``message_3830``（真实图芯片 + 【证据引用】附录的金标答案）→ **逐字节不变**
  （防过度归一的硬闸：合法芯片/附录/失败关闭标记不得被触碰）。

硬约束：① 幂等（守卫+落库双重应用）；② 折叠只作用正文（附录/失败关闭
标记不误伤）；③ 字节级金标门。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yuxi.knowledge.rendering.citation_channel import (
    _ADJACENT_DUPLICATE_CHIP_PATTERN,
    _canonicalize_authority_chip_shapes,
    _collapse_adjacent_duplicate_chips,
    _count_ungrounded_numeric_lines,
    _mark_unverified_bullet_items,
    _strip_display_placeholders,
    _strip_model_meta_descriptions,
)
from yuxi.knowledge.rendering.text_normalization import fold_trailing_duplicate_punctuation

pytestmark = [pytest.mark.unit]

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "render_normalization"


def _fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


# ---- D1：相邻同号芯片折叠 ----


def test_d1_real_fixture_3860_collapsed_to_zero():
    source = _fixture("message_3860.txt")
    result, collapsed = _collapse_adjacent_duplicate_chips(source)
    assert collapsed == 2  # 实测两处相邻重复（段内 + 列表项）
    assert not _ADJACENT_DUPLICATE_CHIP_PATTERN.search(result)
    # 正文内容不丢
    assert "T0和T1代植株在田间自然条件下种植观察表型" in result
    assert result.count("〔证据E1｜") == 3  # 原 5 枚 E1，折叠 2 处后剩 3


def test_d1_idempotent_double_application():
    once, _ = _collapse_adjacent_duplicate_chips(_fixture("message_3860.txt"))
    twice, n2 = _collapse_adjacent_duplicate_chips(once)
    assert n2 == 0 and once == twice


def test_d1_different_refs_and_legitimate_repeat_not_collapsed():
    assert _collapse_adjacent_duplicate_chips("〔证据E1｜a〕。 〔证据E2｜b〕")[1] == 0
    # 同 ref 但两枚之间有正文内容（跨句合法重复引用）：不折叠
    text = "句子一 〔证据E1｜a〕。这里是正文内容。句子二 〔证据E1｜b〕"
    result, collapsed = _collapse_adjacent_duplicate_chips(text)
    assert collapsed == 0 and result == text


def test_d1_appendix_and_failclosed_marker_untouched():
    appendix = "【证据引用】（后端渲染，页码来自证据锚点）\n- E1｜正文·第10页｜x｜ea_1"
    failclosed = "〔当前无法可靠定位原文页码〕"
    for text in (appendix, failclosed):
        result, collapsed = _collapse_adjacent_duplicate_chips(text)
        assert collapsed == 0 and result == text


def test_d1_chain_of_three_collapses_to_one():
    result, collapsed = _collapse_adjacent_duplicate_chips("〔证据E1｜a〕 〔证据E1｜b〕 〔证据E1｜c〕")
    assert result.count("〔证据E1") == 1 and collapsed == 2


# ---- D2：行尾全角句读折叠 ----


def test_d2_real_fixture_3863_folded_to_zero():
    source = _fixture("message_3863.txt")
    result, folded = fold_trailing_duplicate_punctuation(source)
    assert folded == 5  # 实测五处行尾双句号
    assert "。。" not in result


def test_d2_line_end_only_and_whitelist():
    assert fold_trailing_duplicate_punctuation("差异。。\n下")[0].startswith("差异。\n")
    assert fold_trailing_duplicate_punctuation("结尾。。")[0] == "结尾。"
    # 行中双句号（可能是引文原文）：不动
    assert fold_trailing_duplicate_punctuation("他说。。了\n")[0] == "他说。。了\n"
    # 白名单外（省略号/强调/问号）：不动
    for keep in ("省略……\n", "等待...\n", "好！！\n", "真的？？\n"):
        assert fold_trailing_duplicate_punctuation(keep)[0] == keep
    assert fold_trailing_duplicate_punctuation("列表，，\n")[0].startswith("列表，\n")


def test_d2_idempotent():
    once, _ = fold_trailing_duplicate_punctuation(_fixture("message_3863.txt"))
    twice, n2 = fold_trailing_duplicate_punctuation(once)
    assert n2 == 0 and once == twice


# ---- D3 第一层：未绑定数值行计数（块感知） ----


def test_d3_numeric_count_blocks_aware():
    body = (
        "osmyb73 垩白度 26.8%，野生型 12.1% 〔证据E1｜x〕\n"
        "另一行无芯片数字 95.6%\n"
        "```\ncode 12.5%\n```\n"
        "| 表 | 值 |\n| a | 3.14 |\n"
        "【证据引用】\n- E1｜正文·第10页"
    )
    assert _count_ungrounded_numeric_lines(body) == 1  # 仅第二行
    # 整数页码/编号不触发
    assert _count_ungrounded_numeric_lines("第10页，E1 共 3 处") == 0
    # 提示行自身不计数
    assert _count_ungrounded_numeric_lines("（注：3 处具体数值未绑定证据芯片或表格卡片）") == 0


# ---- D3 第二层：未验证数据型列表项标记（幂等 + 块感知） ----


def test_d3_mark_unverified_bullets():
    body = (
        "## 汇总\n\n"
        "- 图5（Figure 5）：表型 〔证据E1｜x〕\n"
        "- 图S22：cr-myb73 籽粒表型量化\n"
        "- 纯叙述条目不含图表编号\n"
        "【证据引用】\n- E1｜正文·第10页"
    )
    result, marked = _mark_unverified_bullet_items(body)
    assert marked == 1
    assert "- （未核验）图S22" in result
    assert "- 图5（Figure 5）：表型 〔证据E1｜x〕" in result  # 有芯片不标
    assert "- 纯叙述条目不含图表编号" in result
    assert "（未核验）E1｜" not in result  # 附录列表不标
    again, n2 = _mark_unverified_bullet_items(result)
    assert n2 == 0 and again == result


def test_d3_codefence_bullets_untouched():
    body = "```\n- 图S99：代码示例\n```\n- 图S22：真实条目"
    result, marked = _mark_unverified_bullet_items(body)
    assert marked == 1
    assert "- （未核验）图S99" not in result
    assert "- （未核验）图S22" in result


# ---- 硬约束③：3830 全文逐字节不变（金标门） ----


def test_gate_3830_byte_identical_under_all_transforms():
    source = _fixture("message_3830.txt")
    for transform in (
        _collapse_adjacent_duplicate_chips,
        fold_trailing_duplicate_punctuation,
        _mark_unverified_bullet_items,
    ):
        result, count = transform(source)
        assert result == source, transform.__name__
        assert count == 0, (transform.__name__, count)


def test_gate_3830_numeric_count_zero():
    # 3830 是有芯片有表格卡的金标答案：数值行全部有芯片绑定 → 计数 0
    assert _count_ungrounded_numeric_lines(_fixture("message_3830.txt")) == 0


# ---- 真实 3860 正文上数值披露与标记的协同（integration-lite） ----


def test_real_3860_numeric_and_marker_behavior():
    source = _fixture("message_3860.txt")
    # 3860 的数值行（"标尺1.0 cm"等）全部带 E1 芯片 → 披露计数 0（证据充分，
    # 不该被披露污染）
    assert _count_ungrounded_numeric_lines(source) == 0
    # 其列表项（图S21/图6）无芯片且含图表编号 → 标记（图5 条目有 E1 芯片不标）
    result, marked = _mark_unverified_bullet_items(source)
    assert marked == 2
    assert "- （未核验）图S21" in result
    assert "- （未核验）图6" in result
    assert "- 图5（Figure 5）：CRISPR/Cas9" in result


# ---- D1 补充：终态形态归一（非规范分隔符的权威芯片不得上屏） ----


def test_shape_canonicalize_halfwidth_separator():
    text = "见 〔证据E1 | 正文·第1页 | paper.pdf〕 与 〔图表F2 | Table 2〕"
    result, normalized = _canonicalize_authority_chip_shapes(text)
    assert normalized == 2
    assert result == "见 〔证据E1｜正文·第1页｜paper.pdf〕 与 〔图表F2｜Table 2〕"


def test_shape_canonicalize_is_idempotent_and_leaves_canonical_untouched():
    canonical = "〔证据E1｜正文·第10页｜rice.pdf〕〔引文定位｜正文·第3页｜rice.pdf〕"
    result, normalized = _canonicalize_authority_chip_shapes(canonical)
    assert normalized == 0 and result == canonical
    once, n1 = _canonicalize_authority_chip_shapes("〔证据E1 | a | b〕")
    twice, n2 = _canonicalize_authority_chip_shapes(once)
    assert n1 == 1 and n2 == 0 and once == twice


def test_shape_canonicalize_does_not_invent_chips():
    # 单段内容（无分隔符）不是芯片形态：保持原样，零计数
    text = "〔证据E1 无法定位〕"
    result, normalized = _canonicalize_authority_chip_shapes(text)
    assert normalized == 0 and result == text
    # 非权威标记（模型自造形态）不归一：避免替模型背书
    other = "〔注：这不是权威标记 | 带半角 | 三个段〕"
    result, normalized = _canonicalize_authority_chip_shapes(other)
    assert normalized == 0 and result == other


# ---- D3 第三层：元描述与自述页码清理 ----


def test_meta_description_line_and_self_reported_page_stripped():
    body = (
        "> 引用标记说明：以下 [E#] 由后端按本轮 Contract 渲染。\n"
        "结论见正文页码 3 的段落。\n"
        "另见 Supporting information 页码 17 的补充图。\n"
        "正常叙述保留：第10页提到粒长。\n"
        "〔证据E1｜正文·第10页｜rice.pdf〕"
    )
    result, removed = _strip_model_meta_descriptions(body)
    assert removed == 3
    assert "Contract" not in result
    assert "正文页码 3" not in result and "该页" in result
    assert "页码 17" not in result
    assert "正常叙述保留：第10页提到粒长。" in result
    assert "〔证据E1｜正文·第10页｜rice.pdf〕" in result  # 含芯片行不碰
    again, n2 = _strip_model_meta_descriptions(result)
    assert n2 == 0 and again == result


def test_internal_locator_placeholder_and_retrieval_meta_never_publish():
    body = (
        "支撑说明：本轮仅返回 9 条证据，Figure S17–S20 仅在图注出现。\n\n"
        "[后端渲染定位行]\n\n"
        "范围限定为后端返回的 E1–E9，其他内容未覆盖。"
    )
    stripped, _markers, placeholders = _strip_display_placeholders(body)
    result, meta_removed = _strip_model_meta_descriptions(stripped)
    assert placeholders == 1
    assert "后端渲染定位行" not in result
    assert "本轮仅返回" not in result
    assert "后端返回的 E1" not in result
    assert "Figure S17–S20" in result
    assert meta_removed == 2


# ---- D3 第二层补充：标题 / 有序列表 / 裸段落体例（Q3 逃逸回归） ----


def test_d3_marks_heading_and_ordered_items():
    body = (
        "### Figure S14 — 表达量分析\n"
        "正文段落。\n"
        "1. Figure S15 — 代谢热图\n"
        "2. Figure 5 — 表型 〔证据E1｜x〕\n"
        "3. 纯叙述条目\n"
    )
    result, marked = _mark_unverified_bullet_items(body)
    assert marked == 2
    assert "### （未核验）Figure S14" in result
    assert "1. （未核验）Figure S15" in result
    assert "2. Figure 5 — 表型 〔证据E1｜x〕" in result  # 有芯片不标
    assert "3. 纯叙述条目" in result
    again, n2 = _mark_unverified_bullet_items(result)
    assert n2 == 0 and again == result


def test_d3_marks_bare_paragraph_with_unbound_figure():
    # Q3 实测形态：错误断言在普通段落里，不在列表/标题里
    body = "Figure S21 给出大群体田间粒长统计结果，Figure S22 展示多年多点田间粒长统计结果。\n"
    result, marked = _mark_unverified_bullet_items(body)
    assert marked == 1
    assert result.startswith("（未核验）Figure S21")
    again, n2 = _mark_unverified_bullet_items(result)
    assert n2 == 0 and again == result


def test_d3_paragraph_not_marked_when_figure_bound_elsewhere():
    # 安全阀：同一图号全文别处已有权威芯片 → 跨句合法回指不标
    body = "见表型 〔图表F1｜Figure 5〕。\n另如 Figure 5 所示，粒长增加。\n"
    result, marked = _mark_unverified_bullet_items(body)
    assert marked == 0
    assert "另如 Figure 5 所示" in result


def test_d3_heading_and_list_not_marked_when_figure_bound_elsewhere():
    # 全文 F# 签名对所有 Markdown 体例生效，避免已核验图号在标题/列表中误报。
    body = (
        "表型证据 〔图表F1｜Figure 2〕。\n"
        "## 粒长变长的表型依据（Figure 2）\n"
        "- Figure 2 显示粒长增加\n"
        "### Figure 3 的补充比较\n"
    )
    result, marked = _mark_unverified_bullet_items(body)
    assert marked == 1
    assert "## 粒长变长的表型依据（Figure 2）" in result
    assert "- Figure 2 显示粒长增加" in result
    assert "### （未核验）Figure 3 的补充比较" in result


def test_d3_hedge_paragraph_not_double_marked():
    # 模型已自述「需进一步核验」→ 不叠加标记（冗余且误导）
    body = "补充说明：粒长的细胞学证据需查阅相关补充图（图S5等）进一步核验。\n"
    result, marked = _mark_unverified_bullet_items(body)
    assert marked == 0 and result == body


def test_d3_paragraph_untouched_without_figure_label():
    body = "这一段完全没有图表编号，是纯叙述，不应被标记。\n"
    result, marked = _mark_unverified_bullet_items(body)
    assert marked == 0 and result == body


# ---- 金标门扩展：3830 对新增两项变换同样逐字节不变 ----


def test_gate_3830_byte_identical_under_new_transforms():
    source = _fixture("message_3830.txt")
    for transform in (_canonicalize_authority_chip_shapes, _strip_model_meta_descriptions):
        result, count = transform(source)
        assert result == source, transform.__name__
        assert count == 0, (transform.__name__, count)
