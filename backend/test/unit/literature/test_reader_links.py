import pytest

from yuxi.literature.reader_links import parse_reader_target

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "target,identity,tab,variant,anchor,relative",
    [
        ("[[doi-10.1234_example/_wiki#核心概念|相关笔记]]", "doi-10.1234_example", "noteL2", None, "核心概念", False),
        ("../10.1234_example/_note.md#摘要", "10.1234_example", "noteL1", None, "摘要", True),
        ("./zh.md#%E6%91%98%E8%A6%81", "", "translation", "zh", "摘要", True),
        ("en_zh.md", "", "parallel", "en_zh", "", True),
        ("#^paragraph", "", None, None, "^paragraph", True),
        ("知识论_L3_relations.md#概念-1", "知识论", "noteL3", None, "概念-1", False),
        ("knowledge_base/doi-10.1234_example/L2_wiki.md", "doi-10.1234_example", "noteL2", None, "", False),
        ("10.1234/example", "10.1234/example", None, None, "", False),
    ],
)
def test_complex_reader_targets(target, identity, tab, variant, anchor, relative):
    assert parse_reader_target(target) == {
        "identity": identity,
        "tab": tab,
        "variant": variant,
        "anchor": anchor,
        "relative": relative,
    }


@pytest.mark.parametrize(
    "target",
    [
        ".",
        "..",
        "../../escape.md",
        ".././escape.md",
        "http://host/paper",
        "javascript:alert(1)",
        "/etc/private",
        "C:\\private",
        "%2e%2e/%2e%2e/private",
        "paper\x00.md",
        "",
        "a" * 501,
    ],
)
def test_reader_target_rejects_unsafe_paths(target):
    with pytest.raises(ValueError):
        parse_reader_target(target)
