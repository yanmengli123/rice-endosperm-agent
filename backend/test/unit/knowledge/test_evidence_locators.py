"""Locator 协议 golden（ADR-0006 §4 验收）：E4 只能由 PDF 定位满足。"""

import pytest

from yuxi.knowledge.evidence.locators import (
    LOCATOR_CONTRACT_VERSION,
    SPAN_METADATA_XML_LOCATOR_KEY,
    LocatorValidationError,
    PDFLocator,
    XMLLocator,
    max_evidence_level,
    supports_exact_locator_request,
)
from yuxi.knowledge.planning.turn_execution_plan import EvidenceLevel


def _pdf() -> PDFLocator:
    return PDFLocator(anchor_id="anc_1", page=7, bbox=(10.0, 20.0, 30.0, 40.0), quote_hash="q" * 8)


def _xml() -> XMLLocator:
    return XMLLocator(
        pmcid="PMC1234567",
        element_path="/article/body/sec[2]/p[3]",
        char_start=120,
        char_end=260,
        quote_hash="q" * 8,
    )


def test_pdf_locator_satisfies_verbatim_locator_obligation():
    assert max_evidence_level(_pdf()) == EvidenceLevel.VERBATIM_LOCATOR
    assert supports_exact_locator_request(_pdf())


def test_xml_locator_caps_at_claim_evidence_and_never_fabricates_pages():
    locator = _xml()
    assert max_evidence_level(locator) == EvidenceLevel.CLAIM_EVIDENCE
    assert not supports_exact_locator_request(locator)
    # XMLLocator 没有页码字段——构造期就不存在编造页码的通道
    assert not hasattr(locator, "page")


def test_xml_locator_serializes_into_controlled_span_metadata():
    metadata = _xml().to_span_metadata()
    assert metadata["kind"] == "XML"
    assert metadata["contract"] == LOCATOR_CONTRACT_VERSION
    assert "page" not in metadata
    # 受控键名是 span.metadata_json 的唯一合法载体
    assert SPAN_METADATA_XML_LOCATOR_KEY == "xml_locator"


def test_pdf_locator_requires_physical_positioning():
    with pytest.raises(LocatorValidationError):
        PDFLocator(anchor_id="anc_1", page=0, bbox=(0.0, 0.0, 1.0, 1.0), quote_hash="q" * 8)
    with pytest.raises(LocatorValidationError):
        PDFLocator(anchor_id="anc_1", page=1, bbox=(0.0, 0.0, 1.0), quote_hash="q" * 8)


def test_xml_locator_validates_pmcid_and_offsets():
    with pytest.raises(LocatorValidationError):
        XMLLocator(pmcid="1234567", element_path="/article", char_start=0, char_end=5, quote_hash="q" * 8)
    with pytest.raises(LocatorValidationError):
        XMLLocator(pmcid="PMC123", element_path="/article", char_start=10, char_end=10, quote_hash="q" * 8)
    with pytest.raises(LocatorValidationError):
        XMLLocator(pmcid="PMC123", element_path=" ", char_start=0, char_end=5, quote_hash="q" * 8)


def test_unknown_locator_type_is_fail_closed():
    class _Bogus:
        pass

    with pytest.raises(LocatorValidationError):
        max_evidence_level(_Bogus())
