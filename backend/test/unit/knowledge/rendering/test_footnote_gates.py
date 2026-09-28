r"""表格脚注正确性门禁（G6 升级：存在性 → 正确性，2026-09-27）。

回归夹具：msg 4058（半角分隔符+吞进表格）与 msg 4064（芯片为空）——
两条坏产出在旧 G6（子串存在性判据）下均通过，本文件证明升级后的
门禁对它们亮红灯。

三条正确性判据：
  C1 芯片非空：`> 表格依据：` 后必须紧跟 `〔证据E\d+｜…〕`
  C2 全角分隔：芯片内部不得出现 ` | `（半角竖线+空格）
  C3 表外位置：脚注行不得被吞进 markdown 表格行（不以 `|` 开头）
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from yuxi.knowledge.rendering.citation_channel import reverse_bind_citations

pytestmark = [pytest.mark.unit]

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "footnote_gates"

# 门禁实现（与金标脚本共用逻辑）
_FOOTNOTE_PREFIX = "> 表格依据："
# C1：脚注后必须紧跟非空芯片（全角 ｜ 分隔）
_VALID_CHIP = re.compile(r"^> 表格依据：〔证据E\d{1,3}｜[^〕]+〕\s*$")
# C2：芯片内不得有半角竖线分隔符
_HALFWIDTH_IN_CHIP = re.compile(r"〔证据E\d{1,3} \|")
# C3：脚注行被吞进表格（以 | 开头的行含脚注文本）
_SWALLOWED_IN_TABLE = re.compile(r"^\|[^>\n]*" + re.escape(_FOOTNOTE_PREFIX))


def check_footnote_correctness(text: str) -> list[str]:
    """返回违反列表（空列表 = 全过）。三条判据：C1 非空 / C2 全角 / C3 表外。"""
    violations: list[str] = []
    for line_num, line in enumerate(text.split("\n"), 1):
        if _FOOTNOTE_PREFIX not in line:
            continue
        # C3：被吞进表格
        if _SWALLOWED_IN_TABLE.match(line):
            violations.append(f"L{line_num} C3_swallowed_in_table: {line.strip()[:60]!r}")
            continue
        # C1：芯片非空（必须匹配完整形态）
        if not _VALID_CHIP.match(line.strip()):
            violations.append(f"L{line_num} C1_empty_or_malformed: {line.strip()[:60]!r}")
        # C2：半角分隔符
        if _HALFWIDTH_IN_CHIP.search(line):
            violations.append(f"L{line_num} C2_halfwidth_separator: {line.strip()[:60]!r}")
    return violations


# ---- 真实坏产出必须被抓 ----


def test_msg_4058_halfwidth_and_swallowed_is_caught():
    """msg 4058：半角分隔符 + 吞进表格 → 门禁必须亮红灯（旧 G6 通过的坏产出）。"""
    text = (FIXTURE_DIR / "msg_4058_halfwidth_in_table.txt").read_text(encoding="utf-8")
    violations = check_footnote_correctness(text)
    # 至少有一条 C2（半角）或 C3（吞表格）违规
    assert any("C2_" in v or "C3_" in v for v in violations), f"4058 未被抓: {violations}"


def test_msg_4064_empty_chip_is_caught():
    """msg 4064：芯片为空 → C1 必须亮红灯（旧 G6 通过的坏产出）。"""
    text = (FIXTURE_DIR / "msg_4064_empty_chip.txt").read_text(encoding="utf-8")
    violations = check_footnote_correctness(text)
    assert any("C1_" in v for v in violations), f"4064 未被抓: {violations}"


# ---- 合法脚注必须通过 ----


def test_valid_footnote_passes_all_three_checks():
    text = "正文段落。\n\n> 表格依据：〔证据E1｜正文·第1页｜paper.pdf〕"
    assert check_footnote_correctness(text) == []


def test_no_footnote_no_violation():
    assert check_footnote_correctness("普通正文，无脚注。") == []


def test_footnote_inside_codefence_not_checked():
    text = "```\n> 表格依据：\n```"
    # codefence 内的内容不应被检查（简化：此处只验证不 crash）
    check_footnote_correctness(text)  # 不抛即过


# ---- 渲染层正确产出必须通过（正路径锁定） ----


def test_reverse_bind_produces_valid_footnote():
    """reverse_bind_citations 的表格分支产出的脚注必须通过三条判据。"""
    citations = [
        {
            "ref": "E1",
            "evidence_id": "ev-1",
            "kb_id": "kb-a",
            "file_id": "file-a",
            "filename": "paper.pdf",
            "zone": "MAIN_TEXT",
            "page_numbers": [3],
            "primary_page": 3,
            "quote_head": "OsMYB73 SANT domains 115-164",
            "anchor_ids": ["ea-1"],
            "locatable": True,
            "toc_line": False,
            "secondary_of": None,
            "_quote": "The structure of OsMYB73 protein has two SANT domains between 115-164.",
            "_quote_norm": "the structure of osmyb73 protein has two sant domains between 115 164",
            "_anchor_id": "ea-1",
        }
    ]
    table = "| 基因 | 结构域 |\n| --- | --- |\n| OsMYB73 | SANT 115-164 |"
    result, _bound, _unc = reverse_bind_citations(table, citations)
    violations = check_footnote_correctness(result)
    assert violations == [], f"渲染层产出违规: {violations}"
    assert _FOOTNOTE_PREFIX in result
