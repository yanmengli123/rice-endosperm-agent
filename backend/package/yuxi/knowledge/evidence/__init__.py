"""科研证据编织层（yuxi.scientific-evidence.v1）。

P0 读取闭环：从既有 chunk + EvidenceAnchor + ParseRevision 组装完整证据
DTO（quote 三 selector + 物理定位 + 版本链 + 确定性验证结果），不落新表。
"""

from yuxi.knowledge.evidence.assembler import assemble_evidence_for_run
from yuxi.knowledge.evidence.protocol import (
    EVIDENCE_ID_ALGO_VERSION,
    SCIENTIFIC_EVIDENCE_SCHEMA_VERSION,
    derive_evidence_id,
)
from yuxi.knowledge.evidence.validator import verify_evidence

__all__ = [
    "SCIENTIFIC_EVIDENCE_SCHEMA_VERSION",
    "EVIDENCE_ID_ALGO_VERSION",
    "derive_evidence_id",
    "verify_evidence",
    "assemble_evidence_for_run",
]
