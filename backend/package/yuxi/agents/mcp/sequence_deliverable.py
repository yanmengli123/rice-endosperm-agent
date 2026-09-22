"""ricekb_sequence 结果的"哈希锚定交付物"：完整 FASTA 由服务端程序字节级落盘。

设计原则（P0-A 终稿）：

1. **序列字节永不经过模型转写**。回答正文只发布摘要事实（sequence_id /
   sequence_length / sequence_sha256，均 ≤240 字符、可入事实账本带 MCP-F）；
   完整 FASTA 从工具 envelope 的 ``data.sequence`` 由程序写入线程 outputs。
2. **写入前完整性门**：``sha256(data.sequence)`` 必须与 ``data.sequence_sha256``
   一致（网关侧 051 校验同口径），不一致拒绝落盘——上游损坏/被篡改的数据
   不得变成带"已核验"光环的交付物。
3. **零新增鉴权面**：交付物落在虚拟路径 ``/home/gem/user-data/outputs/
   sequence_deliverables/`` 下，由既有 viewer 文件面板与下载端点
   （viewer_filesystem_service，含线程归属与路径穿越校验）提供访问控制。
4. **尽力而为**：交付物失败（无执行上下文、IO 错误、完整性不过）绝不
   阻断工具调用与回答流，只记日志；摘要事实仍随账本发布。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from yuxi.utils import logger
from yuxi.utils.paths import VIRTUAL_PATH_OUTPUTS

SEQUENCE_DELIVERABLE_TOOL = "ricekb_sequence"
SEQUENCE_DELIVERABLE_TAG = "YUXI_SEQUENCE_DELIVERABLE"
SEQUENCE_DELIVERABLE_DIR_NAME = "sequence_deliverables"
_FASTA_LINE_WIDTH = 60
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class SequenceDeliverable:
    sequence_id: str
    sequence_type: str
    sequence: str
    sequence_length: int
    sequence_sha256: str
    description: str
    source_table: str


def extract_sequence_deliverable(result_text: str) -> SequenceDeliverable | None:
    """从 ricekb_sequence 的工具观察文本解析交付物要素。

    观察文本 = envelope JSON + 可能追加的事实账本块；用 raw_decode 只取
    JSON 前缀，天然忽略尾部附加块。非 FOUND / 缺序列字段的返回 None。
    """
    candidate = str(result_text or "").strip()
    if not candidate or candidate[0] != "{":
        return None
    try:
        envelope, _ = json.JSONDecoder().raw_decode(candidate)
    except ValueError:
        return None
    if not isinstance(envelope, dict) or str(envelope.get("status") or "") != "FOUND":
        return None
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    sequence_id = str(data.get("sequence_id") or "").strip()
    sequence = str(data.get("sequence") or "")
    sha = str(data.get("sequence_sha256") or "").strip().lower()
    try:
        length = int(data.get("sequence_length") or 0)
    except (TypeError, ValueError):
        length = 0
    if not sequence_id or not sequence or not sha:
        return None
    return SequenceDeliverable(
        sequence_id=sequence_id,
        sequence_type=str(data.get("sequence_type") or "sequence"),
        sequence=sequence,
        sequence_length=length or len(sequence),
        sequence_sha256=sha,
        description=str(data.get("description") or "").strip(),
        source_table=str(data.get("source_table") or ""),
    )


def verify_sequence_integrity(spec: SequenceDeliverable) -> bool:
    """完整性门：sequence 字节的自算哈希必须与上游 sequence_sha256 一致。"""
    computed = hashlib.sha256(spec.sequence.encode("utf-8")).hexdigest()
    return computed == spec.sequence_sha256


def deliverable_filename(spec: SequenceDeliverable) -> str:
    safe_id = _SAFE_FILENAME_RE.sub("-", spec.sequence_id).strip(".-") or "sequence"
    safe_type = _SAFE_FILENAME_RE.sub("-", spec.sequence_type).strip(".-") or "seq"
    return f"{safe_id}_{safe_type}.fa"


def deliverable_virtual_path(filename: str) -> str:
    return f"{VIRTUAL_PATH_OUTPUTS}/{SEQUENCE_DELIVERABLE_DIR_NAME}/{filename}"


def render_fasta(spec: SequenceDeliverable) -> str:
    """确定性渲染 FASTA：头部锚定溯源与完整性哈希，序列 60 列折行。

    渲染是纯函数：同一 envelope 永远得到同一字节；哈希写在头部使文件
    自带完整性锚点，下载方可用 ``sha256(去头部折行序列)`` 本地复核。
    """
    header_parts = [spec.sequence_id]
    if spec.description:
        header_parts.append(spec.description)
    if spec.source_table:
        header_parts.append(f"source={spec.source_table}")
    header_parts.append(f"sha256={spec.sequence_sha256}")
    lines = [">" + " | ".join(header_parts)]
    sequence = spec.sequence
    for offset in range(0, len(sequence), _FASTA_LINE_WIDTH):
        lines.append(sequence[offset : offset + _FASTA_LINE_WIDTH])
    return "\n".join(lines) + "\n"


def append_deliverable_notice(text: str, notice: dict[str, Any]) -> str:
    payload = json.dumps(notice, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{str(text or '').rstrip()}\n\n<{SEQUENCE_DELIVERABLE_TAG}>{payload}</{SEQUENCE_DELIVERABLE_TAG}>"


def _write_deliverable_file(spec: SequenceDeliverable, thread_id: str, uid: str) -> str:
    from yuxi.agents.backends.sandbox import ensure_thread_dirs, sandbox_outputs_dir

    ensure_thread_dirs(thread_id, uid)
    target_dir = sandbox_outputs_dir(thread_id) / SEQUENCE_DELIVERABLE_DIR_NAME
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = deliverable_filename(spec)
    target_path = target_dir / filename
    # 同名不同内容（上游快照更新）时覆盖：交付物始终反映本轮审计调用返回的字节。
    target_path.write_text(render_fasta(spec), encoding="utf-8")
    return filename


async def record_sequence_deliverable(tool_name: str, result_text: str) -> dict[str, Any] | None:
    """落盘交付物并返回模型可见通知；任何失败都只记日志、返回 None。"""
    if tool_name != SEQUENCE_DELIVERABLE_TOOL:
        return None
    try:
        spec = extract_sequence_deliverable(result_text)
        if spec is None:
            return None
        if not verify_sequence_integrity(spec):
            logger.warning(
                "Sequence deliverable rejected: sha256 mismatch for "
                f"{spec.sequence_id} ({spec.sequence_type}); upstream bytes not verifiable"
            )
            return None
        from yuxi.agents.mcp.execution import get_mcp_execution_context

        context = get_mcp_execution_context()
        if context is None or not context.thread_id:
            # 无线程上下文（如离线评估）无处落盘：跳过交付物，摘要事实不受影响。
            return None
        thread_id = str(context.thread_id)
        uid = str(context.uid)
        filename = await asyncio.to_thread(_write_deliverable_file, spec, thread_id, uid)
    except Exception as error:  # 交付物是增强通道，绝不能阻断工具调用主流程
        logger.warning(f"Sequence deliverable recording skipped: {type(error).__name__}: {error}")
        return None
    notice = {
        "schema_version": "sequence-deliverable.v1",
        "sequence_id": spec.sequence_id,
        "sequence_type": spec.sequence_type,
        "sequence_length": spec.sequence_length,
        "sequence_sha256": spec.sequence_sha256,
        "file_name": filename,
        "path": deliverable_virtual_path(filename),
    }
    return notice


__all__ = [
    "SEQUENCE_DELIVERABLE_DIR_NAME",
    "SEQUENCE_DELIVERABLE_TAG",
    "SEQUENCE_DELIVERABLE_TOOL",
    "SequenceDeliverable",
    "append_deliverable_notice",
    "deliverable_filename",
    "deliverable_virtual_path",
    "extract_sequence_deliverable",
    "record_sequence_deliverable",
    "render_fasta",
    "verify_sequence_integrity",
]
