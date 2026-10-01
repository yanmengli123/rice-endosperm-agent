"""L1/L2/L3 编译价值评分（语义移植自 PaperAgent ``paperkb/score.py``，v1.4.0）。

value_score = 0.20×IF档 + 0.15×被引 + 0.25×AI价值 + 0.15×主题 + 0.10×年份
            + 0.10×PaperRank + 0.05×库内被引
- 归一化到 0-5；缺失字段权重等比放大（不惩罚无 IF/无被引文献）
- **升级阈值：L2 ≥ 2.5；L3 ≥ 4.0 且 ai_value 可用；L1 全做**
  （上游源码注释曾误写 L2≥4.0 后自纠为 2.5——test_value_score.py 钉死防回退）
- AI 价值评分由 L1 编译时产出（零额外成本）；paper_rank/库内被引 P4 从计量同步
"""

from __future__ import annotations

import math

WEIGHTS = {
    "if": 0.20,
    "cited": 0.15,
    "ai_value": 0.25,
    "topic": 0.15,
    "year": 0.10,
    "paper_rank": 0.10,
    "lib_cited": 0.05,
}
L2_THRESHOLD = 2.5
L3_THRESHOLD = 4.0

_QUARTILE_MAP = {"Q1": 4, "Q2": 3, "Q3": 2, "Q4": 1}


def if_rank(journal_info: dict | None) -> tuple[float, bool]:
    """IF 档分 0-5。Q1=4..Q4=1；无 Quartile 用 JIF 分档；中科院 1 区 +1。"""
    if not journal_info:
        return 0.0, False
    jcr = journal_info.get("jcr") or {}
    cas = journal_info.get("cas") or {}
    q = (jcr.get("quartile") or "").upper()
    base = _QUARTILE_MAP.get(q)
    if base is None:
        try:
            jif = float(jcr.get("jif") or 0)
        except (TypeError, ValueError):
            jif = 0.0
        base = 4 if jif >= 10 else 3 if jif >= 5 else 2 if jif >= 2 else 1 if jif > 0 else 0
    if base == 0:
        return 0.0, False
    bonus = 1 if str(cas.get("zone") or "") == "1" else 0
    return float(min(5, base + bonus)), True


def cited_norm(times_cited: int | None, has_bib: bool) -> tuple[float, bool]:
    """log 缩放：316 次被引 → 1.0；无 bib（数据缺失）→ 不可用。"""
    if not has_bib:
        return 0.0, False
    return min(1.0, math.log10(max(1, int(times_cited or 0)) + 1) / 2.5), True


def ai_value(score: float | None) -> tuple[float, bool]:
    """AI 价值评分（0-5）：由 L1/L1+L2 编译时产出。未评分（None）→ 不可用。"""
    if score is None:
        return 0.0, False
    return float(min(5.0, max(0.0, score))), True


def topic_score(score: float | None) -> tuple[float, bool]:
    """主题匹配评分（0-1）：AI 基于用户主题表打分。未评分（None）→ 不可用。"""
    if score is None:
        return 0.0, False
    return float(min(1.0, max(0.0, score))), True


def year_weight(year: str | None, current_year: int) -> tuple[float, bool]:
    """近 3 年 = 1.0；3-10 年 = 0.7；>10 年 = 0.4；无年份 = 不可用。"""
    try:
        y = int(str(year or "")[:4])
    except (TypeError, ValueError):
        return 0.0, False
    diff = max(0, current_year - y)
    if diff <= 3:
        return 1.0, True
    if diff <= 10:
        return 0.7, True
    return 0.4, True


def paper_rank_norm(rank: float | None) -> tuple[float, bool]:
    """PaperRank 归一化 0-5。经验分布：>0.01 高 / 0.001-0.01 中 / <0.001 低。"""
    if rank is None or rank <= 0:
        return 0.0, False
    if rank > 0.01:
        return 5.0, True
    if rank > 0.001:
        return 3.0, True
    return 1.0, True


def lib_cited_norm(count: int | None) -> tuple[float, bool]:
    """库内被引（0-5 线性封顶）。未统计（None）→ 不可用。"""
    if count is None:
        return 0.0, False
    return float(min(5.0, int(count))), True


def compute_value_score(inputs: dict) -> float:
    """按权重合成 0-5 价值分；缺失字段权重等比放大（上游口径）。

    inputs 键：journal_info / times_cited / has_bib / ai_value / topic_score /
    year / paper_rank / library_citations；current_year 可选（默认取运行年）。
    """
    from datetime import datetime

    current_year = int(inputs.get("current_year") or datetime.now().year)
    components = {
        "if": if_rank(inputs.get("journal_info")),
        "cited": cited_norm(inputs.get("times_cited"), bool(inputs.get("has_bib"))),
        "ai_value": ai_value(inputs.get("ai_value")),
        "topic": topic_score(inputs.get("topic_score")),
        "year": year_weight(inputs.get("year"), current_year),
        "paper_rank": paper_rank_norm(inputs.get("paper_rank")),
        "lib_cited": lib_cited_norm(inputs.get("library_citations")),
    }
    total_weight = 0.0
    acc = 0.0
    for key, (value, usable) in components.items():
        weight = WEIGHTS[key]
        if usable:
            acc += weight * value
            total_weight += weight
    if total_weight <= 0:
        return 0.0
    return round(acc / total_weight, 3)


def auto_upgrade_levels(value_score: float, *, ai_value_available: bool) -> tuple[bool, bool]:
    """由价值分决定 L2/L3 自动升级（上游自纠后口径）。返回 (want_l2, want_l3)。"""
    return value_score >= L2_THRESHOLD, ai_value_available and value_score >= L3_THRESHOLD
