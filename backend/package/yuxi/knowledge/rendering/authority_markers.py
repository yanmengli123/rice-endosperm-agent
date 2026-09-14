"""唯一权威标记解析器（Authority Markers，D3 收敛）。

用户可见的「权威定位/引用标记」只允许两种形态，形态定义在本模块**单点维护**：

- ``〔证据E#｜…〕``——Claim 绑定引用（EVIDENCE_CITATION）
- ``〔引文定位｜…〕``——确定性定位芯片（LOCATOR_CITATION）

此前 ``citation_channel`` 与 ``source_output_guard`` 各自持有一份 regex
定义 authority shape（代码异味：漏改一处即产生通道间口径漂移）。现在两者
都必须从本模块取用；合法性不由 regex 决定——regex 只负责「发现长得像权威
标记的文本」，是否合法由本 run 的后端签发产物（legitimate chips / locator
binding）决定。
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

AUTHORITY_MARKER_VERSION = "authority_marker_v1"


class AuthorityMarkerKind(StrEnum):
    EVIDENCE_CITATION = "EVIDENCE_CITATION"
    LOCATOR_CITATION = "LOCATOR_CITATION"


# 唯一形态来源：〔证据E#｜…〕 / 〔引文定位｜…〕
_AUTHORITY_MARKER_PATTERN = re.compile(r"〔(证据E\d{1,3}|引文定位)｜[^〕]+〕")
_EVIDENCE_REF_IN_MARKER = re.compile(r"E\d{1,3}")


def authority_marker_pattern() -> re.Pattern[str]:
    """后端签发芯片的规范形态（供保护/审计逻辑复用，勿在本模块外重写）。"""
    return _AUTHORITY_MARKER_PATTERN


def parse_authority_markers(text: str) -> list[dict[str, Any]]:
    """解析文本中全部规范形态的权威标记（含类别、位置、引用号）。"""
    markers: list[dict[str, Any]] = []
    for match in _AUTHORITY_MARKER_PATTERN.finditer(str(text or "")):
        head = match.group(1)
        kind = (
            AuthorityMarkerKind.LOCATOR_CITATION
            if head.startswith("引文定位")
            else AuthorityMarkerKind.EVIDENCE_CITATION
        )
        ref_match = _EVIDENCE_REF_IN_MARKER.search(head)
        markers.append(
            {
                "kind": kind,
                "start": match.start(),
                "end": match.end(),
                "text": match.group(0),
                "ref": ref_match.group(0) if ref_match else None,
            }
        )
    return markers


def count_authority_markers(text: str) -> dict[str, int]:
    """按类别计数（guard/审计用）。"""
    counts = {kind.value: 0 for kind in AuthorityMarkerKind}
    for marker in parse_authority_markers(text):
        counts[str(marker["kind"])] += 1
    return counts


__all__ = [
    "AUTHORITY_MARKER_VERSION",
    "AuthorityMarkerKind",
    "authority_marker_pattern",
    "count_authority_markers",
    "parse_authority_markers",
]
