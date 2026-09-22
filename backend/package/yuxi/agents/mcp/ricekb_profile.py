"""Deterministic rice gene profile assembler (builtin MCP "ricekb-profile").

一次调用返回规范化基因档案小对象：服务端顺序执行 resolve → entity → compare →
support → evidence → references，把 97+ 行的原始 envelope 去重归并为
identity / names / locations / transcripts / xrefs / annotations / support /
references / evidence_refs，并由确定性 QC 规则计算派生值（跨源坐标差、区间
长度、证据行计数）。模型只解释该对象，不再装配数据。

数据契约与 vendored ``ricekb_mcp.py`` 同源（rice-source-envelope-v1.1，
gateway 2.2.x），但本文件是 Yuxi 自有代码：不 import vendored 模块、不修改
其字节一致性。坐标一律 1-based 含端点；QC 派生值已算好，禁止模型心算。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

SERVER_VERSION = "1.0.0"
SCHEMA_VERSION = "ricekb-gene-profile.v1"
DEFAULT_GATEWAY_URL = "http://rice-kb-gateway:8080"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,100}$")
MAX_SYNONYMS = 8
MAX_REFERENCES = 8
MAX_EVIDENCE_REFS = 24
MAX_XREF_EXAMPLES = 5
MAX_AMBIGUOUS_CANDIDATES = 8
MACHINE_STATES = ("FOUND", "PARTIAL", "CONFLICT", "NO_EVIDENCE", "NOT_FOUND", "AMBIGUOUS", "INVALID_IDENTIFIER")
_COORDINATE_SYSTEM = "one_based_inclusive"

HttpResponse = tuple[int, bytes]
Opener = Callable[[urllib.request.Request, float], HttpResponse]


def _urllib_opener(request: urllib.request.Request, timeout: float) -> HttpResponse:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - first-party URL from env
            return int(response.status), response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        return int(error.code), error.read(MAX_RESPONSE_BYTES + 1)


class GatewayError(RuntimeError):
    pass


class _GatewayClient:
    def __init__(self, base_url: str, token: str, timeout: float, opener: Opener = _urllib_opener):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise GatewayError(f"RICE_KB_GATEWAY_URL 必须是绝对 http(s) URL，收到 {base_url!r}")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._opener = opener

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self._base_url}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
        )
        try:
            status, body = self._opener(request, self._timeout)
        except urllib.error.URLError as error:
            raise GatewayError(f"gateway 不可达 {self._base_url}: {error.reason}") from error
        except TimeoutError as error:
            raise GatewayError(f"gateway 超时（{self._timeout:.0f}s）") from error
        if len(body) > MAX_RESPONSE_BYTES:
            raise GatewayError("gateway 响应超过 8 MiB 安全上限")
        if status == 401:
            raise GatewayError("gateway 拒绝 API token")
        if status == 404:
            raise GatewayError("gateway 端点不存在（404）")
        if status >= 400:
            raise GatewayError(f"gateway 拒绝请求（HTTP {status}）")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GatewayError(f"gateway 返回非 JSON（HTTP {status}）") from error
        if not isinstance(payload, dict):
            raise GatewayError("gateway envelope 必须是 JSON 对象")
        return payload


def _client_from_env() -> _GatewayClient:
    token = os.environ.get("RICE_KB_API_TOKEN", "").strip()
    if not token:
        raise GatewayError("缺少 RICE_KB_API_TOKEN")
    base_url = (os.environ.get("RICE_KB_GATEWAY_URL", "").strip() or DEFAULT_GATEWAY_URL).strip()
    timeout_raw = os.environ.get("RICE_KB_PROFILE_TIMEOUT_SECONDS", "").strip()
    try:
        timeout = float(timeout_raw) if timeout_raw else 20.0
    except ValueError as error:
        raise GatewayError("RICE_KB_PROFILE_TIMEOUT_SECONDS 必须是数值") from error
    return _GatewayClient(base_url, token, timeout)


def _truncate(text: Any, maximum: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= maximum else value[: maximum - 1] + "…"


def _dedupe(values: list[str], cap: int) -> list[str]:
    return list(OrderedDict.fromkeys(v for v in (str(item).strip() for item in values) if v))[:cap]


def _location_from_msu(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        start = int(row.get("start"))
        end = int(row.get("stop"))
    except (TypeError, ValueError):
        return None
    return {
        "source": "MSU",
        "chromosome": str(row.get("chr") or ""),
        "locus": str(row.get("locus") or ""),
        "start": start,
        "end": end,
        "strand": str(row.get("ori") or ""),
        "coordinate_system": _COORDINATE_SYSTEM,
        "source_ref": f"source_msu.msu_loci#row={row.get('source_row_number')}",
    }


def _location_from_rap(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        start = int(row.get("start_position"))
        end = int(row.get("end_position"))
    except (TypeError, ValueError):
        return None
    return {
        "source": "RAP_DB",
        "chromosome": str(row.get("seqid") or ""),
        "locus": str(row.get("locus_id") or ""),
        "start": start,
        "end": end,
        "strand": str(row.get("strand") or ""),
        "coordinate_system": _COORDINATE_SYSTEM,
        "source_ref": f"source_rapdb.rapdb_loci#line={row.get('source_line_number')}",
    }


def _group_xrefs(identifier_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in identifier_rows:
        key = str(row.get("identifier_type") or row.get("namespace") or "OTHER")
        bucket = grouped.setdefault(key, {"count": 0, "examples": [], "provenance_id": None})
        bucket["count"] += 1
        if len(bucket["examples"]) < MAX_XREF_EXAMPLES:
            bucket["examples"].append(str(row.get("identifier") or ""))
        if not bucket["provenance_id"] and row.get("provenance_id"):
            bucket["provenance_id"] = str(row["provenance_id"])
    return grouped


def _parse_reference(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "pubmed_id": str(row.get("pubmedid") or "") or None,
        "year": str(row.get("year") or "") or None,
        "journal": _truncate(row.get("journal"), 80) or None,
        "title": _truncate(row.get("title"), 120) or None,
        "source_ref": f"source_oryzabase.references#row={row.get('source_row_number')}",
    }


def _build_qc_records(locations: list[dict[str, Any]], evidence_ref_total: int) -> list[dict[str, Any]]:
    """确定性派生值：坐标差、区间长度、证据行计数。服务器算好，模型只引用。"""
    records: list[dict[str, Any]] = []
    for location in locations:
        records.append(
            {
                "rule": "interval_length_v1",
                "source": location["source"],
                "source_ref": location["source_ref"],
                "value": location["end"] - location["start"] + 1,
                "unit": "bp",
                "formula": "end - start + 1 (one_based_inclusive)",
            }
        )
    by_source = {item["source"]: item for item in locations}
    msu, rap = by_source.get("MSU"), by_source.get("RAP_DB")
    if msu and rap:
        records.append(
            {
                "rule": "coordinate_start_delta_v1",
                "left": msu["source_ref"],
                "right": rap["source_ref"],
                "value": msu["start"] - rap["start"],
                "unit": "bp",
                "formula": "MSU.start - RAP_DB.start",
            }
        )
        records.append(
            {
                "rule": "coordinate_end_delta_v1",
                "left": msu["source_ref"],
                "right": rap["source_ref"],
                "value": msu["end"] - rap["end"],
                "unit": "bp",
                "formula": "MSU.end - RAP_DB.end",
            }
        )
    records.append(
        {
            "rule": "distinct_evidence_rows_v1",
            "value": evidence_ref_total,
            "unit": "rows",
        }
    )
    return records


def _machine_status(envelope: dict[str, Any]) -> str:
    status = str(envelope.get("status") or "").upper()
    return status if status in MACHINE_STATES else "FOUND"


def assemble_gene_profile(client: _GatewayClient, identifier: str) -> dict[str, Any]:
    query = str(identifier or "").strip()
    if not IDENTIFIER_RE.fullmatch(query):
        raise GatewayError("identifier 含非法字符或超长")
    resolve = client.get("/v1/entities/resolve", {"q": query, "limit": 5})
    candidates = resolve.get("data") if isinstance(resolve.get("data"), list) else []
    if not candidates:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "NOT_FOUND",
            "query": query,
            "answer_policy": "NOT_FOUND 表示当前 34 张源快照无法解析该标识符；不得改写成生物学不存在。",
        }
    if len(candidates) > 1:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "AMBIGUOUS",
            "query": query,
            "candidates": [
                {k: item.get(k) for k in ("entity_key", "canonical_rap_id", "source_databases")}
                for item in candidates[:MAX_AMBIGUOUS_CANDIDATES]
            ],
            "answer_policy": "多候选必须列出并请用户选择，禁止替用户挑选。",
        }

    entity_key = str(candidates[0].get("entity_key") or query)
    entity = client.get(f"/v1/entities/{urllib.parse.quote(entity_key, safe=':._-')}")
    compare = client.get(f"/v1/entities/{urllib.parse.quote(entity_key, safe=':._-')}/compare")
    support = client.get(f"/v1/entities/{urllib.parse.quote(entity_key, safe=':._-')}/support")
    evidence = client.get(f"/v1/entities/{urllib.parse.quote(entity_key, safe=':._-')}/evidence", {"limit": 200})
    references = client.get(f"/v1/entities/{urllib.parse.quote(entity_key, safe=':._-')}/references")

    entity_data = entity.get("data") if isinstance(entity.get("data"), dict) else {}
    compare_data = compare.get("data") if isinstance(compare.get("data"), dict) else {}
    support_data = support.get("data") if isinstance(support.get("data"), dict) else {}
    evidence_rows = evidence.get("data") if isinstance(evidence.get("data"), list) else []
    reference_rows = references.get("data") if isinstance(references.get("data"), list) else []
    source_records = entity_data.get("source_records") if isinstance(entity_data.get("source_records"), dict) else {}
    compare_sources = compare_data.get("sources") if isinstance(compare_data.get("sources"), dict) else {}

    resolve_entity = (resolve.get("entity") if isinstance(resolve.get("entity"), dict) else {}) or {}

    names: dict[str, dict[str, Any]] = {}
    for source, rows in compare_sources.items():
        row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
        if not row:
            continue
        names[str(source)] = {
            "cgsnl_symbol": row.get("cgsnl_gene_symbol") or None,
            "cgsnl_name": _truncate(row.get("cgsnl_gene_name"), 120) or None,
            "synonyms": _dedupe(
                str(row.get("rap_db_gene_symbol_synonym_s") or "").replace(",", " ").split()
                + str(row.get("oryzabase_gene_symbol_synonym_s") or "").replace(",", " ").split(),
                MAX_SYNONYMS,
            ),
        }

    locations: list[dict[str, Any]] = []
    msu_rows = source_records.get("msu_loci") if isinstance(source_records.get("msu_loci"), list) else []
    representative = next((r for r in msu_rows if isinstance(r, dict) and r.get("is_representative")), None)
    for candidate in [representative] if representative else msu_rows[:1]:
        if isinstance(candidate, dict):
            location = _location_from_msu(candidate)
            if location:
                locations.append(location)
    rap_rows = source_records.get("rapdb_loci") if isinstance(source_records.get("rapdb_loci"), list) else []
    rap_gene = next(
        (r for r in rap_rows if isinstance(r, dict) and str(r.get("feature_type") or "").lower() == "gene"),
        None,
    )
    if rap_gene is None and rap_rows:
        first = rap_rows[0]
        rap_gene = first if isinstance(first, dict) else None
    if isinstance(rap_gene, dict):
        location = _location_from_rap(rap_gene)
        if location:
            locations.append(location)

    crosswalk = source_records.get("rap_msu_crosswalk")
    crosswalk = crosswalk if isinstance(crosswalk, list) else []
    rap_annotations = source_records.get("rapdb_annotations")
    rap_annotations = rap_annotations if isinstance(rap_annotations, list) else []
    transcripts = {
        "msu_transcripts": _dedupe([r.get("msu_transcript_id") for r in crosswalk if isinstance(r, dict)], 6),
        "rap_transcripts": _dedupe([r.get("transcript_id") for r in rap_annotations if isinstance(r, dict)], 6),
    }

    identifier_rows = entity_data.get("identifiers") if isinstance(entity_data.get("identifiers"), list) else []
    evidence_refs: list[str] = []
    for row in evidence_rows:
        if isinstance(row, dict) and row.get("provenance_id"):
            evidence_refs.append(str(row["provenance_id"]))
    evidence_refs = _dedupe(evidence_refs, MAX_EVIDENCE_REFS)

    rap_row = next(iter(compare_sources.get("RAP_DB") or []), None)
    rap_row = rap_row if isinstance(rap_row, dict) else {}
    support_rows = support_data.get("sources") if isinstance(support_data.get("sources"), list) else []

    data = {
        "identity": {
            "entity_key": entity_key,
            "canonical_rap_id": resolve_entity.get("canonical_rap_id"),
            "matched_identifiers": resolve_entity.get("matched_identifiers"),
            "matched_namespaces": resolve_entity.get("matched_namespaces"),
            "description": _truncate(rap_row.get("description"), 300) or None,
            "source_databases": resolve_entity.get("source_databases"),
            "source_database_count": resolve_entity.get("source_database_count"),
            "source_record_count": resolve_entity.get("source_record_count"),
            "source_coverage_score": resolve_entity.get("source_coverage_score"),
            "source_coverage_note": "source_coverage_score 只表示已配置源表的记录覆盖完整度，不是生物学置信度。",
        },
        "contract_notes": [
            "调控关系（regulators/targets）：当前 RiceKB 契约未提供该数据；"
            "本轮未执行该查询——不得写成任何工具返回 NO_EVIDENCE。",
            "source_coverage_score 只表示覆盖完整度，不是生物学置信度，禁止渲染成置信度/可信度。",
            "坐标差与区间长度只引用 qc 记录的 value，禁止自行计算或改写口径。",
        ],
        "names": names,
        "locations": locations,
        "transcripts": transcripts,
        "xrefs": _group_xrefs([r for r in identifier_rows if isinstance(r, dict)]),
        "annotations": {
            "go": _truncate(rap_row.get("go"), 600) or None,
            "interpro": _truncate(rap_row.get("interpro"), 400) or None,
            "msu_annotation": _truncate(
                next(iter((compare_data.get("source_specific") or {}).get("MSU_annotations") or []), ""), 240
            )
            or None,
        },
        "compare": {
            "agreements": (compare_data.get("agreements") or [])[:12],
            "conflicts": (compare_data.get("conflicts") or [])[:12],
            "missing_sources": compare_data.get("missing_sources") or [],
        },
        "support": [
            {
                "source_database": row.get("source_database"),
                "source_table": row.get("source_table"),
                "record_count": row.get("record_count"),
            }
            for row in support_rows
            if isinstance(row, dict)
        ][:20],
        "references": [_parse_reference(row) for row in reference_rows if isinstance(row, dict)][:MAX_REFERENCES],
        "evidence_refs": {"total": len(evidence_rows), "provenance_ids": evidence_refs},
    }
    qc = _build_qc_records(locations, len(evidence_rows))
    conflicts = data["compare"]["conflicts"]
    status = "CONFLICT" if conflicts else _machine_status(resolve)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "query": query,
        "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "data": data,
        "qc": qc,
        "answer_policy": (
            "所有字段与 qc 派生值均可直接引用（坐标 1-based 含端点）。渲染要求：必须把 data.locations 的"
            "每一行（每个来源一行）都列进坐标表，并单列「跨源坐标差与区间长度」小节按规则名引用 qc 记录"
            "（coordinate_start_delta_v1 / coordinate_end_delta_v1 / interval_length_v1）的 value；"
            "contract_notes 原样引用。conflicts 只分来源呈现，不得静默消解；"
            "references 仅为书目元数据，不构成正文命题证据。"
        ),
    }


def build_server():
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

    server = FastMCP(
        "ricekb-profile",
        instructions=(
            "Deterministic rice gene profile assembler. One call returns the normalized "
            "identity/names/locations/transcripts/xrefs/annotations/support/references view "
            "plus server-computed qc derivations (coordinate deltas, interval lengths). "
            "Cite qc values instead of computing; NOT_FOUND/AMBIGUOUS/CONFLICT must be "
            "disclosed verbatim."
        ),
    )

    @server.tool(
        name="ricekb_gene_profile",
        description=(
            "Single-call deterministic rice gene profile: resolve→entity→compare→support→evidence→references "
            "assembled server-side into one normalized object with row-level provenance refs and qc derivations "
            "(coordinate_start_delta_v1 / coordinate_end_delta_v1 / interval_length_v1). Prefer this over chained "
            "ricekb_* calls for gene-profile questions."
        ),
    )
    def _gene_profile(identifier: str) -> dict[str, Any]:
        try:
            return assemble_gene_profile(_client_from_env(), identifier)
        except GatewayError as error:
            raise ToolError(str(error)) from error

    return server


def main() -> int:
    if sys.argv[1:] == ["--check"]:
        client = _client_from_env()
        snapshot = client.get("/v1/meta/snapshot")
        print(
            json.dumps(
                {
                    "ok": True,
                    "server": f"ricekb-profile/{SERVER_VERSION}",
                    "snapshot_status": _machine_status(snapshot),
                }
            )
        )
        return 0
    if sys.argv[1:]:
        print("ricekb-profile-mcp 不接受命令行参数", file=sys.stderr)
        return 2
    build_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
