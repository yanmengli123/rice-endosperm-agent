"""Deterministic projections for official authority records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from yuxi.knowledge.planning.turn_execution_plan import source_use_execution_succeeded

NCBI_REPORT_TOOL = "ncbi_datasets_gene_report_rest"


@dataclass(frozen=True)
class AuthorityProjection:
    kind: str
    audit_id: int
    blocks: str
    used_fact_ids: tuple[str, ...]
    coverage: dict[str, Any]


def _value(item: Any, field: str) -> Any:
    return item.get(field) if isinstance(item, dict) else getattr(item, field, None)


def _fact_value(fact: dict) -> str | int | float | None:
    if "numeric_value" in fact:
        return fact["numeric_value"]
    return fact.get("string_value")


def _manifest_facts(use: Any) -> list[dict]:
    provenance = _value(use, "provenance") or {}
    return list((provenance.get("fact_manifest") or {}).get("facts") or []) if isinstance(provenance, dict) else []


def _is_adopted_report(use: Any) -> bool:
    return bool(
        _value(use, "adopted")
        and source_use_execution_succeeded(use)
        and _value(use, "operation") == NCBI_REPORT_TOOL
        and _value(use, "provider_id") == "gene-authority"
    )


def _facts_by_path(facts: list[dict]) -> dict[str, dict]:
    return {str(fact.get("path") or ""): fact for fact in facts if _fact_value(fact) is not None}


def _marker(audit_id: int, fact: dict) -> str:
    return f"[MCP-F:{audit_id}:{fact.get('id')}]"


def project_ncbi_official_links(source_uses: list[Any] | None) -> list[AuthorityProjection]:
    projections: list[AuthorityProjection] = []
    for use in list(source_uses or []):
        if not _is_adopted_report(use):
            continue
        provenance = _value(use, "provenance") or {}
        audit_id = int(provenance.get("mcp_call_audit_id"))
        facts = _manifest_facts(use)
        by_path = _facts_by_path(facts)
        report_indexes = sorted(
            {
                path.split("/")[3]
                for path in by_path
                if path.startswith("/data/reports/") and len(path.split("/")) > 5 and path.endswith("/gene/gene_id")
            },
            key=lambda value: int(value),
        )
        for index in report_indexes:
            prefix = f"/data/reports/{index}/gene"
            gene_id = by_path.get(f"{prefix}/gene_id")
            if gene_id is None:
                continue
            gene_id_value = str(_fact_value(gene_id) or "")
            if not gene_id_value.isdigit():
                continue
            used = [gene_id]
            rows = [f"| NCBI Gene ID | `{gene_id_value}` | {_marker(audit_id, gene_id)} |"]
            for field, label in (("symbol", "NCBI 符号"), ("description", "描述"), ("taxname", "物种")):
                fact = by_path.get(f"{prefix}/{field}")
                if fact is not None:
                    value = str(_fact_value(fact) or "")
                    rows.append(f"| {label} | {value} | {_marker(audit_id, fact)} |")
                    used.append(fact)
            gene_url = f"https://www.ncbi.nlm.nih.gov/gene/{gene_id_value}"
            datasets_url = f"https://api.ncbi.nlm.nih.gov/datasets/v2/gene/id/{gene_id_value}/dataset_report"
            rows.extend(
                [
                    f"| NCBI Gene 官方主页 | [{gene_url}]({gene_url}) | {_marker(audit_id, gene_id)} |",
                    f"| NCBI Datasets API | [{datasets_url}]({datasets_url}) | {_marker(audit_id, gene_id)} |",
                ]
            )
            projections.append(
                AuthorityProjection(
                    kind="official_link",
                    audit_id=audit_id,
                    blocks="\n".join(["| 项目 | 值 | 引用 |", "| --- | --- | --- |", *rows]),
                    used_fact_ids=tuple(str(fact.get("id")) for fact in used),
                    coverage={
                        "main": [str(fact.get("id")) for fact in used],
                        "boundary": [],
                        "folded": [],
                        "skipped": [],
                    },
                )
            )
    return projections


__all__ = ["AuthorityProjection", "NCBI_REPORT_TOOL", "project_ncbi_official_links"]
