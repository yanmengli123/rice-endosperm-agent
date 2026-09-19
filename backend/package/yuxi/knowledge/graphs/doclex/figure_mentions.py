"""图注 mention 解析（R6）：正文「（图 1）/ Fig. 5A / 见 Table S2」→ 规范图表键。

复用 ``caption_locator`` 的编号正则族与 ``canonical_figure_label`` 归一——
mention 侧与题注侧共享同一套规范键（figure 5 / figure s8 / table 2），
绑定即等值 join，不发明第二套编号体系。纯函数、零模型。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from yuxi.knowledge.evidence.caption_locator import _LABEL_PATTERN, canonical_figure_label

FIGURE_MENTION_VERSION = "figure_mentions_v1"
# 单 chunk 的 mention 上限（防题注目录页刷屏）
MAX_MENTIONS_PER_CHUNK = 64


@dataclass(frozen=True)
class FigureMention:
    """一次正文图表编号提及。canonical_key 与 figure_entities.entity_key 同域。"""

    surface: str
    canonical_key: str
    start: int
    end: int


def parse_figure_mentions(text: str, *, limit: int = MAX_MENTIONS_PER_CHUNK) -> list[FigureMention]:
    """解析文本中的图表编号提及，按出现位置排序、同键去重（首现保留）。"""
    mentions: list[FigureMention] = []
    seen: set[str] = set()
    for match in _LABEL_PATTERN.finditer(text or ""):
        surface = match.group(0).strip()
        canonical_key = canonical_figure_label(surface)
        if not canonical_key or canonical_key in seen:
            continue
        seen.add(canonical_key)
        mentions.append(
            FigureMention(
                surface=surface[:128],
                canonical_key=canonical_key,
                start=match.start(),
                end=match.end(),
            )
        )
        if len(mentions) >= limit:
            break
    return mentions


def mention_sentence(text: str, mention: FigureMention, *, window: int = 120) -> str:
    """mention 所在的上下文片段（供审核与证据面板预览）。"""
    start = max(0, mention.start - window)
    end = min(len(text or ""), mention.end + window)
    return (text or "")[start:end]


# 图表编号独占整行（题注行）时不作正文 mention：那是 caption 自身，已由题注通道索引
_CAPTION_LINE = re.compile(r"^\s*(?:Figure|Fig\.?|Table|图|表)\s*[0-9sS]", re.IGNORECASE)


def parse_body_figure_mentions(text: str) -> list[FigureMention]:
    """按行过滤：跳过题注行/表格行，只保留正文 mention。"""
    kept: list[FigureMention] = []
    offset = 0
    for line in (text or "").splitlines(keepends=True):
        if not _CAPTION_LINE.match(line):
            for mention in parse_figure_mentions(line):
                kept.append(
                    FigureMention(
                        surface=mention.surface,
                        canonical_key=mention.canonical_key,
                        start=offset + mention.start,
                        end=offset + mention.end,
                    )
                )
        offset += len(line)
    return kept
