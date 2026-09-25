"""emitter↔schema 一致性门禁（2026-09 漂移清理引入，figures 事故红线的协议版）。

三道闸：

1. 反向（事件名）：AST 扫描 yuxi 包内全部 emit/emit_trace 调用的字面量
   event_type，必须已登记进 ``EVENT_ATTRIBUTE_SCHEMAS``——在构建期
   ValueError 之外再加一道编译期前置闸，防止业务代码静默发明事件名。
2. 正向（覆盖）：schema 中每个非 RESERVED 事件必须在
   ``EVENT_EMITTER_INDEX`` 有发射点登记，且登记文件存在、包含登记标记串
   ——防止 schema 沦为纸面字段（credential_source / result_digest /
   mcp_audit_id / message_id 历史漂移的直接反例）。
3. 字面量属性键：emit 调用现场以字面量 dict 传 attributes 时，键必须 ⊆
   对应 schema——verbatim_hit_count 式「发射了但不在白名单被静默剥离」的
   静态可测子集（动态构造 dict 的发射点由 protocol 层运行时漂移哨兵兜底）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from yuxi.trace.protocol import (
    EVENT_ATTRIBUTE_SCHEMAS,
    EVENT_EMITTER_INDEX,
    RESERVED_EVENT_TYPES,
)

pytestmark = [pytest.mark.unit]

PACKAGE_ROOT = Path(__import__("yuxi").__file__).resolve().parent

_EMIT_FUNCS = {"emit", "emit_trace"}
_EMITTING_CALL_ATTRS = {"emit", "emit_trace", "finish_span", "record_run_terminal", "start_span"}


def _iter_emit_calls():
    """遍历包内全部 trace 发射调用点，产出 (path, node)。"""
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _EMITTING_CALL_ATTRS:
                yield path, node
            elif isinstance(func, ast.Name) and func.id in _EMIT_FUNCS:
                yield path, node


def _literal_event_type(call: ast.Call) -> str | None:
    for kw in call.keywords:
        if kw.arg == "event_type" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _literal_attribute_keys(call: ast.Call) -> list[str] | None:
    for kw in call.keywords:
        if kw.arg == "attributes" and isinstance(kw.value, ast.Dict):
            keys = [k.value for k in kw.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if len(keys) == len(kw.value.keys):
                return keys
    return None


def test_no_unregistered_literal_event_types():
    offenders: list[str] = []
    for path, call in _iter_emit_calls():
        event_type = _literal_event_type(call)
        if event_type is None:
            continue
        if event_type not in EVENT_ATTRIBUTE_SCHEMAS:
            offenders.append(f"{path.relative_to(PACKAGE_ROOT)}: {event_type}")
    assert not offenders, f"未登记的字面量事件名（先在 EVENT_ATTRIBUTE_SCHEMAS 登记）：\n{offenders}"


def test_every_non_reserved_schema_event_has_emitter():
    missing = sorted(set(EVENT_ATTRIBUTE_SCHEMAS) - RESERVED_EVENT_TYPES - set(EVENT_EMITTER_INDEX))
    assert not missing, (
        f"schema 登记了但没有发射点索引（补发射、补 EVENT_EMITTER_INDEX，或移入 RESERVED_EVENT_TYPES）：\n{missing}"
    )


def test_emitter_index_only_references_registered_events():
    unknown = sorted(set(EVENT_EMITTER_INDEX) - set(EVENT_ATTRIBUTE_SCHEMAS))
    assert not unknown, f"EVENT_EMITTER_INDEX 引用了未登记事件：{unknown}"
    reserved_leak = sorted(set(EVENT_EMITTER_INDEX) & RESERVED_EVENT_TYPES)
    assert not reserved_leak, f"RESERVED 事件不应有发射点索引（接线后应从 RESERVED 移除）：{reserved_leak}"


def test_emitter_index_files_exist_and_contain_markers():
    problems: list[str] = []
    for event_type, sites in EVENT_EMITTER_INDEX.items():
        for rel_path, marker in sites:
            target = PACKAGE_ROOT / rel_path
            if not target.exists():
                problems.append(f"{event_type}: 文件不存在 {rel_path}")
                continue
            if marker not in target.read_text(encoding="utf-8", errors="ignore"):
                problems.append(f"{event_type}: {rel_path} 中找不到标记串 {marker!r}")
    assert not problems, f"发射点索引失效（发射点被移动/重命名时必须同步更新索引）：\n{problems}"


def test_literal_attribute_keys_stay_within_schema():
    offenders: list[str] = []
    for path, call in _iter_emit_calls():
        event_type = _literal_event_type(call)
        keys = _literal_attribute_keys(call)
        if event_type is None or keys is None:
            continue
        schema = EVENT_ATTRIBUTE_SCHEMAS.get(event_type)
        if schema is None:
            continue
        extra = sorted(set(keys) - schema)
        if extra:
            offenders.append(f"{path.relative_to(PACKAGE_ROOT)}: {event_type} 越界键 {extra}")
    assert not offenders, (
        f"字面量 attributes 越出 schema 白名单（会被协议层静默剥离，先改 schema 或删键）：\n{offenders}"
    )


def test_literal_event_types_are_indexed_when_possible():
    """字面量事件名的发射文件应出现在该事件的索引登记里（索引与真实发射点不脱节）。"""
    problems: list[str] = []
    for path, call in _iter_emit_calls():
        event_type = _literal_event_type(call)
        if event_type is None or event_type in RESERVED_EVENT_TYPES:
            continue
        rel_path = path.relative_to(PACKAGE_ROOT).as_posix()
        sites = EVENT_EMITTER_INDEX.get(event_type, ())
        if not any(rel_path == site_path for site_path, _ in sites):
            problems.append(f"{event_type}: 字面量发射于 {rel_path}，但索引未登记该文件")
    assert not problems, f"字面量发射点未进索引：\n{problems}"
