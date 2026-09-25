"""Deterministic scientific intent frame used before model tool selection.

The frame deliberately captures only high-confidence routing facts.  It is not
a semantic answer generator: ambiguous entities remain unresolved and are
handled by the corresponding authoritative executor.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


class ScientificAction(StrEnum):
    UNKNOWN = "UNKNOWN"
    GENE_PROFILE = "GENE_PROFILE"
    SEQUENCE_EXPORT = "SEQUENCE_EXPORT"
    OFFICIAL_LINK = "OFFICIAL_LINK"
    PROTEIN_PROFILE = "PROTEIN_PROFILE"
    LITERATURE_SEARCH = "LITERATURE_SEARCH"
    DATASET_DISCOVERY = "DATASET_DISCOVERY"
    HOMOLOGY = "HOMOLOGY"
    EXPRESSION = "EXPRESSION"
    FULL_GENE_DOSSIER = "FULL_GENE_DOSSIER"


class ScientificDeliverable(StrEnum):
    VALUE_TABLE = "VALUE_TABLE"
    FASTA = "FASTA"
    LINK_CARD = "LINK_CARD"
    BIBLIOGRAPHY = "BIBLIOGRAPHY"
    DATASET_LIST = "DATASET_LIST"


@dataclass(frozen=True)
class ScientificIntentFrame:
    action: ScientificAction = ScientificAction.UNKNOWN
    entity: str | None = None
    organism: str | None = None
    provider: str | None = None
    sequence_type: str | None = None
    deliverable: ScientificDeliverable = ScientificDeliverable.VALUE_TABLE
    detail_level: str = "STANDARD"


_PROVIDERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<![A-Za-z0-9_])(?:NCBI|Entrez)(?![A-Za-z0-9_])", re.I), "NCBI"),
    (re.compile(r"(?<![A-Za-z0-9_])UniProt(?:KB)?(?![A-Za-z0-9_])", re.I), "UNIPROT"),
    (re.compile(r"(?<![A-Za-z0-9_])Europe\s*PMC(?![A-Za-z0-9_])", re.I), "EUROPE_PMC"),
    (re.compile(r"(?<![A-Za-z0-9_])PRIDE(?![A-Za-z0-9_])", re.I), "PRIDE"),
    (re.compile(r"(?<![A-Za-z0-9_])Gramene(?![A-Za-z0-9_])", re.I), "GRAMENE"),
    (re.compile(r"(?<![A-Za-z0-9_])Plant[\s-]*Genomics(?![A-Za-z0-9_])", re.I), "PLANT_GENOMICS"),
    (
        re.compile(
            r"(?<![A-Za-z0-9_])(?:RiceKB|RAP-?DB|Oryzabase)(?![A-Za-z0-9_])",
            re.I,
        ),
        "RICEKB",
    ),
)
_OFFICIAL_LINK = re.compile(r"(?:官网|官方网站|官方(?:主页|网页|页面|地址|链接|入口)|主页|网址|URL|链接|地址)", re.I)
_SEQUENCE = re.compile(
    r"(?:CDS|cDNA|mRNA|转录本|蛋白|protein|基因组|genomic|FASTA).{0,12}(?:序列|下载|保存)|"
    r"(?:序列|下载|保存).{0,12}(?:CDS|cDNA|mRNA|转录本|蛋白|protein|基因组|genomic|FASTA)",
    re.I,
)
_RAP_OR_MSU = re.compile(
    r"(?<![A-Za-z0-9_])(?:Os(?:0[1-9]|1[0-2])[gt]\d{5,7}(?:-\d{2})?|"
    r"LOC_Os(?:0[1-9]|1[0-2])g\d{5,7}(?:\.\d+)?)(?![A-Za-z0-9_])",
    re.I,
)
_ENTITY_BEFORE_PROVIDER = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z][A-Za-z0-9_.-]{0,39})(?![A-Za-z0-9_.-])"
    r"\s*的?(?:在|到|从)?\s*(?:NCBI|Entrez|UniProt|Gramene|RiceKB)",
    re.I,
)
_ENTITY_BEFORE_ACTION = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z][A-Za-z0-9_.-]{0,39})(?![A-Za-z0-9_.-])"
    r"\s*的\s*(?:CDS|cDNA|mRNA|转录本|蛋白|基因组|FASTA|序列|基因档案|详细档案|完整档案)",
    re.I,
)
_RESERVED = frozenset({"MCP", "CDS", "CDNA", "MRNA", "FASTA", "NCBI", "ENTREZ", "UNIPROT", "GRAMENE", "RICEKB"})


def _normalize(question: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(question or ""))).strip()


def _provider(text: str) -> str | None:
    for pattern, provider in _PROVIDERS:
        if pattern.search(text):
            return provider
    return None


def _entity(text: str) -> str | None:
    locus = _RAP_OR_MSU.search(text)
    if locus:
        return locus.group(0)
    for pattern in (_ENTITY_BEFORE_PROVIDER, _ENTITY_BEFORE_ACTION):
        match = pattern.search(text)
        if match and match.group(1).upper() not in _RESERVED:
            return match.group(1)
    return None


def _sequence_type(text: str) -> str | None:
    # Python's Unicode ``\b`` does not split Chinese and ASCII word characters;
    # use ASCII-only lookarounds so ``Wx的CDS序列`` and ``Wx CDS 序列`` agree.
    if re.search(r"(?<![A-Za-z0-9_])CDS(?![A-Za-z0-9_])", text, re.I):
        return "cds"
    if re.search(r"蛋白|(?<![A-Za-z0-9_])protein(?![A-Za-z0-9_])", text, re.I):
        return "protein"
    if re.search(r"基因组|genomic|gene\s+sequence", text, re.I):
        return "gene"
    if re.search(r"cDNA|mRNA|转录本", text, re.I):
        return "transcript"
    return None


def parse_scientific_intent(question: str) -> ScientificIntentFrame:
    text = _normalize(question)
    provider = _provider(text)
    entity = _entity(text)
    sequence_type = _sequence_type(text)
    organism = (
        "Oryza sativa"
        if re.search(r"水稻|稻|Oryza\s+sativa|(?<![A-Za-z0-9_])Wx(?![A-Za-z0-9_])", text, re.I)
        or _RAP_OR_MSU.search(text)
        else None
    )

    if provider and _OFFICIAL_LINK.search(text):
        action = ScientificAction.OFFICIAL_LINK
        deliverable = ScientificDeliverable.LINK_CARD
    elif _SEQUENCE.search(text):
        action = ScientificAction.SEQUENCE_EXPORT
        deliverable = ScientificDeliverable.FASTA
    elif re.search(r"(?:详细|完整).{0,4}(?:基因)?档案|全景档案", text, re.I):
        action = ScientificAction.FULL_GENE_DOSSIER
        deliverable = ScientificDeliverable.VALUE_TABLE
    elif re.search(r"(?:基因)?档案|完整信息", text, re.I):
        action = ScientificAction.GENE_PROFILE
        deliverable = ScientificDeliverable.VALUE_TABLE
    elif re.search(r"同源|ortholog|homolog", text, re.I):
        action = ScientificAction.HOMOLOGY
        deliverable = ScientificDeliverable.VALUE_TABLE
    elif re.search(r"表达|expression", text, re.I):
        action = ScientificAction.EXPRESSION
        deliverable = ScientificDeliverable.VALUE_TABLE
    elif re.search(r"论文|文献|literature|papers?", text, re.I):
        action = ScientificAction.LITERATURE_SEARCH
        deliverable = ScientificDeliverable.BIBLIOGRAPHY
    elif re.search(r"数据集|dataset|GEO|SRA|BioProject|PXD\d*", text, re.I):
        action = ScientificAction.DATASET_DISCOVERY
        deliverable = ScientificDeliverable.DATASET_LIST
    elif provider == "UNIPROT" or re.search(r"蛋白信息|protein profile", text, re.I):
        action = ScientificAction.PROTEIN_PROFILE
        deliverable = ScientificDeliverable.VALUE_TABLE
    else:
        action = ScientificAction.UNKNOWN
        deliverable = ScientificDeliverable.VALUE_TABLE

    return ScientificIntentFrame(
        action=action,
        entity=entity,
        organism=organism,
        provider=provider,
        sequence_type=sequence_type,
        deliverable=deliverable,
        detail_level="FULL" if action == ScientificAction.FULL_GENE_DOSSIER else "STANDARD",
    )


__all__ = [
    "ScientificAction",
    "ScientificDeliverable",
    "ScientificIntentFrame",
    "parse_scientific_intent",
]
