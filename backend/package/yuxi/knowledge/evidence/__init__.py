"""科研证据编织层（yuxi.scientific-evidence.v1）。

P0 读取闭环：从既有 chunk + EvidenceAnchor + ParseRevision 组装完整证据
DTO（quote 三 selector + 物理定位 + 版本链 + 确定性验证结果），不落新表。
P2-10/P2-11：evidence_spans 证据单元 + scientific_lexical_index 词法倒排。
P0-P2（GREP 通道）：verbatim 确定性字面量检索（消费上述词法倒排与 span）。
"""

from yuxi.knowledge.evidence.assembler import assemble_evidence_for_run
from yuxi.knowledge.evidence.protocol import (
    EVIDENCE_ID_ALGO_VERSION,
    SCIENTIFIC_EVIDENCE_SCHEMA_VERSION,
    derive_evidence_id,
)
from yuxi.knowledge.evidence.sentence_splitter import SPLITTER_VERSION, split_evidence_units
from yuxi.knowledge.evidence.span_builder import (
    EVIDENCE_SPAN_BUILDER_VERSION,
    build_evidence_spans,
    build_lexical_index,
    extract_lexical_rows,
)
from yuxi.knowledge.evidence.validator import verify_evidence
from yuxi.knowledge.evidence.verbatim import (
    VERBATIM_CHANNEL_VERSION,
    extract_verbatim_patterns,
    query_verbatim_evidence,
)

__all__ = [
    "EVIDENCE_ID_ALGO_VERSION",
    "EVIDENCE_SPAN_BUILDER_VERSION",
    "SCIENTIFIC_EVIDENCE_SCHEMA_VERSION",
    "SPLITTER_VERSION",
    "VERBATIM_CHANNEL_VERSION",
    "assemble_evidence_for_run",
    "build_evidence_spans",
    "build_lexical_index",
    "derive_evidence_id",
    "extract_lexical_rows",
    "extract_verbatim_patterns",
    "query_verbatim_evidence",
    "split_evidence_units",
    "verify_evidence",
]
