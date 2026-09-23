"""本轮产物清单块：发布链尾（全门禁之后）追加的确定性下载入口。

定位（P5，实测缺陷修正版）：
- **join 语义**：以 ``run_artifacts.run_id`` 为第一维限定本轮；``origin.source
  == "mcp"`` 的行再按 adopted 审计集合过滤（保留审计语义）；
  ``sequence_deliverable`` / ``agent_presented`` 行按 run_id 直收——
  sequence_deliverable 的 origin **没有** mcp_call_audit_id，按审计 id join
  会恰好漏掉最关键的 .fa 文件。
- **注入时机**：产物元数据（size_bytes 等）不是 manifest 事实，进组合复跑
  门禁必被 ``unsupported_numbers`` 打回——本块只允许在终态门禁、组合复跑与
  citation 分派**全部之后**追加，且不携带 MCP-F marker。
- 数据源是 ``run_artifacts`` 表权威行（零幻觉）；查询失败/无产物 → 返回
  None，调用方跳过追加，绝不阻断发布。
"""

from __future__ import annotations

from typing import Any

_ARTIFACT_GUIDANCE = "以上产物可在消息下方产物卡中下载或保存到工作区；SHA256 前缀用于校验下载完整性。"


def select_publishable_artifacts(rows: list[dict] | None, adopted_audit_ids: set[int] | None) -> list[dict]:
    """按修正版 join 语义筛选本轮可发布的产物行。

    - 非 ``mcp`` 来源（sequence_deliverable / agent_presented）：按 run_id 直收；
    - ``mcp`` 来源：``origin.mcp_call_audit_id`` 必须落在本轮 adopted 成功调用
      集合内（保留审计语义，防止未采纳的探查调用混入清单）。
    """
    adopted = adopted_audit_ids or set()
    selected: list[dict] = []
    for row in rows or []:
        origin = row.get("origin") if isinstance(row, dict) else None
        origin = origin if isinstance(origin, dict) else {}
        if str(origin.get("source") or "") == "mcp":
            audit_id = origin.get("mcp_call_audit_id")
            try:
                if audit_id is None or int(audit_id) not in adopted:
                    continue
            except (TypeError, ValueError):
                continue
        selected.append(row)
    return selected


def _fmt_size(size_bytes: Any) -> str:
    try:
        size = float(size_bytes or 0)
    except (TypeError, ValueError):
        size = 0
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _artifact_kind_label(row: dict) -> str:
    name = str(row.get("name") or "")
    media_type = str(row.get("media_type") or "")
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {
        "fa": "FASTA",
        "fasta": "FASTA",
        "json": "JSON",
        "md": "Markdown",
        "csv": "CSV",
        "txt": "文本",
    }.get(suffix, media_type or "文件")


def render_run_artifacts_block(rows: list[dict] | None) -> str | None:
    """渲染「本轮产物与校验」markdown 块；空清单返回 None。"""
    publishable = [row for row in rows or [] if str(row.get("name") or "").strip()]
    if not publishable:
        return None
    lines = [
        "**本轮产物**",
        "",
        "| 产物 | 类型 | 大小 | 完整性（sha256 前 8 位） |",
        "| --- | --- | --- | --- |",
    ]
    for row in publishable:
        sha = str(row.get("sha256") or "")
        lines.append(
            f"| `{row['name']}` | {_artifact_kind_label(row)} | {_fmt_size(row.get('size_bytes'))} | `{sha[:8]}` |"
        )
    lines.extend(["", _ARTIFACT_GUIDANCE])
    return "\n".join(lines)


__all__ = ["render_run_artifacts_block", "select_publishable_artifacts"]
