"""翻译域契约测试（P3）：latextap 逐字节还原、MATH 占位贯穿、lang 进身份、
翻译零证据面写入（D2 决策的结构保证）。
"""

from __future__ import annotations

import pytest

from yuxi.literature.translate import prompts
from yuxi.literature.translate.latextap import reassemble, split_text

pytestmark = pytest.mark.unit


def test_latextap_roundtrip_is_byte_exact():
    cases = [
        "The pH was maintained at 7.4 with $\\mathrm{NaHCO_3}$ buffer.",
        "Grain yield $$Y = a + bx$$ increased across seasons.",
        "Expression of <sup>13</sup>CO2 fixation genes changed.",
        "No math here at all.",
    ]
    for text in cases:
        labeled, math_list = split_text(text)
        assert reassemble(labeled, math_list) == text, f"逐字节还原失败: {text}"


def test_latextap_placeholder_survives_translation_payload():
    text = "Starch content ($\\alpha$-glucan) rose; see $$C=kT$$ for the model."
    labeled, math_list = split_text(text)
    # 模拟"翻译"：中文化非公式部分、原样保留标签
    translated = (
        labeled.replace("Starch content", "淀粉含量")
        .replace("rose; see", "上升；见")
        .replace("for the model", "为模型")
    )
    restored = reassemble(translated, math_list)
    assert "\\alpha" in restored and "C=kT" in restored
    assert "[[MATH" not in restored


def test_shared_prefix_remaps_math_tags_globally():
    paras = ["第一段 $x^2$ 公式。", "第二段 $y_1$ 与 $z^3$。"]
    prefix, math_list = prompts.build_shared_prefix_with_math(paras)
    # 全局编号连续（段内局部编号被重映射）
    assert "[[MATH0]]" in prefix and "[[MATH1]]" in prefix and "[[MATH2]]" in prefix
    assert len(math_list) == 3
    assert "[P001] 第一段 [[MATH0]] 公式。" in prefix
    assert "[P002] 第二段 [[MATH1]] 与 [[MATH2]]。" in prefix
    # 逐字节还原每个公式
    assert math_list[0] == "$x^2$" and math_list[1] == "$y_1$" and math_list[2] == "$z^3$"


def test_translate_task_requires_json_and_tag_discipline():
    task = prompts.translate_task(["P001", "P002"])
    assert "[[MATHn]]" in task
    assert "绝不展开、绝不改写" in task
    assert '{"translations": [{"para_id": "P001", "zh": "..."}]}' in task


def test_translate_package_never_imports_knowledge_domain():
    """D2 结构保证：翻译链路零 yuxi.knowledge 依赖（英文证据定位不可能被触碰）。"""
    import inspect

    from yuxi.literature.boundaries import find_forbidden_imports
    from yuxi.literature.translate import latextap, prompts as tp, service as ts

    for module in (latextap, tp, ts):
        assert find_forbidden_imports(inspect.getsource(module), ("yuxi.knowledge",)) == [], module.__name__
