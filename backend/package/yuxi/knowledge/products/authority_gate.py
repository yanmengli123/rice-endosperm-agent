"""Authority Gate：答案平面与导航平面之间的运行时门禁。

职责（对应四平面不变量）：

1. 只放行 Authority Plane 的证据行（``to_evidence_envelope`` 已在类型层
   拒绝派生产品；这里再做一遍 idempotent 的批量过滤）。
2. 拒绝任何 Wiki 导航命中混入证据通道——导航命中只能通过
   ``navigation_hits`` 通道进入 Retrieval Plan / 提示词扩展词。
3. 提供统一的 ``AuthorityGateError``，调用方（scope_gateway、
   answer_context_builder）捕获后转为告警，而不是静默降级。
"""

from __future__ import annotations

from typing import Any

from yuxi.knowledge.products.contracts import EvidenceEnvelope, WikiNavigationHit, to_evidence_envelope


class AuthorityGateError(Exception):
    """Authority Gate 拒绝了一次越界的数据流动。"""


class AuthorityGate:
    """无状态门禁；所有方法可并发调用。"""

    @staticmethod
    def filter_evidence_rows(rows: list[dict[str, Any]] | None) -> list[EvidenceEnvelope]:
        """把原始检索行压缩为 EvidenceEnvelope 列表。

        派生产品行被直接丢弃并返回给调用方（通过 ``rejected`` 计数暴露），
        而不是抛异常——批量管道里一条脏行不应中断整个回答。
        """
        envelopes: list[EvidenceEnvelope] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            try:
                envelopes.append(to_evidence_envelope(row))
            except Exception:  # noqa: BLE001 - 单行越界不中断批处理
                continue
        return envelopes

    @staticmethod
    def reject_navigation_as_evidence(items: list[Any] | None) -> None:
        """任何 WikiNavigationHit 出现在证据通道都立即抛错。"""
        for item in items or []:
            if isinstance(item, WikiNavigationHit):
                raise AuthorityGateError("WikiNavigationHit cannot enter the evidence channel")

    @staticmethod
    def navigation_hits(items: list[Any] | None) -> list[WikiNavigationHit]:
        """返回真正属于 Navigation Plane 的命中（供召回扩展使用）。"""
        return [item for item in (items or []) if isinstance(item, WikiNavigationHit)]


def gate_evidence(rows: list[dict[str, Any]] | None) -> list[EvidenceEnvelope]:
    """便捷函数：等价于 ``AuthorityGate.filter_evidence_rows``。"""
    return AuthorityGate.filter_evidence_rows(rows)
