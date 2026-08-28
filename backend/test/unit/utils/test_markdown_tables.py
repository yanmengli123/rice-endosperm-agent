from yuxi.utils.markdown_tables import normalize_markdown_tables


def test_repairs_mismatched_scientific_table_from_model_output() -> None:
    source = """证据类型与限制 | 关系 | 证据等级 | 关系组 | 证据类型 |
|—|—|—|—|
| OsbZIP58 → grain filling | E3 | ASSOCIATION_OR_CONTEXT | 文献观点/关联 | IND-2 热胁迫 |
| OsbZIP58 → germination | E3 | FUNCTIONAL_REGULATION | 互作 + 转录激活 | OsKO2 |

- 结论需要原始文献核验。"""

    result = normalize_markdown_tables(source)

    assert result.startswith(
        "| 证据类型与限制 | 关系 | 证据等级 | 关系组 | 证据类型 |\n| --- | --- | --- | --- | --- |\n"
    )
    assert "| OsbZIP58 → germination | E3 | FUNCTIONAL_REGULATION | 互作 + 转录激活 | OsKO2 |" in result
    assert result.endswith("\n\n- 结论需要原始文献核验。")


def test_preserves_alignment_and_pads_short_rows() -> None:
    source = "A｜B｜C\n:--｜--:｜:—:\n1｜2"

    assert normalize_markdown_tables(source) == ("| A | B | C |\n| :--- | ---: | :---: |\n| 1 | 2 |  |")


def test_does_not_change_code_fences_or_pipe_prose() -> None:
    source = """普通文字 A | B 不应变成表格。

```markdown
A | B
— | —
1 | 2
```
"""

    assert normalize_markdown_tables(source) == source


def test_preserves_escaped_pipes_and_inline_code() -> None:
    source = "| 表达式 | 说明 |\n| - | - |\n| `A | B` | A \\| B |"

    assert normalize_markdown_tables(source) == ("| 表达式 | 说明 |\n| --- | --- |\n| `A | B` | A \\| B |")
