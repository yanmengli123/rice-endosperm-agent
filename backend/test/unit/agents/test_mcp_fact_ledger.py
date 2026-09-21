from dataclasses import dataclass, field

from yuxi.agents.mcp.fact_ledger import append_model_ledger, build_audit_manifest, extract_facts


@dataclass
class Result:
    text: str
    structured_content: dict | None = None
    content_blocks: list = field(default_factory=list)


def test_fact_ledger_extracts_bounded_scalar_leaves_deterministically():
    result = Result(text='{"gene":{"symbol":"Wx","start":1770556,"end":1770653},"rows":[]}')
    first, truncated = extract_facts(result)
    second, _ = extract_facts(result)

    assert truncated is False
    assert [(item.fact_id, item.path, item.value) for item in first] == [
        (item.fact_id, item.path, item.value) for item in second
    ]
    assert {item.path for item in first} == {"/gene/end", "/gene/start", "/gene/symbol"}
    audit = build_audit_manifest(first, truncated=False, public_values=True)
    assert {fact.get("numeric_value") for fact in audit["facts"] if "numeric_value" in fact} == {
        1770556,
        1770653,
    }


def test_model_ledger_contains_audit_bound_citation_format():
    facts, _ = extract_facts(Result(text='{"status":"FOUND"}'))
    rendered = append_model_ledger("provider payload", audit_id=17, facts=facts, truncated=False)

    assert "<YUXI_MCP_FACT_LEDGER>" in rendered
    assert "[MCP-F:17:<fact-id>]" in rendered
    assert facts[0].fact_id in rendered
