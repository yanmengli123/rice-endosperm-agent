"""R6：图注 mention 解析（纯函数）——规范键与 caption_locator 同源。"""

from __future__ import annotations

from yuxi.knowledge.graphs.doclex.figure_mentions import (
    parse_body_figure_mentions,
    parse_figure_mentions,
)


def test_parse_mentions_normalizes_keys():
    text = "Expression analysis is shown in Fig. 5A and 图 3 respectively (see also Table S2)."
    keys = [mention.canonical_key for mention in parse_figure_mentions(text)]
    assert "figure 5a" in keys
    assert "figure 3" in keys
    assert "table s2" in keys


def test_supplementary_qualifier_normalized():
    mentions = parse_figure_mentions("Data are presented in Supplementary Figure 4 and Extended Data Fig. 2.")
    keys = [mention.canonical_key for mention in mentions]
    assert "figure s4" in keys
    assert "figure ed2" in keys


def test_duplicate_keys_keep_first_occurrence():
    mentions = parse_figure_mentions("图 1 shows morphology; details in 图 1.")
    assert len(mentions) == 1
    assert mentions[0].canonical_key == "figure 1"
    assert mentions[0].surface == "图 1"


def test_body_parser_skips_caption_lines():
    text = (
        "Figure 2. Phenotype of the gif1 mutant at 10 DAP.\n"
        "Grain size was reduced as shown in Figure 2.\n"
        "表 1 灌浆速率统计。\n"
        "统计结果见表 1。"
    )
    mentions = parse_body_figure_mentions(text)
    # 题注行（Figure 2. / 表 1 开头整行）跳过，只保留正文行里的两个 mention
    assert [mention.canonical_key for mention in mentions] == ["figure 2", "table 1"]
    assert all(
        "shown in" in text[mention.start - 40 : mention.end] or "见表" in text[mention.start - 20 : mention.end]
        for mention in mentions
    )


def test_no_mentions_in_plain_text():
    assert parse_figure_mentions("No figures were referenced in this sentence.") == []
