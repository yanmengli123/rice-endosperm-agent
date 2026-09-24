"""证据定位协议（ADR-0006 §4）：PDF 与 OA XML 共用契约，不共用定位语义。

不变量：

- ``EvidenceSpanRecord`` 是 PDF/XML 证据的唯一汇合点；定位语义由本模块的
  Locator 类型表达，互不伪装。
- ``E4_VERBATIM_LOCATOR``（「原文第几页」）只能由 ``PDFLocator`` 满足；
  ``XMLLocator`` 最高满足 ``E3_CLAIM_EVIDENCE``。OA XML 命中而用户要页码时，
  门禁必须明示「该文仅有 OA XML 全文，无页码定位」，**禁止编造页码**。
- ``EvidenceAnchorRecord.page/bbox`` 为 NOT NULL，XML 证据物理上不得写入；
  XML 定位挂在 span 的受控 metadata 子结构（``xml_locator`` 键）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from yuxi.knowledge.planning.turn_execution_plan import EvidenceLevel

LOCATOR_CONTRACT_VERSION = "evidence-locator.v1"

# span.metadata_json 中承载 XMLLocator 的受控键
SPAN_METADATA_XML_LOCATOR_KEY = "xml_locator"

_PMCID_RE = re.compile(r"^PMC\d{3,}$")


class LocatorKind(StrEnum):
    PDF = "PDF"
    XML = "XML"


class LocatorValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PDFLocator:
    """PDF 物理定位：页码与版面框必填（对齐 evidence_anchors 的 NOT NULL 约束）。"""

    anchor_id: str
    page: int
    bbox: tuple[float, float, float, float]
    quote_hash: str
    kind: ClassVar[LocatorKind] = LocatorKind.PDF

    def __post_init__(self) -> None:
        if not str(self.anchor_id or "").strip():
            raise LocatorValidationError("PDFLocator requires anchor_id")
        if not isinstance(self.page, int) or self.page < 1:
            raise LocatorValidationError("PDFLocator page must be a positive integer")
        if len(self.bbox) != 4 or not all(isinstance(v, int | float) for v in self.bbox):
            raise LocatorValidationError("PDFLocator bbox must be a 4-tuple of numbers")
        if not str(self.quote_hash or "").strip():
            raise LocatorValidationError("PDFLocator requires quote_hash")


@dataclass(frozen=True, slots=True)
class XMLLocator:
    """JATS OA XML 定位：元素路径 + 字符区间；**没有也不允许有页码字段**。"""

    pmcid: str
    element_path: str
    char_start: int
    char_end: int
    quote_hash: str
    kind: ClassVar[LocatorKind] = LocatorKind.XML

    def __post_init__(self) -> None:
        if not _PMCID_RE.match(str(self.pmcid or "")):
            raise LocatorValidationError("XMLLocator pmcid must match PMC<digits>")
        if not str(self.element_path or "").strip():
            raise LocatorValidationError("XMLLocator requires element_path")
        if not isinstance(self.char_start, int) or not isinstance(self.char_end, int) or self.char_start < 0:
            raise LocatorValidationError("XMLLocator char offsets must be non-negative integers")
        if self.char_end <= self.char_start:
            raise LocatorValidationError("XMLLocator char_end must be greater than char_start")
        if not str(self.quote_hash or "").strip():
            raise LocatorValidationError("XMLLocator requires quote_hash")

    def to_span_metadata(self) -> dict[str, object]:
        """序列化为 span.metadata_json 的受控子结构（唯一合法载体）。"""
        return {
            "contract": LOCATOR_CONTRACT_VERSION,
            "kind": LocatorKind.XML.value,
            "pmcid": self.pmcid,
            "element_path": self.element_path,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "quote_hash": self.quote_hash,
        }


def max_evidence_level(locator: PDFLocator | XMLLocator) -> EvidenceLevel:
    """该定位类型最高可满足的证据义务（门禁比对用的上界）。"""
    if isinstance(locator, PDFLocator):
        return EvidenceLevel.VERBATIM_LOCATOR
    if isinstance(locator, XMLLocator):
        return EvidenceLevel.CLAIM_EVIDENCE
    raise LocatorValidationError(f"unknown locator type: {type(locator).__name__}")


def supports_exact_locator_request(locator: PDFLocator | XMLLocator) -> bool:
    """E4（「原文第几页」）请求是否可被该定位类型满足。"""
    return max_evidence_level(locator) == EvidenceLevel.VERBATIM_LOCATOR


__all__ = [
    "LOCATOR_CONTRACT_VERSION",
    "LocatorKind",
    "LocatorValidationError",
    "PDFLocator",
    "SPAN_METADATA_XML_LOCATOR_KEY",
    "XMLLocator",
    "max_evidence_level",
    "supports_exact_locator_request",
]
