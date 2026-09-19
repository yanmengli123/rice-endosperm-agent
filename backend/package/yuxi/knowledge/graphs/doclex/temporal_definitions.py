"""发育期定义断言（B3）：「灌浆中期 = 抽穗后 10-25 dDAH」的结构化区间解析。

定义不是别名：sameAs 的传递性会让冲突定义互相污染（文献 A 10-25、文献 B
12-28 若都 sameAs 到「灌浆中期」，等价闭包即矛盾）。定义以带逐字引文的区间
断言落库（knowledge_doclex_definitions），跨文献区间不一致由冲突检测登记为
DEFINITION 冲突，回答可并陈两个口径。

纯规则、零模型；阶段词表与 lexicon v2 的灌浆分期词条同源。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TEMPORAL_DEFINITION_VERSION = "temporal_definitions_v1"

# 阶段词（中文优先，兼容英文；与 lexicon._STAGES 分期词条同族）
_STAGE_TERMS: tuple[str, ...] = (
    "灌浆前期",
    "灌浆初期",
    "早期灌浆",
    "early grain filling stage",
    "灌浆中期",
    "中期灌浆",
    "middle grain filling stage",
    "mid grain filling",
    "灌浆后期",
    "晚期灌浆",
    "late grain filling stage",
)
_STAGE_ALTERNATION = "|".join(re.escape(term) for term in _STAGE_TERMS)

# 区间模式：数值[-–~至数值] + 单位（缩写或英文全称或中文「抽穗后N-M天」）
_INTERVAL_PATTERNS: tuple[tuple[re.Pattern, str, str], ...] = (
    (
        re.compile(
            r"(?<![0-9])(\d{1,3})\s*[-–~～至]\s*(\d{1,3})\s*(?:d\s*)?(DAH|dDAH)\b",
            re.IGNORECASE,
        ),
        "DAH",
        "heading",
    ),
    (
        re.compile(
            r"(?<![0-9])(\d{1,3})\s*[-–~～至]\s*(\d{1,3})\s*days?\s+after\s+heading",
            re.IGNORECASE,
        ),
        "DAH",
        "heading",
    ),
    (
        re.compile(
            r"(?<![0-9])(\d{1,3})\s*[-–~～至]\s*(\d{1,3})\s*(?:d\s*)?(DAF|dDAF)\b",
            re.IGNORECASE,
        ),
        "DAF",
        "flowering",
    ),
    (
        re.compile(
            r"(?<![0-9])(\d{1,3})\s*[-–~～至]\s*(\d{1,3})\s*days?\s+after\s+(?:flowering|anthesis)",
            re.IGNORECASE,
        ),
        "DAF",
        "flowering",
    ),
    (
        re.compile(
            r"(?<![0-9])(\d{1,3})\s*[-–~～至]\s*(\d{1,3})\s*(?:d\s*)?(DAP|dDAP)\b",
            re.IGNORECASE,
        ),
        "DAP",
        "pollination",
    ),
    (
        re.compile(
            r"(?<![0-9])(\d{1,3})\s*[-–~～至]\s*(\d{1,3})\s*days?\s+after\s+(?:pollination|fertilization)",
            re.IGNORECASE,
        ),
        "DAP",
        "pollination",
    ),
    (re.compile(r"抽穗后\s*第?(\d{1,3})\s*[-–~～至]\s*第?(\d{1,3})\s*[天日]"), "DAH", "heading"),
    (re.compile(r"开花后\s*第?(\d{1,3})\s*[-–~～至]\s*第?(\d{1,3})\s*[天日]"), "DAF", "flowering"),
    (re.compile(r"授粉后\s*第?(\d{1,3})\s*[-–~～至]\s*第?(\d{1,3})\s*[天日]"), "DAP", "pollination"),
)

# 阶段词与区间的邻近窗口：同一句内（跨标点/换行不算）
_SENTENCE_BOUNDARY = re.compile(r"[。.；;\n]")
_STAGE_PROXIMITY = 80


@dataclass(frozen=True)
class TemporalDefinition:
    """一条定义断言：实体（发育期）在 ref_event 后 [start, end] unit 天。"""

    entity_name: str
    entity_label: str
    interval_start: int
    interval_end: int
    interval_unit: str
    ref_event: str
    quote: str
    match_start: int
    match_end: int


def parse_temporal_definitions(text: str) -> list[TemporalDefinition]:
    """提取「阶段词 ± 邻近时间区间」的定义断言；无阶段词邻近的区间不算定义。"""
    stage_matches = [match for match in re.finditer(_STAGE_ALTERNATION, text)]
    definitions: list[TemporalDefinition] = []
    seen_spans: set[tuple[int, int]] = set()
    for pattern, unit, ref_event in _INTERVAL_PATTERNS:
        for match in pattern.finditer(text):
            start, end = int(match.group(1)), int(match.group(2))
            if end < start:
                start, end = end, start
            stage = _nearest_stage(text, stage_matches, match)
            if stage is None:
                continue
            key = (stage.group(0), start, end, unit)
            if key in seen_spans:
                continue
            seen_spans.add(key)
            definitions.append(
                TemporalDefinition(
                    entity_name=stage.group(0),
                    entity_label="DevelopmentStage",
                    interval_start=start,
                    interval_end=end,
                    interval_unit=unit,
                    ref_event=ref_event,
                    quote=text[max(0, match.start() - 60) : match.end() + 60].strip(),
                    match_start=match.start(),
                    match_end=match.end(),
                )
            )
    definitions.sort(key=lambda item: item.match_start)
    return definitions


def _nearest_stage(text: str, stage_matches: list[re.Match], interval: re.Match) -> re.Match | None:
    """区间附近最近的阶段词；必须未被句边界隔断（同句才算定义关系）。"""
    best: re.Match | None = None
    best_distance = _STAGE_PROXIMITY
    for stage in stage_matches:
        distance = min(
            abs(stage.start() - interval.start()),
            abs(stage.end() - interval.end()),
            abs(stage.start() - interval.end()),
            abs(stage.end() - interval.start()),
        )
        if distance >= best_distance:
            continue
        between = text[min(stage.end(), interval.end()) : max(stage.start(), interval.start())]
        if _SENTENCE_BOUNDARY.search(between):
            continue
        best, best_distance = stage, distance
    return best
