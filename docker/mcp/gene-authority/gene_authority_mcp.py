#!/usr/bin/env python3
"""Governed MCP facade for official gene, dataset and literature APIs.

Only fixed upstream hosts and bounded, typed operations are exposed.  The
server never accepts arbitrary URLs or CLI arguments.  Every response carries
the exact request URL (without credentials), retrieval time and a deterministic
machine state so an agent can disclose missing or conflicting data instead of
filling gaps from model memory.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from defusedxml import ElementTree

SERVER_VERSION = "1.2.0"
SCHEMA_VERSION = "gene-authority-envelope.v1"
NCBI_BASE = "https://api.ncbi.nlm.nih.gov/datasets/v2"
NCBI_EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
UNIPROT_BASE = "https://rest.uniprot.org"
EUROPE_PMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
PRIDE_BASE = "https://www.ebi.ac.uk/pride/ws/archive/v2"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_IDENTIFIERS = 20
HTTP_TIMEOUT_SECONDS = 45.0
CLI_TIMEOUT_SECONDS = 240
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
UNIPROT_ACCESSION_RE = re.compile(r"^[A-Za-z0-9-]{6,25}$")
ARTICLE_SOURCE_RE = re.compile(r"^[A-Z]{3,12}$")
ARTICLE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
IDENTIFIER_TYPES = ("gene-id", "symbol", "accession")
GENE_INCLUDE_VALUES = ("none", "gene", "rna", "cds", "protein")
ENTREZ_DATABASES = ("pubmed", "pmc", "gds", "sra", "bioproject")
ENTREZ_ID_RE = re.compile(r"^[0-9]{1,18}$")
PXD_RE = re.compile(r"^PXD[0-9]{6,12}$", re.I)
PMCID_RE = re.compile(r"^PMC[0-9]{1,12}$", re.I)


class AuthorityError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _bounded_text(name: str, value: str, maximum: int = 500) -> str:
    text = (value or "").strip()
    if not text or len(text) > maximum:
        raise AuthorityError(f"{name} must contain 1-{maximum} characters")
    return text


def _bounded_int(name: str, value: int, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise AuthorityError(f"{name} must be an integer between {low} and {high}")
    return value


def _identifier_type(value: str) -> str:
    text = (value or "").strip().lower()
    if text not in IDENTIFIER_TYPES:
        raise AuthorityError(f"identifier_type must be one of {', '.join(IDENTIFIER_TYPES)}")
    return text


def _identifiers(values: list[str]) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_IDENTIFIERS:
        raise AuthorityError(f"identifiers must contain 1-{MAX_IDENTIFIERS} values")
    normalized: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not IDENTIFIER_RE.fullmatch(text):
            raise AuthorityError(f"invalid identifier: {text!r}")
        normalized.append(text)
    return list(dict.fromkeys(normalized))


def _gene_path(identifier_type: str, identifiers: list[str], taxon: str | None, suffix: str) -> str:
    kind = _identifier_type(identifier_type)
    ids = _identifiers(identifiers)
    encoded = urllib.parse.quote(",".join(ids), safe=",")
    if kind == "symbol":
        taxon_value = _bounded_text("taxon", taxon or "", 100)
        if not IDENTIFIER_RE.fullmatch(taxon_value.replace(" ", "_")):
            raise AuthorityError("taxon contains unsupported characters")
        return f"/gene/symbol/{encoded}/taxon/{urllib.parse.quote(taxon_value, safe='')}/{suffix}"
    rest_kind = "id" if kind == "gene-id" else kind
    return f"/gene/{rest_kind}/{encoded}/{suffix}"


def _request_json(
    provider: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    private_params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    query = urllib.parse.urlencode({key: value for key, value in (params or {}).items() if value is not None})
    public_url = url + (f"?{query}" if query else "")
    private_query = urllib.parse.urlencode(
        {key: value for key, value in (private_params or {}).items() if value is not None}
    )
    request_url = public_url + (("&" if query else "?") + private_query if private_query else "")
    request_headers = {
        "Accept": "application/json",
        "User-Agent": f"Yuxi-gene-authority/{SERVER_VERSION} ({os.getenv('YUXI_NCBI_EMAIL', 'contact-unset')})",
        **dict(headers or {}),
    }
    api_key = os.getenv("NCBI_API_KEY", "").strip()
    if provider == "NCBI_DATASETS" and api_key:
        request_headers["api-key"] = api_key
    request = urllib.request.Request(request_url, method="GET", headers=request_headers)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
                body = response.read(MAX_RESPONSE_BYTES + 1)
                status_code = int(response.status)
            if len(body) > MAX_RESPONSE_BYTES:
                raise AuthorityError(f"{provider} response exceeded the 8 MiB limit")
            payload = json.loads(body.decode("utf-8"))
            if isinstance(payload, list):
                payload = {"results": payload}
            if not isinstance(payload, dict):
                raise AuthorityError(f"{provider} response must be a JSON object or array")
            return _envelope(provider, public_url, payload, status_code=status_code)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return _envelope(provider, public_url, {}, status="NOT_FOUND", status_code=404)
            last_error = error
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                break
        except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
            last_error = error
            if attempt == 2:
                break
        time.sleep(0.5 * (2**attempt))
    raise AuthorityError(f"{provider} request failed after bounded retries: {type(last_error).__name__}")


def _has_records(payload: dict[str, Any]) -> bool:
    if isinstance(payload.get("esearchresult"), dict):
        return int(payload["esearchresult"].get("count") or 0) > 0
    for key in ("reports", "results", "resultList"):
        value = payload.get(key)
        if isinstance(value, list) and value:
            return True
        if isinstance(value, dict):
            nested = value.get("result")
            if isinstance(nested, list) and nested:
                return True
    return bool(payload) and payload.get("total_count") not in {0, "0"}


def _envelope(
    provider: str,
    request_url: str,
    payload: dict[str, Any],
    *,
    status: str | None = None,
    status_code: int = 200,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status or ("FOUND" if _has_records(payload) else "NOT_FOUND"),
        "provider": provider,
        "retrieved_at": _now(),
        "request": {"method": "GET", "url": request_url, "http_status": status_code},
        "data": payload,
        "answer_policy": "Publish only fields present in data. Preserve NOT_FOUND and conflicts; do not infer.",
    }


def ncbi_gene_report_rest(
    identifiers: list[str],
    identifier_type: str = "gene-id",
    taxon: str | None = None,
    page_size: int = 20,
) -> dict[str, Any]:
    path = _gene_path(identifier_type, identifiers, taxon, "dataset_report")
    return _request_json(
        "NCBI_DATASETS",
        NCBI_BASE + path,
        params={"page_size": _bounded_int("page_size", page_size, 1, 100)},
    )


def _cli_base(identifier_type: str, identifiers: list[str], taxon: str | None) -> list[str]:
    kind = _identifier_type(identifier_type)
    ids = _identifiers(identifiers)
    args = [kind, *ids]
    if kind == "symbol":
        args.extend(["--taxon", _bounded_text("taxon", taxon or "", 100)])
    return args


def _run_datasets(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "NO_COLOR": "1"}
    try:
        completed = subprocess.run(
            ["/usr/local/bin/datasets", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        raise AuthorityError(f"NCBI Datasets CLI timed out after {CLI_TIMEOUT_SECONDS}s") from error
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "unknown CLI failure").strip()[:1000]
        raise AuthorityError(f"NCBI Datasets CLI rejected the bounded request: {message}")
    if len(completed.stdout.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise AuthorityError("NCBI Datasets CLI summary exceeded the 8 MiB limit")
    return completed


def ncbi_gene_summary_cli(
    identifiers: list[str], identifier_type: str = "gene-id", taxon: str | None = None
) -> dict[str, Any]:
    args = ["summary", "gene", *_cli_base(identifier_type, identifiers, taxon), "--as-json-lines"]
    completed = _run_datasets(args)
    records: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "FOUND" if records else "NOT_FOUND",
        "provider": "NCBI_DATASETS_CLI",
        "retrieved_at": _now(),
        "command_contract": "datasets summary gene <allowlisted-selector> --as-json-lines",
        "data": {"reports": records},
        "answer_policy": "Publish only returned CLI fields; do not infer missing records.",
    }


def ncbi_gene_package_cli(
    identifiers: list[str],
    identifier_type: str = "gene-id",
    taxon: str | None = None,
    include: list[str] | None = None,
) -> dict[str, Any]:
    includes = list(dict.fromkeys(include or ["none"]))
    if not includes or any(item not in GENE_INCLUDE_VALUES for item in includes):
        raise AuthorityError(f"include values must be a subset of {', '.join(GENE_INCLUDE_VALUES)}")
    if "none" in includes and len(includes) > 1:
        raise AuthorityError("include=none cannot be combined with sequence payloads")
    root = Path("/home/gem/user-data/artifacts/ncbi-datasets").resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = (root / f"gene-{uuid.uuid4().hex}.zip").resolve()
    if root not in destination.parents:
        raise AuthorityError("artifact path escaped the governed workspace")
    args = [
        "download",
        "gene",
        *_cli_base(identifier_type, identifiers, taxon),
        "--include",
        ",".join(includes),
        "--filename",
        str(destination),
        "--no-progressbar",
    ]
    _run_datasets(args)
    size = destination.stat().st_size
    if size > MAX_DOWNLOAD_BYTES:
        destination.unlink(missing_ok=True)
        raise AuthorityError("downloaded package exceeded the 512 MiB policy limit")
    hasher = hashlib.sha256()
    with destination.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "FOUND",
        "provider": "NCBI_DATASETS_CLI",
        "retrieved_at": _now(),
        "artifact": {
            "path": str(destination),
            "bytes": size,
            "sha256": digest,
            "media_type": "application/zip",
        },
        "answer_policy": "The artifact digest and byte count are deterministic CLI outputs.",
    }


def uniprot_entry_rest(accession: str) -> dict[str, Any]:
    value = _bounded_text("accession", accession, 25).upper()
    if not UNIPROT_ACCESSION_RE.fullmatch(value):
        raise AuthorityError("accession is not a valid bounded UniProt accession")
    return _request_json("UNIPROT", f"{UNIPROT_BASE}/uniprotkb/{urllib.parse.quote(value, safe='')}.json")


def uniprot_search_rest(query: str, size: int = 10, reviewed_only: bool = False) -> dict[str, Any]:
    value = _bounded_text("query", query, 500)
    if reviewed_only:
        value = f"({value}) AND reviewed:true"
    return _request_json(
        "UNIPROT",
        f"{UNIPROT_BASE}/uniprotkb/search",
        params={"query": value, "format": "json", "size": _bounded_int("size", size, 1, 50)},
    )


def europe_pmc_search_rest(query: str, page_size: int = 10) -> dict[str, Any]:
    return _request_json(
        "EUROPE_PMC",
        f"{EUROPE_PMC_BASE}/search",
        params={
            "query": _bounded_text("query", query, 500),
            "format": "json",
            "resultType": "core",
            "pageSize": _bounded_int("page_size", page_size, 1, 50),
        },
    )


def europe_pmc_article_rest(source: str, external_id: str) -> dict[str, Any]:
    source_value = _bounded_text("source", source, 12).upper()
    id_value = _bounded_text("external_id", external_id, 80)
    if not ARTICLE_SOURCE_RE.fullmatch(source_value) or not ARTICLE_ID_RE.fullmatch(id_value):
        raise AuthorityError("source or external_id contains unsupported characters")
    return europe_pmc_search_rest(f'EXT_ID:"{id_value}" AND SRC:{source_value}', page_size=5)


def ncbi_eutils_search_rest(database: str, term: str, retmax: int = 10) -> dict[str, Any]:
    db = _bounded_text("database", database, 20).lower()
    if db not in ENTREZ_DATABASES:
        raise AuthorityError(f"database must be one of {', '.join(ENTREZ_DATABASES)}")
    email = os.getenv("YUXI_NCBI_EMAIL", "").strip()
    if email:
        email = _bounded_text("YUXI_NCBI_EMAIL", email, 200)
    result = _request_json(
        "NCBI_EUTILS",
        f"{NCBI_EUTILS_BASE}/esearch.fcgi",
        params={"db": db, "term": _bounded_text("term", term), "retmax": _bounded_int("retmax", retmax, 1, 50),
                "retmode": "json", "tool": "yuxi-gene-authority"},
        private_params={"email": email, "api_key": os.getenv("NCBI_API_KEY", "").strip() or None},
    )
    result["answer_policy"] = "Discovery IDs only; resolve official records before claiming dataset or article properties."
    return result


def ncbi_eutils_summary_rest(database: str, ids: list[str]) -> dict[str, Any]:
    db = _bounded_text("database", database, 20).lower()
    if db not in ENTREZ_DATABASES:
        raise AuthorityError(f"database must be one of {', '.join(ENTREZ_DATABASES)}")
    values = _identifiers(ids)
    if any(not ENTREZ_ID_RE.fullmatch(value) for value in values):
        raise AuthorityError("Entrez IDs must be decimal integers")
    email = os.getenv("YUXI_NCBI_EMAIL", "").strip()
    if email:
        email = _bounded_text("YUXI_NCBI_EMAIL", email, 200)
    return _request_json(
        "NCBI_EUTILS", f"{NCBI_EUTILS_BASE}/esummary.fcgi",
        params={"db": db, "id": ",".join(values), "retmode": "json", "tool": "yuxi-gene-authority"},
        private_params={"email": email, "api_key": os.getenv("NCBI_API_KEY", "").strip() or None},
    )


def ncbi_eutils_fetch_rest(database: str, ids: list[str]) -> dict[str, Any]:
    db = _bounded_text("database", database, 20).lower()
    if db not in ENTREZ_DATABASES:
        raise AuthorityError(f"database must be one of {', '.join(ENTREZ_DATABASES)}")
    values = _identifiers(ids)
    if any(not ENTREZ_ID_RE.fullmatch(value) for value in values):
        raise AuthorityError("Entrez IDs must be decimal integers")
    email = os.getenv("YUXI_NCBI_EMAIL", "").strip()
    if email:
        email = _bounded_text("YUXI_NCBI_EMAIL", email, 200)
    public_params = {"db": db, "id": ",".join(values), "retmode": "xml",
                     "tool": "yuxi-gene-authority"}
    public_url = f"{NCBI_EUTILS_BASE}/efetch.fcgi?{urllib.parse.urlencode(public_params)}"
    api_key = os.getenv("NCBI_API_KEY", "").strip()
    request_url = public_url + "&" + urllib.parse.urlencode(
        {key: value for key, value in {"email": email, "api_key": api_key}.items() if value}
    )
    raw, status_code = _request_xml(request_url, "NCBI_EUTILS", maximum=1024 * 1024)
    return {
        "schema_version": SCHEMA_VERSION, "status": "FOUND" if raw.strip() else "NOT_FOUND",
        "provider": "NCBI_EUTILS", "retrieved_at": _now(),
        "request": {"method": "GET", "url": public_url, "http_status": status_code},
        "data": {"database": db, "ids": values, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                 "xml": raw.decode("utf-8")},
        "answer_policy": "Official Entrez record XML; do not infer experimental conclusions from metadata.",
    }


def pride_search_projects_rest(keyword: str, page_size: int = 10) -> dict[str, Any]:
    result = _request_json(
        "PRIDE", f"{PRIDE_BASE}/search/projects",
        params={"keyword": _bounded_text("keyword", keyword), "page": 0,
                "pageSize": _bounded_int("page_size", page_size, 1, 25)},
    )
    result["answer_policy"] = "Candidate projects only. Verify accession details, organism, tissue, assay and files."
    return result


def pride_project_rest(accession: str) -> dict[str, Any]:
    value = _bounded_text("accession", accession, 15).upper()
    if not PXD_RE.fullmatch(value):
        raise AuthorityError("accession must be a PXD identifier")
    return _request_json("PRIDE", f"{PRIDE_BASE}/projects/{value}")


def pride_project_files_rest(accession: str, page_size: int = 20) -> dict[str, Any]:
    value = _bounded_text("accession", accession, 15).upper()
    if not PXD_RE.fullmatch(value):
        raise AuthorityError("accession must be a PXD identifier")
    return _request_json(
        "PRIDE", f"{PRIDE_BASE}/projects/{value}/files",
        params={"page": 0, "pageSize": _bounded_int("page_size", page_size, 1, 50)},
    )


def europe_pmc_oa_passages_rest(pmcid: str, query: str, max_hits: int = 5) -> dict[str, Any]:
    """Return verbatim OA paragraph snippets with XML locators, never PDF pages."""
    value = _bounded_text("pmcid", pmcid, 15).upper()
    needle = _bounded_text("query", query, 100)
    limit = _bounded_int("max_hits", max_hits, 1, 10)
    if not PMCID_RE.fullmatch(value):
        raise AuthorityError("pmcid must be a PMC identifier")
    url = f"{EUROPE_PMC_BASE}/{value}/fullTextXML"
    raw, status_code = _request_xml(url, "EUROPE_PMC_OA", maximum=MAX_RESPONSE_BYTES, not_found_ok=True)
    if status_code == 404:
        return _envelope("EUROPE_PMC_OA", url, {}, status="NOT_FOUND", status_code=404)
    try:
        root = ElementTree.fromstring(raw)
    except (ElementTree.ParseError, ElementTree.EntitiesForbidden, ElementTree.ExternalReferenceForbidden) as error:
        raise AuthorityError("Europe PMC OA XML is malformed") from error
    parents = {child: parent for parent in root.iter() for child in parent}
    passages: list[dict[str, Any]] = []
    all_matches = 0
    for paragraph_index, paragraph in enumerate(root.iter("p")):
        if not any(ancestor.tag == "body" for ancestor in _ancestors(paragraph, parents)):
            continue
        text = " ".join("".join(paragraph.itertext()).split())
        start = text.casefold().find(needle.casefold())
        if start < 0:
            continue
        all_matches += 1
        if len(passages) >= limit:
            continue
        quote_start = max(0, start - 80)
        quote_end = min(len(text), start + len(needle) + 80)
        quote = text[quote_start:quote_end]
        section = next((ancestor for ancestor in _ancestors(paragraph, parents) if ancestor.tag == "sec"), None)
        title = " ".join("".join(section.find("title").itertext()).split()) if section is not None and section.find("title") is not None else ""
        passages.append({
            "quote": quote,
            "quote_hash": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
            "locator": {"kind": "XML", "pmcid": value, "section": title,
                        "paragraph_index": paragraph_index, "char_start": quote_start, "char_end": quote_end},
            "verification": "QUOTE_MATCHED_IN_SOURCE_XML",
        })
    license_node = root.find(".//license")
    license_text = " ".join("".join(license_node.itertext()).split())[:500] if license_node is not None else None
    return {
        "schema_version": SCHEMA_VERSION, "status": "FOUND" if passages else "NOT_FOUND",
        "provider": "EUROPE_PMC_OA", "retrieved_at": _now(),
        "request": {"method": "GET", "url": url, "http_status": status_code},
        "data": {"pmcid": value, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                 "license_text": license_text, "matches": all_matches, "passages": passages},
        "answer_policy": "Quotes are verified XML substrings, not validated biological claims or PDF page locators.",
    }


def _request_xml(url: str, provider: str, *, maximum: int, not_found_ok: bool = False) -> tuple[bytes, int]:
    request = urllib.request.Request(
        url, headers={"Accept": "application/xml", "User-Agent": f"Yuxi-gene-authority/{SERVER_VERSION}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
            raw = response.read(maximum + 1)
            status_code = int(response.status)
    except urllib.error.HTTPError as error:
        if not_found_ok and error.code == 404:
            return b"", 404
        raise AuthorityError(f"{provider} request failed: HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise AuthorityError(f"{provider} request failed: {type(error).__name__}") from error
    if len(raw) > maximum:
        raise AuthorityError(f"{provider} XML exceeded the {maximum} byte limit")
    return raw, status_code


def _ancestors(node: ElementTree.Element, parents: dict[ElementTree.Element, ElementTree.Element]):
    while node in parents:
        node = parents[node]
        yield node


def verify_genomic_interval(start: int, end: int, coordinate_system: str = "one_based_inclusive") -> dict[str, Any]:
    start_value = _bounded_int("start", start, 0, 2**63 - 1)
    end_value = _bounded_int("end", end, 0, 2**63 - 1)
    if end_value < start_value:
        raise AuthorityError("end must be greater than or equal to start")
    if coordinate_system == "one_based_inclusive":
        if start_value < 1:
            raise AuthorityError("one-based coordinates must start at 1 or greater")
        length = end_value - start_value + 1
        formula = "end - start + 1"
    elif coordinate_system == "zero_based_half_open":
        length = end_value - start_value
        formula = "end - start"
    else:
        raise AuthorityError("coordinate_system must be one_based_inclusive or zero_based_half_open")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "VERIFIED",
        "provider": "YUXI_DETERMINISTIC_CALCULATOR",
        "retrieved_at": _now(),
        "data": {
            "start": start_value,
            "end": end_value,
            "coordinate_system": coordinate_system,
            "length": length,
            "formula": formula,
        },
        "answer_policy": "Length is deterministic; cite this tool instead of mental arithmetic.",
    }


def _bounded_number(name: str, value: float, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AuthorityError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        raise AuthorityError(f"{name} must be a finite number between {int(low)} and {int(high)}")
    return number


def compute_delta(minuend: float, subtrahend: float, label: str | None = None) -> dict[str, Any]:
    """Deterministic difference for published derived values (e.g. coordinate deltas).

    坐标差、计数差等派生值必须经此工具计算后引用，不得由模型心算——
    区间长度语义（end-start+1）与差值语义（minuend-subtrahend）不同，
    不得互相借用。
    """
    left = _bounded_number("minuend", minuend, -(2**53), 2**53)
    right = _bounded_number("subtrahend", subtrahend, -(2**53), 2**53)
    difference = left - right
    if not math.isfinite(difference):
        raise AuthorityError("difference is not finite")
    note = _bounded_text("label", label, 100) if label else None
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "VERIFIED",
        "provider": "YUXI_DETERMINISTIC_CALCULATOR",
        "retrieved_at": _now(),
        "data": {
            "minuend": left,
            "subtrahend": right,
            "delta": int(difference) if difference.is_integer() else difference,
            "abs_delta": abs(difference),
            "formula": "minuend - subtrahend",
            **({"label": note} if note else {}),
        },
        "answer_policy": "The delta is deterministic; cite this tool for every published difference.",
    }


def build_server():
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

    server = FastMCP(
        "gene-authority",
        instructions=(
            "Authoritative, read-only gene/protein/literature access. Use exact identifiers and taxa. "
            "Never turn NOT_FOUND into a biological conclusion. Every published fact must use the "
            "Yuxi MCP fact marker returned by the host; use verify_genomic_interval for arithmetic."
        ),
    )

    def guarded(function, **kwargs):
        try:
            return function(**kwargs)
        except (AuthorityError, json.JSONDecodeError, OSError) as error:
            raise ToolError(str(error)) from error

    @server.tool(name="ncbi_datasets_gene_report_rest", description="Official NCBI Datasets v2 REST gene data report. Symbol queries require taxon. No inference on empty results.")
    def _ncbi_rest(identifiers: list[str], identifier_type: str = "gene-id", taxon: str | None = None, page_size: int = 20) -> dict[str, Any]:
        return guarded(ncbi_gene_report_rest, identifiers=identifiers, identifier_type=identifier_type, taxon=taxon, page_size=page_size)

    @server.tool(name="ncbi_datasets_gene_summary_cli", description="Pinned official NCBI Datasets CLI metadata summary with allowlisted arguments only.")
    def _ncbi_cli_summary(identifiers: list[str], identifier_type: str = "gene-id", taxon: str | None = None) -> dict[str, Any]:
        return guarded(ncbi_gene_summary_cli, identifiers=identifiers, identifier_type=identifier_type, taxon=taxon)

    @server.tool(name="ncbi_datasets_gene_package_cli", description="Pinned official NCBI Datasets CLI gene package download into the current governed workspace; returns path, byte count and SHA-256.")
    def _ncbi_cli_download(identifiers: list[str], identifier_type: str = "gene-id", taxon: str | None = None, include: list[str] | None = None) -> dict[str, Any]:
        return guarded(ncbi_gene_package_cli, identifiers=identifiers, identifier_type=identifier_type, taxon=taxon, include=include)

    @server.tool(name="uniprot_entry_rest", description="Official UniProt REST entry JSON by accession.")
    def _uniprot_entry(accession: str) -> dict[str, Any]:
        return guarded(uniprot_entry_rest, accession=accession)

    @server.tool(name="uniprot_search_rest", description="Bounded official UniProtKB REST search. Query syntax is UniProt's documented syntax.")
    def _uniprot_search(query: str, size: int = 10, reviewed_only: bool = False) -> dict[str, Any]:
        return guarded(uniprot_search_rest, query=query, size=size, reviewed_only=reviewed_only)

    @server.tool(name="europe_pmc_search_rest", description="Official Europe PMC core-metadata search; returns bibliographic records, not proof of article claims.")
    def _epmc_search(query: str, page_size: int = 10) -> dict[str, Any]:
        return guarded(europe_pmc_search_rest, query=query, page_size=page_size)

    @server.tool(name="europe_pmc_article_rest", description="Official Europe PMC exact external-id/source metadata lookup.")
    def _epmc_article(source: str, external_id: str) -> dict[str, Any]:
        return guarded(europe_pmc_article_rest, source=source, external_id=external_id)

    @server.tool(name="ncbi_eutils_search_rest", description="NCBI Entrez ESearch across allowlisted databases. Returns candidate IDs, not dataset or article claims.")
    def _entrez_search(database: str, term: str, retmax: int = 10) -> dict[str, Any]:
        return guarded(ncbi_eutils_search_rest, database=database, term=term, retmax=retmax)

    @server.tool(name="ncbi_eutils_summary_rest", description="NCBI Entrez ESummary for exact numeric IDs in an allowlisted database.")
    def _entrez_summary(database: str, ids: list[str]) -> dict[str, Any]:
        return guarded(ncbi_eutils_summary_rest, database=database, ids=ids)

    @server.tool(name="ncbi_eutils_fetch_rest", description="NCBI Entrez EFetch bounded XML for exact numeric IDs in an allowlisted database.")
    def _entrez_fetch(database: str, ids: list[str]) -> dict[str, Any]:
        return guarded(ncbi_eutils_fetch_rest, database=database, ids=ids)

    @server.tool(name="pride_search_projects_rest", description="Search candidate PRIDE projects; a keyword hit is not verified tissue or assay evidence.")
    def _pride_search(keyword: str, page_size: int = 10) -> dict[str, Any]:
        return guarded(pride_search_projects_rest, keyword=keyword, page_size=page_size)

    @server.tool(name="pride_project_rest", description="Official PRIDE project metadata by exact PXD accession.")
    def _pride_project(accession: str) -> dict[str, Any]:
        return guarded(pride_project_rest, accession=accession)

    @server.tool(name="pride_project_files_rest", description="Official PRIDE file metadata by exact PXD accession; no file download.")
    def _pride_files(accession: str, page_size: int = 20) -> dict[str, Any]:
        return guarded(pride_project_files_rest, accession=accession, page_size=page_size)

    @server.tool(name="europe_pmc_oa_passages_rest", description="Find exact query-matching passages in Europe PMC OA XML with XML-only locators; not a mechanism verifier.")
    def _epmc_passages(pmcid: str, query: str, max_hits: int = 5) -> dict[str, Any]:
        return guarded(europe_pmc_oa_passages_rest, pmcid=pmcid, query=query, max_hits=max_hits)

    @server.tool(name="verify_genomic_interval", description="Deterministically calculate interval length under an explicit coordinate convention. Use this for every published coordinate-derived length.")
    def _verify_interval(start: int, end: int, coordinate_system: str = "one_based_inclusive") -> dict[str, Any]:
        return guarded(verify_genomic_interval, start=start, end=end, coordinate_system=coordinate_system)

    @server.tool(name="compute_delta", description="Deterministically calculate a difference (e.g. coordinate delta between two sources). Use this for every published derived difference; never mental arithmetic, never interval-length semantics.")
    def _compute_delta(minuend: float, subtrahend: float, label: str | None = None) -> dict[str, Any]:
        return guarded(compute_delta, minuend=minuend, subtrahend=subtrahend, label=label)

    return server


def main() -> int:
    if sys.argv[1:] == ["--check"]:
        print(json.dumps({"ok": True, "server": f"gene-authority/{SERVER_VERSION}"}))
        return 0
    if sys.argv[1:]:
        print("gene-authority-mcp accepts no command-line arguments", file=sys.stderr)
        return 2
    build_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
