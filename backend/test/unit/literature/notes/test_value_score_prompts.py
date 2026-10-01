"""价值分与提示词契约测试（P2，ADR-0010 决议 5b）。

价值分阈值是**上游自纠后的口径**（L2=2.5 / L3=4.0；上游源码注释曾误写
L2≥4.0 后自纠）——本文件钉死防"修正"回去。提示词常量逐字移植自
paperkb/compile.py，证据硬规则/六维清单/段落 ID 引用是算法保真本体。
"""

from __future__ import annotations

import pytest

from yuxi.literature.notes import prompts, value_score

pytestmark = pytest.mark.unit


# --- 价值分（阈值防漂移） -------------------------------------------------------


def test_value_score_thresholds_match_upstream_corrected_values():
    assert value_score.L2_THRESHOLD == 2.5, "L2 阈值必须是 2.5（上游 2026-09 自纠值）"
    assert value_score.L3_THRESHOLD == 4.0
    assert value_score.WEIGHTS == {
        "if": 0.20,
        "cited": 0.15,
        "ai_value": 0.25,
        "topic": 0.15,
        "year": 0.10,
        "paper_rank": 0.10,
        "lib_cited": 0.05,
    }


def test_compute_value_score_renormalizes_missing_fields():
    # 只有 ai_value=5 可用 → 归一化后即 5（不惩罚缺失字段）
    score = value_score.compute_value_score({"ai_value": 5})
    assert score == 5.0
    # ai_value=4 + 年份 1.0 → (0.25*4 + 0.10*1) / 0.35 ≈ 3.143
    mixed = value_score.compute_value_score({"ai_value": 4, "year": "2025", "current_year": 2026})
    assert abs(mixed - (0.25 * 4 + 0.10 * 1.0) / 0.35) < 1e-3
    # 全缺失 → 0
    assert value_score.compute_value_score({}) == 0.0


def test_auto_upgrade_levels_use_corrected_thresholds():
    # L2=2.5：2.4 不升级，2.5 升级；L3=4.0：需 ai_value 可用
    assert value_score.auto_upgrade_levels(2.4, ai_value_available=True) == (False, False)
    assert value_score.auto_upgrade_levels(2.5, ai_value_available=True) == (True, False)
    assert value_score.auto_upgrade_levels(4.0, ai_value_available=True) == (True, True)
    # ai_value 不可用（None 传入 compute 后 score 由其他字段撑起）→ L3 不升级
    assert value_score.auto_upgrade_levels(4.5, ai_value_available=False) == (True, False)


# --- 提示词契约 -----------------------------------------------------------------


def test_prompts_carry_evidence_rules_and_paragraph_ids():
    paragraphs = ["第一段：背景介绍。", "第二段：方法细节。", "第三段：结果数据。"]
    prompt = prompts.prompt_l1_l2_merged({"title": "T", "doi": "10.1/x"}, paragraphs)
    # 共享前缀：段落编号块
    assert "[P001] 第一段：背景介绍。" in prompt
    assert "[P003] 第三段：结果数据。" in prompt
    # 证据硬规则（逐字移植本体）
    assert "只写原文存在的内容" in prompt
    assert "不得引入原文之外的事实" in prompt
    assert "原文未报告" in prompt
    # 六维 + wiki 检查清单
    assert "方法论批判" in prompt and "可复现性分析" in prompt and "潜在应用与转化路径" in prompt
    assert "one_liner" in prompt and "ai_value" in prompt and "topic_score" in prompt
    # LaTeX 禁令
    assert "LaTeX" in prompt


def test_task_mark_split_yields_prefix_stable_messages():
    paragraphs = ["段落A。", "段落B。"]
    prompt = prompts.prompt_l1_l2_merged({"title": "T"}, paragraphs)
    system, user = prompts.split_task(prompt)
    # system = 共享全文前缀（逐字节稳定 ⇒ 前缀缓存命中）；user = 任务指令
    assert system == prompts.build_shared_prefix(paragraphs)
    assert user.startswith("你是科研知识编译助手")
    assert prompts.TASK_MARK not in system and prompts.TASK_MARK not in user


def test_parse_compile_json_salvages_fenced_and_noisy_output():
    good = '{"one_liner": "x", "ai_value": 3}'
    assert prompts.parse_compile_json(good) == {"one_liner": "x", "ai_value": 3}
    fenced = f"```json\n{good}\n```"
    assert prompts.parse_compile_json(fenced)["ai_value"] == 3
    noisy = f"模型前言\n{good}\n模型后记"
    assert prompts.parse_compile_json(noisy)["one_liner"] == "x"
    with pytest.raises(ValueError, match="JSON"):
        prompts.parse_compile_json("完全不是 JSON")


def test_l3_prompt_requires_doi_citation_format():
    related = [{"doi": "10.1016/j.snb.2022.132616", "context": "ctx", "connection": "方法继承"}]
    prompt = prompts.prompt_l3({"title": "T", "doi": "10.1/self"}, "self ctx", related)
    assert "文献N（DOI）" in prompt
    assert "1=10.1016/j.snb.2022.132616" in prompt
    assert "concept_map" in prompt and "methodology_connections" in prompt
