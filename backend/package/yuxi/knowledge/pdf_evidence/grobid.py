from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import httpx

TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}
MAX_TEI_BYTES = 20 * 1024 * 1024


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _person_name(node: ET.Element) -> str:
    """由 persName 生成「名字 姓氏」形式；逐文本片段以空格连接，避免
    <forename>Ashish</forename><surname>Vaswani</surname> 粘连成 AshishVaswani。"""
    return " ".join(" ".join(node.itertext()).split())


def _doi_from_node(node: ET.Element) -> str | None:
    for identifier in node.findall(".//tei:idno", TEI_NS):
        if str(identifier.attrib.get("type", "")).lower() == "doi":
            value = _text(identifier)
            if value:
                return value
    match = re.search(r"10\.\d{4,9}/\S+", _text(node), re.IGNORECASE)
    return match.group(0).rstrip(".,;)") if match else None


def parse_tei(tei_xml: str) -> dict[str, Any]:
    encoded_size = len(tei_xml.encode("utf-8"))
    if encoded_size > MAX_TEI_BYTES:
        raise ValueError(f"GROBID TEI 超过安全上限: {encoded_size} bytes")
    lowered = tei_xml[:4096].lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ValueError("GROBID TEI 包含不允许的 DTD/ENTITY 声明")
    root = ET.fromstring(tei_xml)
    title = _text(root.find(".//tei:titleStmt/tei:title", TEI_NS))
    abstract = _text(root.find(".//tei:profileDesc/tei:abstract", TEI_NS))
    authors = []
    seen: set[str] = set()
    # GROBID 0.9.x 将正文作者置于 sourceDesc/biblStruct/analytic/author；
    # 部分 TEI 变体放在 titleStmt/author。统一在 fileDesc 作用域查找两类位置，
    # 只取 persName 姓名，避免把 email/affiliation 拼进名字；去重保持顺序。
    for author in root.findall(".//tei:fileDesc//tei:author", TEI_NS):
        pers = author.find("tei:persName", TEI_NS)
        name = _person_name(pers if pers is not None else author)
        if name and name not in seen:
            seen.add(name)
            authors.append(name)

    sections: list[dict[str, Any]] = []
    for index, div in enumerate(root.findall(".//tei:text/tei:body//tei:div", TEI_NS)):
        heading = _text(div.find("tei:head", TEI_NS))
        paragraphs = [_text(node) for node in div.findall("tei:p", TEI_NS)]
        paragraphs = [paragraph for paragraph in paragraphs if paragraph]
        if heading or paragraphs:
            sections.append({"section_index": index, "heading": heading, "paragraphs": paragraphs})

    references: list[dict[str, Any]] = []
    for index, item in enumerate(root.findall(".//tei:listBibl/tei:biblStruct", TEI_NS)):
        reference_id = item.attrib.get("{http://www.w3.org/XML/1998/namespace}id") or f"b{index}"
        references.append(
            {
                "reference_id": reference_id,
                "title": _text(item.find(".//tei:title[@level='a']", TEI_NS))
                or _text(item.find(".//tei:title", TEI_NS)),
                "doi": _doi_from_node(item),
                "raw": _text(item),
            }
        )

    mentions: list[dict[str, Any]] = []
    for paragraph in root.findall(".//tei:text/tei:body//tei:p", TEI_NS):
        context = _text(paragraph)
        for ref in paragraph.findall(".//tei:ref[@type='bibr']", TEI_NS):
            target = str(ref.attrib.get("target") or "").lstrip("#")
            mentions.append(
                {
                    "mention_id": f"cm_{len(mentions)}",
                    "reference_id": target or None,
                    "text": _text(ref),
                    "context": context,
                }
            )

    return {
        "metadata": {"title": title, "abstract": abstract, "authors": authors},
        "sections": sections,
        "references": references,
        "citation_mentions": mentions,
    }


class GrobidClient:
    def __init__(self, base_url: str | None = None, timeout_seconds: float | None = None):
        self.base_url = (base_url or os.getenv("GROBID_URL") or "http://grobid:8070").rstrip("/")
        self.timeout_seconds = timeout_seconds or float(os.getenv("GROBID_TIMEOUT_SECONDS", "180"))

    async def process(self, file_path: str | Path) -> tuple[str, dict[str, Any]]:
        path = Path(file_path)
        timeout = httpx.Timeout(self.timeout_seconds, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            with path.open("rb") as source:
                response = await client.post(
                    f"{self.base_url}/api/processFulltextDocument",
                    files={"input": (path.name, source, "application/pdf")},
                    data={
                        "consolidateHeader": "0",
                        "consolidateCitations": "0",
                        "includeRawCitations": "1",
                        "segmentSentences": "1",
                    },
                )
        response.raise_for_status()
        tei_xml = response.text
        return tei_xml, parse_tei(tei_xml)
