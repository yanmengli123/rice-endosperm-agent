"""渲染层共享的确定性文本归一（D2 修复，2026-09-26）。

只做**行尾**全角句读重复折叠：模型块文本自带的 ``。。``/``，，`` 属于生成
伪影；行中重复与引文片段**不动**（中文文献引文里可能合法存在）。

白名单保守：仅 ``。`` ``．`` ``，`` 三字符的连续重复参与折叠——不碰
``……``（U+2026 省略号，不同字符）、拉丁 ``...``（省略号）、``！！``（强调）。
幂等：折叠产物不再匹配，守卫+落库双重应用安全。
"""

from __future__ import annotations

import re

_TRAILING_DUPLICATE_PUNCTUATION = re.compile(r"([。．，])\1+([ \t]*)$", re.MULTILINE)


def fold_trailing_duplicate_punctuation(text: str) -> tuple[str, int]:
    """行尾连续重复的全角句读折叠为一。返回 ``(归一文本, 折叠次数)``。"""
    folded = 0

    def _fold(match: re.Match) -> str:
        nonlocal folded
        folded += 1
        return match.group(1) + match.group(2)

    result = _TRAILING_DUPLICATE_PUNCTUATION.sub(_fold, str(text or ""))
    return result, folded


__all__ = ["fold_trailing_duplicate_punctuation"]
