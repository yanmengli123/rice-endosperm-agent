from dataclasses import dataclass, field

from yuxi.agents.mcp.fact_ledger import (
    append_model_ledger,
    build_audit_manifest,
    extract_facts,
    extract_number_tokens,
    mask_structural_number_spans,
)


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


def test_extract_number_tokens_catches_cjk_adjacent_and_attached_units():
    # CJK 毗邻与贴合单位的数字必须被抽取——这是 Wx 案例里 3bp 无空格逃逸的补洞。
    assert extract_number_tokens("相差3bp") == ["3"]
    assert extract_number_tokens("差值为97个碱基") == ["97"]
    assert extract_number_tokens("长度 3 bp")[0] == "3"
    assert extract_number_tokens("蛋白长度是300aa") == ["300"]


def test_extract_number_tokens_ignores_identifier_and_version_digits():
    # 标识符与版本号内嵌数字不构成数值主张。
    assert extract_number_tokens("LOC_Os06g01210.1") == []
    assert extract_number_tokens("Os06t0101600-01") == ["01"]  # 与事实侧口径一致即可
    assert extract_number_tokens("Q0DEV5") == []
    assert extract_number_tokens("v1.21.0") == []
    assert extract_number_tokens("1,770,653") == ["1,770,653"]


def test_mask_structural_spans_exempts_datetimes_and_json_paths_but_not_values():
    assert mask_structural_number_spans("检索时间：2026-09-21T07:12:43+00:00") == "检索时间：▁"
    assert mask_structural_number_spans("路径 /rows/0/start") == "路径 ▁"
    assert mask_structural_number_spans("2026年9月21日") == "▁"
    # 坐标本身不能被当成路径掩蔽
    assert "1770653" in mask_structural_number_spans("坐标 1770556/1770653")


def test_public_manifest_carries_string_values_for_repair_and_degraded_rendering():
    result = Result(text='{"gene":{"symbol":"Wx","start":1770556}}')
    facts, _ = extract_facts(result)
    audit = build_audit_manifest(facts, truncated=False, public_values=True)
    by_path = {fact["path"]: fact for fact in audit["facts"]}
    assert by_path["/gene/symbol"]["string_value"] == "Wx"
    assert by_path["/gene/start"]["numeric_value"] == 1770556
    restricted = build_audit_manifest(facts, truncated=False, public_values=False)
    assert all("string_value" not in fact and "numeric_value" not in fact for fact in restricted["facts"])
