#!/usr/bin/env python3
"""ricekb-mcp: stdio MCP server over the Rice Source Knowledge Base Gateway.

Canonical implementation lives in the Rice Research Agent repository at
``rice-kb-gateway/mcp/ricekb_mcp.py``. Consuming platforms (Yuxi) vendor this
file unchanged; the gateway contract it speaks is ``rice-source-envelope-v1.1``
served by gateway 2.2.x.

The fifteen tools mirror the QM ``ricekb`` CLI one-to-one so both agent platforms
answer from the same 34 lossless MSU / Oryzabase / RAP-DB source tables with
the same machine states. ``ricekb_sequence``, ``ricekb_region`` and
``ricekb_genome`` additionally expose the exact source FASTA and GFF/GFF3
records (sequence bases, sequence_sha256, 1-based inclusive coordinates) that
the row-oriented tools deliberately omit. Every tool returns the gateway
envelope verbatim (``status``, ``evidence``, row-level ``provenance``); nothing
is summarised, re-ranked, or invented here. Transport, authentication, and
parameter errors raise ``ToolError`` so the calling agent sees ``isError``
instead of a fake scientific state.

Runtime dependencies: the ``mcp`` package (>= 1.10, present in the Yuxi API
image) and the standard library. HTTP is done with ``urllib`` on purpose: the
gateway is a first-party service, and one fewer dependency is one fewer
supply-chain surface inside the agent runtime.

Environment:
  RICE_KB_GATEWAY_URL          default http://rice-kb-gateway:8080
  RICE_KB_API_TOKEN            required; the caller-specific gateway token
  RICE_KB_MCP_TIMEOUT_SECONDS  default 20 (gateway statement timeout is 8s)

Usage:
  ricekb-mcp            run the stdio server (what the MCP host launches)
  ricekb-mcp --check    verify gateway reachability and token, then exit
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MCP_SERVER_NAME = "ricekb"
MCP_SERVER_VERSION = "1.1.0"
GATEWAY_CONTRACT = "rice-source-envelope-v1.1"
DEFAULT_GATEWAY_URL = "http://rice-kb-gateway:8080"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

ANNOTATION_CATEGORIES = ("rap", "oryzabase", "msu", "go", "interpro", "pfam", "coexpression", "expression", "all")
SEARCH_KINDS = ("all", "identifier", "annotation", "reference")
SOURCE_DATABASES = ("MSU", "ORYZABASE", "RAP_DB")
CANDIDATE_SORTS = ("source_coverage_desc", "source_count_desc", "record_count_desc", "entity_key_asc")

# Exact source FASTA records. The gateway exposes only these (source, type)
# pairs: ORYZABASE has no sequence table and MSU has no protein FASTA table, so
# an unsupported pair is a caller error, not a missing source record.
SEQUENCE_TYPES: dict[str, tuple[str, ...]] = {
    "MSU": ("cds", "cdna"),
    "RAP_DB": ("cds", "gene", "protein", "transcript"),
}
REGION_SOURCES = ("MSU", "RAP_DB", "RAP_DB_TRANSCRIPT", "RAP_DB_EXON")
MAX_REGION_SPAN = 10_000_000
MAX_REGION_ROWS = 2_000
MAX_GENOME_SPAN = 100_000

TOOL_NAMES = (
    "ricekb_resolve",
    "ricekb_entity",
    "ricekb_compare",
    "ricekb_annotations",
    "ricekb_support",
    "ricekb_evidence",
    "ricekb_references",
    "ricekb_regulators",
    "ricekb_targets",
    "ricekb_candidates",
    "ricekb_search",
    "ricekb_source",
    "ricekb_sequence",
    "ricekb_region",
    "ricekb_genome",
)


class GatewayError(RuntimeError):
    """Transport, authentication, or parameter failure talking to the gateway."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


Opener = Callable[[urllib.request.Request, float], HttpResponse]


def _urllib_opener(request: urllib.request.Request, timeout: float) -> HttpResponse:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - first-party URL from env
            return HttpResponse(response.status, response.read(MAX_RESPONSE_BYTES + 1))
    except urllib.error.HTTPError as error:
        return HttpResponse(error.code, error.read(MAX_RESPONSE_BYTES + 1))


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Opener = _urllib_opener,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise GatewayError(f"RICE_KB_GATEWAY_URL must be an absolute http(s) URL, got {base_url!r}")
        if not token:
            raise GatewayError("RICE_KB_API_TOKEN is required")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._opener = opener

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self._base_url}{path}" + (f"?{query}" if query else "")
        return self._send(urllib.request.Request(url, method="GET", headers=self._headers()))

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        headers = self._headers() | {"Content-Type": "application/json"}
        return self._send(urllib.request.Request(f"{self._base_url}{path}", data=body, method="POST", headers=headers))

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": f"ricekb-mcp/{MCP_SERVER_VERSION}",
        }

    def _send(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            response = self._opener(request, self._timeout)
        except urllib.error.URLError as error:
            raise GatewayError(f"gateway unreachable at {self._base_url}: {error.reason}") from error
        except TimeoutError as error:
            raise GatewayError(f"gateway timed out after {self._timeout:.0f}s") from error
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise GatewayError("gateway response exceeded the 8 MiB safety limit")
        if response.status == 401:
            raise GatewayError("gateway rejected the API token; check RICE_KB_API_TOKEN for this caller")
        if response.status == 504:
            raise GatewayError("gateway database query timed out (8s statement timeout)")
        if response.status == 503:
            raise GatewayError("source knowledge database unavailable")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GatewayError(f"gateway returned non-JSON body (HTTP {response.status})") from error
        if response.status >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            raise GatewayError(f"gateway rejected the request (HTTP {response.status}): {detail}")
        if not isinstance(payload, dict):
            raise GatewayError("gateway envelope must be a JSON object")
        return payload


def client_from_env(environ: dict[str, str] | None = None, *, opener: Opener = _urllib_opener) -> GatewayClient:
    env = os.environ if environ is None else environ
    timeout_raw = env.get("RICE_KB_MCP_TIMEOUT_SECONDS", "").strip()
    try:
        timeout = float(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError as error:
        raise GatewayError("RICE_KB_MCP_TIMEOUT_SECONDS must be a number") from error
    if not 1.0 <= timeout <= 120.0:
        raise GatewayError("RICE_KB_MCP_TIMEOUT_SECONDS must be between 1 and 120")
    return GatewayClient(
        env.get("RICE_KB_GATEWAY_URL", DEFAULT_GATEWAY_URL).strip() or DEFAULT_GATEWAY_URL,
        env.get("RICE_KB_API_TOKEN", "").strip(),
        timeout=timeout,
        opener=opener,
    )


# --------------------------------------------------------------------------
# Tool implementations. Pure functions over a GatewayClient so they can be
# unit tested and reused by the regression runner without the MCP stack.
# --------------------------------------------------------------------------


def _identifier(value: str) -> str:
    text = (value or "").strip()
    if not text:
        raise GatewayError("identifier must not be empty")
    if len(text) > 200:
        raise GatewayError("identifier must not exceed 200 characters")
    return text


def _bounded(name: str, value: int, low: int, high: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise GatewayError(f"{name} must be an integer between {low} and {high}")
    return value


def _choice(name: str, value: str, allowed: tuple[str, ...]) -> str:
    text = (value or "").strip()
    if text not in allowed:
        raise GatewayError(f"{name} must be one of {', '.join(allowed)}")
    return text


def _chromosome(value: str) -> str:
    text = (value or "").strip()
    if not 1 <= len(text) <= 30 or not all(char.isalnum() or char in "_.-" for char in text):
        raise GatewayError("chromosome must be 1-30 characters of letters, digits, underscore, dot or hyphen")
    return text


def tool_resolve(client: GatewayClient, query: str, limit: int = 20) -> dict[str, Any]:
    return client.get("/v1/entities/resolve", {"q": _identifier(query), "limit": _bounded("limit", limit, 1, 100)})


def tool_entity(client: GatewayClient, identifier: str) -> dict[str, Any]:
    return client.get(f"/v1/entities/{urllib.parse.quote(_identifier(identifier), safe='')}")


def tool_compare(client: GatewayClient, identifier: str, limit: int = 100) -> dict[str, Any]:
    return client.get(
        f"/v1/entities/{urllib.parse.quote(_identifier(identifier), safe='')}/compare",
        {"limit": _bounded("limit", limit, 1, 500)},
    )


def tool_annotations(client: GatewayClient, identifier: str, category: str = "all", limit: int = 100) -> dict[str, Any]:
    return client.get(
        f"/v1/entities/{urllib.parse.quote(_identifier(identifier), safe='')}/annotations",
        {"category": _choice("category", category, ANNOTATION_CATEGORIES), "limit": _bounded("limit", limit, 1, 500)},
    )


def tool_support(client: GatewayClient, identifier: str) -> dict[str, Any]:
    return client.get(f"/v1/entities/{urllib.parse.quote(_identifier(identifier), safe='')}/support")


def tool_evidence(client: GatewayClient, identifier: str, limit: int = 100) -> dict[str, Any]:
    return client.get(
        f"/v1/entities/{urllib.parse.quote(_identifier(identifier), safe='')}/evidence",
        {"limit": _bounded("limit", limit, 1, 500)},
    )


def tool_regulators(client: GatewayClient, identifier: str) -> dict[str, Any]:
    return client.get(f"/v1/entities/{urllib.parse.quote(_identifier(identifier), safe='')}/regulators")


def tool_targets(client: GatewayClient, identifier: str) -> dict[str, Any]:
    return client.get(f"/v1/entities/{urllib.parse.quote(_identifier(identifier), safe='')}/targets")


def tool_references(client: GatewayClient, identifier: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    return client.get(
        f"/v1/entities/{urllib.parse.quote(_identifier(identifier), safe='')}/references",
        {"limit": _bounded("limit", limit, 1, 200), "offset": _bounded("offset", offset, 0, 100000)},
    )


def tool_candidates(
    client: GatewayClient,
    min_source_databases: int = 1,
    require_sources: list[str] | None = None,
    canonical_rap_only: bool = True,
    min_source_records: int = 1,
    sort: list[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    sources = [_choice("require_sources", item, SOURCE_DATABASES) for item in (require_sources or [])]
    sorts = [_choice("sort", item, CANDIDATE_SORTS) for item in (sort or ["source_coverage_desc"])]
    payload = {
        "filters": {
            "min_source_databases": _bounded("min_source_databases", min_source_databases, 1, 3),
            "require_sources": sources,
            "canonical_rap_only": bool(canonical_rap_only),
            "min_source_records": _bounded("min_source_records", min_source_records, 1, 100000),
        },
        "sort": sorts,
        "limit": _bounded("limit", limit, 1, 100),
        "offset": _bounded("offset", offset, 0, 100000),
    }
    return client.post("/v1/query/candidates", payload)


def tool_search(client: GatewayClient, query: str, kind: str = "all", limit: int = 20) -> dict[str, Any]:
    text = (query or "").strip()
    if not 2 <= len(text) <= 300:
        raise GatewayError("query must be between 2 and 300 characters")
    return client.get(
        "/v1/search",
        {"q": text, "kind": _choice("kind", kind, SEARCH_KINDS), "limit": _bounded("limit", limit, 1, 100)},
    )


def tool_source(
    client: GatewayClient,
    source: str,
    table: str,
    row: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    source_name = _choice("source", (source or "").upper(), SOURCE_DATABASES)
    table_name = (table or "").strip()
    if not table_name or len(table_name) > 128 or not table_name.replace("_", "").isalnum():
        raise GatewayError("table must be a source table name (letters, digits, underscore)")
    params: dict[str, Any] = {"limit": _bounded("limit", limit, 1, 100), "offset": _bounded("offset", offset, 0, 1000000)}
    if row is not None:
        params["row"] = _bounded("row", row, 1, 2**31 - 1)
    return client.get(
        f"/v1/source/{urllib.parse.quote(source_name, safe='')}/{urllib.parse.quote(table_name, safe='')}/records",
        params,
    )


def tool_sequence(client: GatewayClient, source: str, sequence_type: str, sequence_id: str) -> dict[str, Any]:
    """Exact FASTA record: bases plus sequence_sha256 for one source sequence id.

    MSU keys are transcript suffixes of the locus (`LOC_Os06g01210.1`); RAP-DB
    keys are transcript ids (`Os06t0101600-01`). A locus id without the suffix
    is a legitimate FOUND/NOT_FOUND lookup, never a silent rewrite.
    """
    source_name = _choice("source", (source or "").upper(), tuple(SEQUENCE_TYPES))
    types = SEQUENCE_TYPES[source_name]
    type_name = _choice("sequence_type", (sequence_type or "").lower(), types)
    return client.get(
        f"/v1/sequences/{urllib.parse.quote(source_name, safe='')}/"
        f"{urllib.parse.quote(type_name, safe='')}/"
        f"{urllib.parse.quote(_identifier(sequence_id), safe='')}"
    )


def tool_region(
    client: GatewayClient,
    source: str,
    chromosome: str,
    start: int,
    end: int,
    limit: int = 500,
) -> dict[str, Any]:
    """GFF/GFF3 features overlapping a 1-based inclusive interval."""
    source_name = _choice("source", (source or "").upper(), REGION_SOURCES)
    start_value = _bounded("start", start, 1, 2**31 - 1)
    end_value = _bounded("end", end, 1, 2**31 - 1)
    if end_value < start_value:
        raise GatewayError("end must be greater than or equal to start")
    if end_value - start_value + 1 > MAX_REGION_SPAN:
        raise GatewayError(f"region span must not exceed {MAX_REGION_SPAN} bp")
    return client.get(
        "/v1/regions",
        {
            "source": source_name,
            "chromosome": _chromosome(chromosome),
            "start": start_value,
            "end": end_value,
            "limit": _bounded("limit", limit, 1, MAX_REGION_ROWS),
        },
    )


def tool_genome(client: GatewayClient, chromosome: str, start: int, end: int) -> dict[str, Any]:
    """Exact genome slice (IRGSP-1.0 bases) for a 1-based inclusive interval."""
    start_value = _bounded("start", start, 1, 2**31 - 1)
    end_value = _bounded("end", end, 1, 2**31 - 1)
    if end_value < start_value:
        raise GatewayError("end must be greater than or equal to start")
    if end_value - start_value + 1 > MAX_GENOME_SPAN:
        raise GatewayError(f"genome slice must not exceed {MAX_GENOME_SPAN} bp")
    return client.get(
        f"/v1/genome/{urllib.parse.quote(_chromosome(chromosome), safe='')}",
        {"start": start_value, "end": end_value},
    )


TOOL_FUNCTIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "ricekb_resolve": tool_resolve,
    "ricekb_entity": tool_entity,
    "ricekb_compare": tool_compare,
    "ricekb_annotations": tool_annotations,
    "ricekb_support": tool_support,
    "ricekb_evidence": tool_evidence,
    "ricekb_references": tool_references,
    "ricekb_regulators": tool_regulators,
    "ricekb_targets": tool_targets,
    "ricekb_candidates": tool_candidates,
    "ricekb_search": tool_search,
    "ricekb_source": tool_source,
    "ricekb_sequence": tool_sequence,
    "ricekb_region": tool_region,
    "ricekb_genome": tool_genome,
}

_CONTRACT_NOTE = (
    " Returns the gateway envelope verbatim: `status` is one of FOUND, PARTIAL, CONFLICT, "
    "NO_EVIDENCE, NOT_FOUND, AMBIGUOUS, INVALID_IDENTIFIER; `provenance` lists source database, "
    "schema, table, row_ref, content_sha256 and import_run_id for every fact. Cite only those rows."
)


def build_server(client: GatewayClient):
    """Register the fifteen tools on a FastMCP server. Imports ``mcp`` lazily."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

    server = FastMCP(
        MCP_SERVER_NAME,
        instructions=(
            "Read-only access to the frozen Rice source knowledge base (34 lossless MSU, Oryzabase and "
            "RAP-DB tables). Resolve identifiers before asking for records; never guess identifiers; "
            "treat NO_EVIDENCE and NOT_FOUND as final answers, not as reasons to speculate."
        ),
    )

    def guarded(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        def run(**kwargs: Any) -> dict[str, Any]:
            try:
                return fn(client, **kwargs)
            except GatewayError as error:
                raise ToolError(str(error)) from error

        return run

    @server.tool(name="ricekb_resolve", description="Resolve a rice gene identifier or symbol (RAP Os..g.., MSU LOC_Os..g.., Oryzabase id, gene symbol) to source entities." + _CONTRACT_NOTE)
    def ricekb_resolve(query: str, limit: int = 20) -> dict[str, Any]:
        return guarded(tool_resolve)(query=query, limit=limit)

    @server.tool(name="ricekb_entity", description="Fetch the entity profile for one resolved identifier across all three source databases." + _CONTRACT_NOTE)
    def ricekb_entity(identifier: str) -> dict[str, Any]:
        return guarded(tool_entity)(identifier=identifier)

    @server.tool(name="ricekb_compare", description="Compare what MSU, Oryzabase and RAP-DB each record for one identifier; CONFLICT is reported, never auto-resolved." + _CONTRACT_NOTE)
    def ricekb_compare(identifier: str, limit: int = 100) -> dict[str, Any]:
        return guarded(tool_compare)(identifier=identifier, limit=limit)

    @server.tool(name="ricekb_annotations", description="Source annotations for one identifier. category: rap, oryzabase, msu, go, interpro, pfam, coexpression, expression, all." + _CONTRACT_NOTE)
    def ricekb_annotations(identifier: str, category: str = "all", limit: int = 100) -> dict[str, Any]:
        return guarded(tool_annotations)(identifier=identifier, category=category, limit=limit)

    @server.tool(name="ricekb_support", description="Which source databases and tables hold records for one identifier, with per-table record counts." + _CONTRACT_NOTE)
    def ricekb_support(identifier: str) -> dict[str, Any]:
        return guarded(tool_support)(identifier=identifier)

    @server.tool(name="ricekb_evidence", description="Row-level source records supporting one identifier across all three databases; the material to cite." + _CONTRACT_NOTE)
    def ricekb_evidence(identifier: str, limit: int = 100) -> dict[str, Any]:
        return guarded(tool_evidence)(identifier=identifier, limit=limit)

    @server.tool(name="ricekb_regulators", description="Upstream regulators of one identifier. The 34 source tables define no curated regulator contract, so a resolved entity always yields NO_EVIDENCE; answer exactly that." + _CONTRACT_NOTE)
    def ricekb_regulators(identifier: str) -> dict[str, Any]:
        return guarded(tool_regulators)(identifier=identifier)

    @server.tool(name="ricekb_targets", description="Downstream targets of one identifier. The 34 source tables define no curated target contract, so a resolved entity always yields NO_EVIDENCE; answer exactly that." + _CONTRACT_NOTE)
    def ricekb_targets(identifier: str) -> dict[str, Any]:
        return guarded(tool_targets)(identifier=identifier)

    @server.tool(name="ricekb_references", description="Oryzabase literature references linked to one identifier's symbols." + _CONTRACT_NOTE)
    def ricekb_references(identifier: str, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return guarded(tool_references)(identifier=identifier, limit=limit, offset=offset)

    @server.tool(name="ricekb_candidates", description="List entities by source coverage. require_sources subset of MSU, ORYZABASE, RAP_DB. source_coverage_score is coverage, not biological confidence." + _CONTRACT_NOTE)
    def ricekb_candidates(
        min_source_databases: int = 1,
        require_sources: list[str] | None = None,
        canonical_rap_only: bool = True,
        min_source_records: int = 1,
        sort: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return guarded(tool_candidates)(
            min_source_databases=min_source_databases,
            require_sources=require_sources,
            canonical_rap_only=canonical_rap_only,
            min_source_records=min_source_records,
            sort=sort,
            limit=limit,
            offset=offset,
        )

    @server.tool(name="ricekb_search", description="Full-text search over source identifiers, annotations and references. kind: all, identifier, annotation, reference." + _CONTRACT_NOTE)
    def ricekb_search(query: str, kind: str = "all", limit: int = 20) -> dict[str, Any]:
        return guarded(tool_search)(query=query, kind=kind, limit=limit)

    @server.tool(name="ricekb_source", description="Raw rows from one source table (source: MSU, ORYZABASE, RAP_DB). FASTA bodies are omitted by the gateway." + _CONTRACT_NOTE)
    def ricekb_source(source: str, table: str, row: int | None = None, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        return guarded(tool_source)(source=source, table=table, row=row, limit=limit, offset=offset)

    @server.tool(name="ricekb_sequence", description="Exact source FASTA record: sequence bases, sequence_length and sequence_sha256 for one sequence id. sequence_type pairs: MSU cds|cdna, RAP_DB cds|gene|protein|transcript. MSU keys are transcript ids such as LOC_Os06g01210.1; RAP-DB keys are transcript ids such as Os06t0101600-01." + _CONTRACT_NOTE)
    def ricekb_sequence(source: str, sequence_type: str, sequence_id: str) -> dict[str, Any]:
        return guarded(tool_sequence)(source=source, sequence_type=sequence_type, sequence_id=sequence_id)

    @server.tool(name="ricekb_region", description="GFF/GFF3 features overlapping a 1-based inclusive interval (source: MSU, RAP_DB, RAP_DB_TRANSCRIPT, RAP_DB_EXON)." + _CONTRACT_NOTE)
    def ricekb_region(source: str, chromosome: str, start: int, end: int, limit: int = 500) -> dict[str, Any]:
        return guarded(tool_region)(source=source, chromosome=chromosome, start=start, end=end, limit=limit)

    @server.tool(name="ricekb_genome", description="Exact IRGSP-1.0 genome bases for a 1-based inclusive interval of at most 100,000 bp; returns sequence and sequence_sha256." + _CONTRACT_NOTE)
    def ricekb_genome(chromosome: str, start: int, end: int) -> dict[str, Any]:
        return guarded(tool_genome)(chromosome=chromosome, start=start, end=end)

    return server


def check(client: GatewayClient) -> dict[str, Any]:
    """Operator preflight: authenticated snapshot read proves URL, token and DB."""
    snapshot = client.get("/v1/meta/snapshot")
    return {
        "ok": True,
        "server": f"{MCP_SERVER_NAME}/{MCP_SERVER_VERSION}",
        "contract": GATEWAY_CONTRACT,
        "snapshot": snapshot,
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        client = client_from_env()
        if args == ["--check"]:
            print(json.dumps(check(client), ensure_ascii=False, indent=2))
            return 0
        if args:
            print(__doc__, file=sys.stderr)
            return 2
        build_server(client).run(transport="stdio")
        return 0
    except GatewayError as error:
        print(f"ricekb-mcp: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
