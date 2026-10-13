"""RLS 覆盖守卫（独立核查发现的缺口回归）：每个 literature 租户表必须有 RLS 策略。

背景：0070 建了 ``literature_reader_renders`` 但漏建策略（0071-0073 的表都有），
8 张表 7 有 1 无。当前 Yuxi 以表所有者连接（策略不生效，见 ADR-0001 §8），策略是
为切换非所有者角色预留的防御纵深——缺一张 = 将来唯一裸奔的表。

守卫静态扫描 ``manager.py``（必须覆盖三种写法）：
1. 字面量 ``ALTER TABLE <表> ENABLE ROW LEVEL SECURITY``（0074 补丁形态）；
2. 字符串元组循环 ``for table in ("<表名>", ...)`` 且循环体内有 ENABLE 语句（0071-0073
   以 f-string 循环建策略——字面量中不存在表名，必须解析元组）；
3. ORM 类元组循环 ``for model in (ModelA, ModelB):`` 且循环体内有 ENABLE 语句（0112
   形态——表由 ``model.__table__.create(checkfirst=True)`` 创建，表名只存在于
   models_knowledge 的 ``__tablename__``，必须解析 ORM 类才能发现建了哪些表）。
凡租户可见的 ``literature_*`` 表（字面量 DDL 建表 + ORM 循环建表），三者必居其一。
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
    # 0111 字面量建表
    "literature_reference_batches",
    "literature_reference_members",
    # 0112 ORM 循环建表（静态扫描曾漏掉的三张）
    "literature_reference_records",
    "literature_topic_runs",
    "literature_topic_documents",
}


def _manager_source() -> str:
    import yuxi.storage.postgres.manager as manager_module

    return Path(manager_module.__file__).read_text(encoding="utf-8-sig")


def _iter_loops(source: str):
    """产出 (元组文本, 循环体前 600 字符)。头以首个 ``:`` 换行终止，容忍嵌套元组。"""
    for match in re.finditer(r"for [\w, ]+? in \(", source):
        head = source[match.start() : match.start() + 2000]
        colon = re.search(r":\s*\n", head)
        if colon is None:
            continue
        targets = head[match.end() - match.start() : colon.start()]
        yield targets, head[colon.end() : colon.end() + 600]


def _loop_tables(tuple_source: str) -> set[str]:
    """循环元组里的表名：字符串字面量直接取，ORM 类名解析 __tablename__。"""
    tables = set(re.findall(r'"(literature_\w+)"', tuple_source))
    for name in re.findall(r"[A-Za-z_]\w*", tuple_source):
        if name in tables or name[0].islower():
            continue
        from yuxi.storage.postgres import models_knowledge

        model = getattr(models_knowledge, name, None)
        table = getattr(model, "__table__", None)
        if table is not None and table.name.startswith("literature_"):
            tables.add(table.name)
    return tables


def _rls_covered_tables(source: str) -> set[str]:
    """返回源码中受 RLS 保护的表名集合（字面量 ALTER + 两种循环形态）。"""
    covered: set[str] = set(re.findall(r"ALTER TABLE (literature_\w+) ENABLE ROW LEVEL SECURITY", source))
    for targets, body in _iter_loops(source):
        if "ENABLE ROW LEVEL SECURITY" in body:
            covered.update(_loop_tables(targets))
    return covered


def _policy_covered_tables(source: str) -> set[str]:
    """有策略的表：字面量 CREATE POLICY … ON <表> + 循环体内含 CREATE POLICY 的元组表。

    f-string 循环（0071-0073 的 p_*_tenant、0084、0112 的 *_owner）按循环头关联——
    循环体为每个元组成员各建一条策略，比定长字符窗口稳健。
    """
    covered: set[str] = set(re.findall(r"CREATE POLICY \w+ ON (literature_\w+)", source))
    for targets, body in _iter_loops(source):
        if "CREATE POLICY" in body:
            covered.update(_loop_tables(targets))
    return covered


def _orm_created_literature_tables(source: str) -> set[str]:
    """ORM 循环建表形态（0112）：for model in (...): … table.create(checkfirst=True)。"""
    created: set[str] = set()
    for targets, body in _iter_loops(source):
        if "table.create(" in body and "checkfirst" in body:
            created.update(_loop_tables(targets))
    return created


def test_every_literature_tenant_table_has_rls():
    source = _manager_source()
    created_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (literature_\w+)\s*\(", source))
    created_tables |= _orm_created_literature_tables(source)
    assert created_tables, "守卫有效性：至少应发现 literature_* 建表语句"
    covered = _rls_covered_tables(source)
    missing = created_tables - covered
    assert not missing, f"literature 租户表缺 RLS（防御纵深缺口）: {sorted(missing)}"


def test_all_literature_policy_tables_are_locked():
    """锁面：当前 13 张 literature 租户表全部在册——新增表必须同步进 RLS 循环/补丁。"""
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


def test_every_covered_table_has_a_policy():
    """有 RLS 还必须有策略：每张 ENABLE 的表至少被一条 CREATE POLICY 覆盖
    （字面量或 f-string 循环派生），防止只 ENABLE 不建策略导致非所有者角色
    全表拒绝访问而非按租户过滤。"""
    source = _manager_source()
    covered = _rls_covered_tables(source)
    assert covered, "守卫有效性：至少应发现受 RLS 保护的表"
    missing = covered - _policy_covered_tables(source)
    assert not missing, f"以下表启用了 RLS 但没有任何 CREATE POLICY 覆盖: {sorted(missing)}"
