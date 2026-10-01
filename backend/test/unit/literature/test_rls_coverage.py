"""RLS 覆盖守卫（独立核查发现的缺口回归）：每个 literature 租户表必须有 RLS 策略。

背景：0070 建了 ``literature_reader_renders`` 但漏建策略（0071-0073 的表都有），
8 张表 7 有 1 无。当前 Yuxi 以表所有者连接（策略不生效，见 ADR-0001 §8），策略是
为切换非所有者角色预留的防御纵深——缺一张 = 将来唯一裸奔的表。

守卫静态扫描 ``manager.py``（必须覆盖两种写法）：
1. 字面量 ``ALTER TABLE <表> ENABLE ROW LEVEL SECURITY``（0074 补丁形态）；
2. 循环形态 ``for table in (<表名元组>):`` 且循环体内有 ENABLE 语句（0071-0073
   以 f-string 循环建策略——字面量中不存在表名，必须解析元组）。
凡 ``CREATE TABLE IF NOT EXISTS literature_*`` 且含 ``tenant_id`` 的表，两者必居其一。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_EXPECTED_POLICY_TABLES = {
    "literature_reader_renders",
    "literature_papers",
    "literature_compile_jobs",
    "literature_citations",
    "literature_variants",
    "literature_translation_jobs",
    "literature_journal_metrics",
    "literature_attachments",
}


def _manager_source() -> str:
    import yuxi.storage.postgres.manager as manager_module

    return Path(manager_module.__file__).read_text(encoding="utf-8-sig")


def _rls_covered_tables(source: str) -> set[str]:
    """返回源码中受 RLS 保护的表名集合（字面量 ALTER + f-string 循环两种形态）。"""
    covered: set[str] = set()
    # 形态 1：字面量 ALTER TABLE <表> ENABLE ROW LEVEL SECURITY
    covered.update(re.findall(r"ALTER TABLE (literature_\w+) ENABLE ROW LEVEL SECURITY", source))
    # 形态 2：for table in (<元组>): … ENABLE ROW LEVEL SECURITY（f-string 循环）
    for match in re.finditer(r"for table in \(([^)]*)\)\s*:", source):
        loop_tail = source[match.end() : match.end() + 600]
        if "ENABLE ROW LEVEL SECURITY" not in loop_tail:
            continue
        covered.update(re.findall(r'"(literature_\w+)"', match.group(1)))
    return covered


def test_every_literature_tenant_table_has_rls():
    source = _manager_source()
    created_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (literature_\w+)\s*\(", source))
    assert created_tables, "守卫有效性：至少应发现 literature_* 建表语句"
    covered = _rls_covered_tables(source)
    missing = created_tables - covered
    assert not missing, f"literature 租户表缺 RLS（防御纵深缺口）: {sorted(missing)}"


def test_all_eight_literature_tables_are_policy_covered():
    """锁面：当前 8 张 literature 租户表全部在册——新增表必须同步进 RLS 循环/补丁。"""
    source = _manager_source()
    covered = _rls_covered_tables(source)
    assert _EXPECTED_POLICY_TABLES <= covered, f"以下表缺 RLS 策略: {sorted(_EXPECTED_POLICY_TABLES - covered)}"


def test_policy_naming_follows_convention():
    """策略命名统一 p_<table>_tenant（ADR-0001 §4 惯例），防止命名漂移。"""
    source = _manager_source()
    expected = {f"p_{table}_tenant" for table in _EXPECTED_POLICY_TABLES}
    # 字面量 CREATE POLICY（0074 形态）+ f-string 循环派生（0071-0073 形态）
    literal = set(re.findall(r"CREATE POLICY (p_literature_\w+_tenant) ON", source))
    derived = {f"p_{table}_tenant" for table in _rls_covered_tables(source)}
    assert expected <= literal | derived
