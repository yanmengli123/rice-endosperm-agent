from pathlib import Path

import pytest

from yuxi.knowledge.parser.office_text import html_to_markdown, mineru_options, xls_to_markdown


def test_xls_all_sheets_preserve_literal_identifiers_and_na():
    source = Path(__file__).parents[2] / "fixtures" / "parser_compat" / "workbook.xls"
    result = xls_to_markdown(source)
    assert "## IDs" in result and "## Notes" in result
    assert "00123" in result and "NA" in result and "OsbZIP58" in result
    assert "00123.0" not in result
    assert result.index("00123") < result.index("## Notes")


def test_html_extraction_keeps_text_without_loading_or_executing_resources():
    result = html_to_markdown(
        "<html><style>private style</style><script>secret script</script>"
        '<h1>Grain filling</h1><p>Wx <b>methods</b></p><img src="http://127.0.0.1/secret" alt="Figure 1">'
        '<iframe src="https://example.com/private">hidden frame</iframe></html>'
    )
    assert "# Grain filling" in result and "Wx **methods**" in result and "Figure 1" in result
    assert "secret script" not in result and "private style" not in result
    assert "127.0.0.1" not in result and "hidden frame" not in result


def test_mineru_explicit_selection_and_compatible_backend_names():
    assert mineru_options({}) == {"backend": "hybrid-auto-engine", "parse_method": "auto"}
    assert mineru_options({"backend": "pipeline", "parse_method": "ocr"}) == {
        "backend": "pipeline",
        "parse_method": "ocr",
    }
    assert mineru_options({"backend": "vlm-vllm-engine"})["backend"] == "vlm-vllm-engine"
    for options in ({"backend": "bad\nheader"}, {"backend": None}, {"parse_method": "guess"}):
        with pytest.raises(ValueError):
            mineru_options(options)
