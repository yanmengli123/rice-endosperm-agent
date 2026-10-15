# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic critical-token checks; does not certify scientific semantics."""

import re
import unicodedata
from collections import Counter

GENES = re.compile(r"(?<![A-Za-z0-9_])(?:LOC_Os\d{2}g\d+|Os(?=[A-Z]|[A-Za-z_-]*\d)[A-Za-z][A-Za-z0-9_-]*)(?![A-Za-z0-9_])")
NUMBERS = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d+(?:[.,]\d+)*(?:[eE][-+]?\d+)?")


def normalized(text):
    return unicodedata.normalize("NFKC", text).replace("−", "-").replace("–", "-")


def critical_issues(source, translated, block):
    source, translated = normalized(source), normalized(translated)
    issues = []
    for code, matcher, label in (
        ("gene_symbol_changed", GENES, "基因符号或位点编号"),
        ("numeric_value_changed", NUMBERS, "数字、符号或精度"),
    ):
        expected, actual = Counter(matcher.findall(source)), Counter(matcher.findall(translated))
        if expected != actual:
            issues.append({"page": 0, "block": block, "code": code, "severity": "error",
                           "message": f"文本块 {block} 的{label}与原文不一致，需要人工复核"})
    return issues
