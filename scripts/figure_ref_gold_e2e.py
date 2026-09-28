"""图表引用锚点金标验收（ADR-0008）：真实 run 全链路断言 + 环境原值恢复。

在真实栈（api :5050 + worker）上执行一次完整的图文并联金标问答，断言：
SSE `citation_ready.figure_refs` ↔ 消息落库 `extra_metadata.citation_ready`
严格一致（发射解耦 / figure_index 回填 / 抑制降级），并在结束时把所有触碰过的
配置开关**恢复为启动时读到的原值**（绝不硬编码——否则每跑一次金标就漂一次
环境，2026-09-26 事故的教训）。

用法（在仓库根目录，宿主机需能访问 api）::

    # 容器内（推荐；令牌不出容器）：
    docker compose exec -T api uv run --no-sync python - < scripts/figure_ref_gold_e2e.py
    # 自定义问题 / 保留线程便于排查：
    docker compose exec -T api uv run --no-sync python scripts/figure_ref_gold_e2e.py \\
        --query "..." --keep-thread

认证：用应用自身的 JWT 工具为 --uid 指定的用户铸造短时令牌（默认 superadmin
uid=1；零 DB 写入、不改密码）。断言失败以非零码退出，可作灰度验收门。

金标语料（开发实例）：RC-G3 库 `kb_sgm3mj317r` 的 OsMYB73 论文（Figure 5/6
有图组资产，S5/S21 为预期 no_asset_row 抑制样本）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta

import httpx

DEFAULT_QUERY = (
    '@doc:"file_708e44" 请依据这篇论文中关于 CRISPR 突变体籽粒表型的图注与正文，'
    '解释突变体粒长变长、出现腹白垩白的原因，并注明依据来自哪个图。'
)
DEFAULT_THREAD_TITLE = "figref-gold-e2e"
BASE_URL = "http://127.0.0.1:5050"
# 本脚本触碰的开关：结束时一律恢复为启动时的原值
TOUCHED_SWITCHES = ("figure_ref_anchor_enabled", "figure_card_enabled")


def _fail(checks: list[tuple[bool, str]]) -> int:
    """打印断言摘要，返回退出码（0=全过）。"""
    failed = [message for passed, message in checks if not passed]
    for message in failed:
        print(f"ASSERT FAIL: {message}")
    print(f"gold e2e: {'PASS' if not failed else f'FAIL ({len(failed)})'} ({len(checks)} checks)")
    return 1 if failed else 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default=DEFAULT_QUERY, help="金标问题（默认 OsMYB73 图注问题）")
    parser.add_argument("--thread-title", default=DEFAULT_THREAD_TITLE)
    parser.add_argument("--agent", default="default-chatbot")
    parser.add_argument("--uid", default="1", help="铸造令牌的用户 id（默认 superadmin）")
    parser.add_argument("--auth-version", type=int, default=0)
    parser.add_argument("--keep-thread", action="store_true", help="保留线程便于排查（默认删除）")
    parser.add_argument("--timeout", type=float, default=240.0, help="run 完成等待秒数")
    args = parser.parse_args()

    from yuxi.utils.auth_utils import AuthUtils

    token = AuthUtils.create_access_token(
        {"sub": args.uid, "auth_version": args.auth_version}, expires_delta=timedelta(minutes=30)
    )
    headers = {"Authorization": f"Bearer {token}"}
    original_switches: dict[str, bool] = {}
    thread_id: str | None = None
    checks: list[tuple[bool, str]] = []
    exit_code = 0
    async with httpx.AsyncClient(base_url=BASE_URL, headers=headers, timeout=args.timeout) as client:
        try:
            # 0) 协议能力位 + 记录开关原值（finally 恢复的依据）
            proto = (await client.get("/api/agent/protocol")).json()
            checks.append((proto.get("protocol_version") == "1.8", f"协议版本 1.8（实测 {proto.get('protocol_version')}）"))
            checks.append(("figure_refs" in proto.get("capabilities", []), "能力位含 figure_refs"))
            checks.append(("table_cards" in proto.get("capabilities", []), "能力位含 table_cards"))
            config_view = (await client.get("/api/system/config")).json()
            config_payload = config_view.get("data") if isinstance(config_view.get("data"), dict) else config_view
            for key in TOUCHED_SWITCHES:
                value = (config_payload or {}).get(key)
                if isinstance(value, dict):  # dump_config 的 {value, ...} 形态
                    value = value.get("value")
                original_switches[key] = bool(value)
            print("original switches:", original_switches)

            for key in TOUCHED_SWITCHES:
                if not original_switches[key]:
                    resp = await client.post("/api/system/config", json={"key": key, "value": True})
                    checks.append((resp.status_code == 200, f"开关 {key} → on"))
            await asyncio.sleep(6)  # Redis 运行时快照双端同步 ≤5s

            # 1) 建线程 + 发起 run + 收 SSE
            thread = (await client.post("/api/chat/thread", json={"agent_id": args.agent, "title": args.thread_title})).json()
            thread_id = thread.get("id") or thread.get("thread_id")
            run = (
                await client.post(
                    "/api/agent/runs",
                    json={"query": args.query, "agent_slug": args.agent, "thread_id": thread_id},
                )
            ).json()
            run_id = run.get("run_id") or run.get("id")
            print("thread:", thread_id, "| run:", run_id)
            checks.append((bool(run_id), "run 创建成功"))

            citation_ready: dict | None = None
            terminal_seen = False
            event_type = "message"
            async with client.stream("GET", f"/api/agent/runs/{run_id}/events") as stream:
                async for line in stream.aiter_lines():
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line[5:].strip() or "null")
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    payload = data.get("payload") or {}
                    for chunk in payload.get("items") or ([payload["chunk"]] if payload.get("chunk") else []):
                        if isinstance(chunk, dict) and chunk.get("status") == "citation_ready":
                            citation_ready = chunk
                    if event_type == "end" or data.get("status"):
                        terminal_seen = True
                        break
            checks.append((terminal_seen, "SSE 终态到达"))
            checks.append((citation_ready is not None, "SSE 收到 citation_ready"))
            if citation_ready is None:
                return _fail(checks)

            refs = citation_ready.get("figure_refs") or []
            figures = citation_ready.get("figures") or []
            checks.append((len(refs) >= 1, f"figure_refs 非空（{len(refs)} 条）"))
            checks.append((all(set(ref) <= _PUBLIC_KEYS for ref in refs), "figure_refs 只含公开键（无内部字段泄漏）"))
            attached = [ref for ref in refs if ref.get("figure_index") is not None]
            suppressed = [ref for ref in refs if ref.get("suppressed_reason")]
            tables = citation_ready.get("tables") or []
            table_refs = [ref for ref in refs if ref.get("kind") == "table"]
            if tables:
                attached_tables = [ref for ref in table_refs if ref.get("table_index") is not None]
                checks.append(
                    (
                        all(0 <= int(ref["table_index"]) < len(tables) for ref in attached_tables),
                        "attached table refs 的 table_index 指向 tables[] 有效下标",
                    )
                )
                checks.append(
                    (
                        all(not str(cell.get("text", "")).strip().startswith("<") for t in tables for row in (t.get("rows") or []) for cell in row),
                        "表格单元格为纯文本（无标记语言泄漏）",
                    )
                )
                # 截断必须可见（ADR-0008 P2）：规模护栏截断（limited）必须并入
                # truncated——否则用户会把被截断的表当成完整表。
                checks.append(
                    (
                        all((not t.get("limited")) or t.get("truncated") for t in tables),
                        "limited 截断已并入 truncated（无静默截断）",
                    )
                )
                checks.append(
                    (
                        all(str((t.get("selection") or {}).get("chunk_id") or "") for t in tables),
                        "表格携带 chunk 审计来源（selection.chunk_id）",
                    )
                )
            if table_refs and not tables:
                checks.append(
                    (
                        all(ref.get("suppressed_reason") for ref in table_refs),
                        "无 tables[] 时 table refs 必须带抑制原因（失败关闭）",
                    )
                )
            print(f"SSE: refs={len(refs)} tables={len(tables)} attached={len(attached)} suppressed={len(suppressed)} figures={len(figures)} limited={sum(1 for t in tables if t.get('limited'))}")
            # figure_index 断言仅在有 figure attached 时适用（table-only run 合法地
            # 全部 figure_index=None，由上方 tables[] 断言族覆盖）
            if attached:
                checks.append(
                    (
                        all(0 <= int(ref["figure_index"]) < len(figures) for ref in attached),
                        "attached refs 的 figure_index 指向 figures[] 有效下标",
                    )
                )
            for ref in refs:
                print(
                    "  -",
                    ref.get("ref"),
                    ref.get("label"),
                    f"page={ref.get('page')}",
                    f"figure_index={ref.get('figure_index')}",
                    f"suppressed={ref.get('suppressed_reason')}",
                )

            # 2) 落库一致性（SSE ↔ extra_metadata.citation_ready）
            await asyncio.sleep(2)  # 落库事务收口
            from sqlalchemy import text as _text

            from yuxi.storage.postgres.manager import pg_manager

            async with pg_manager.get_async_session_context() as session:
                row = (
                    await session.execute(
                        _text(
                            "select extra_metadata::jsonb -> 'citation_ready' as cr, content "
                            "from messages where run_id = :run_id and role = 'assistant' "
                            "and length(content) > 30 order by id desc limit 1"
                        ),
                        {"run_id": run_id},
                    )
                ).first()
            checks.append((row is not None and row.cr is not None, "消息落库含 citation_ready"))
            if row and row.cr:
                persisted_refs = row.cr.get("figure_refs") or []
                persisted_figures = row.cr.get("figures") or []
                checks.append(
                    (
                        [(r.get("ref"), r.get("figure_index")) for r in persisted_refs]
                        == [(r.get("ref"), r.get("figure_index")) for r in refs],
                        f"落库 refs 与 SSE 一致（{len(persisted_refs)} 条）",
                    )
                )
                checks.append((len(persisted_figures) == len(figures), f"落库 figures 数一致（{len(persisted_figures)}）"))
                persisted_tables = row.cr.get("tables") or []
                checks.append((len(persisted_tables) == len(tables), f"落库 tables 数一致（{len(persisted_tables)}）"))
                checks.append(("〔图表F" in str(row.content or ""), "正文含签发芯片"))

                # G6 正确性门禁（2026-09-27：存在性→正确性，用 DB 正文检查）
                import re as _gate_re

                footnote_violations = []
                for _ln, _line in enumerate(str(row.content or "").split("\n"), 1):
                    if "> 表格依据：" not in _line:
                        continue
                    _stripped = _line.strip()
                    if _stripped.startswith("|"):
                        footnote_violations.append(f"L{_ln} C3_swallowed_in_table")
                    elif not _gate_re.match(r"^>?\s*表格依据：〔证据E\d+｜", _stripped):
                        footnote_violations.append(f"L{_ln} C1_empty_or_malformed")
                    if "〔证据E" in _line and " | " in _line:
                        footnote_violations.append(f"L{_ln} C2_halfwidth_separator")
                if footnote_violations:
                    checks.append((False, f"表格脚注正确性违规: {footnote_violations[:3]}"))
                else:
                    checks.append((True, "表格脚注正确性（芯片非空+全角+表外）"))

            # 3) trace 事件（ADMIN 可见性，DB 直查）
            async with pg_manager.get_async_session_context() as session:
                trace_row = (
                    await session.execute(
                        _text(
                            "select count(*) from agent_run_trace_events "
                            "where run_id = :run_id and event_type = 'knowledge.figure_ref.resolved'"
                        ),
                        {"run_id": run_id},
                    )
                ).scalar()
            checks.append((int(trace_row or 0) >= 1, "trace figure_ref.resolved 已写入"))
        finally:
            # 4) 环境恢复：开关回原值；线程清理（除非 --keep-thread）
            for key, original in original_switches.items():
                resp = await client.post("/api/system/config", json={"key": key, "value": original})
                print(f"switch {key} -> {original} (restore):", resp.status_code == 200)
            if thread_id and not args.keep_thread:
                resp = await client.delete(f"/api/chat/thread/{thread_id}")
                print("thread cleanup:", resp.status_code)

    exit_code = _fail(checks)
    return exit_code


# 载荷公开键（与 services/chat_service._figure_refs_public_payload 白名单一致）
_PUBLIC_KEYS = {
    "ref",
    "kind",
    "label",
    "source",
    "citation_ref",
    "evidence_id",
    "anchor_id",
    "kb_id",
    "file_id",
    "revision_id",
    "page",
    "figure_index",
    "table_index",
    "suppressed_reason",
    "visual_status",
}


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
