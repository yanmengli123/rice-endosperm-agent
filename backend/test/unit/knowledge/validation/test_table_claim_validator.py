from yuxi.knowledge.validation.table_claim_validator import table_facts, validate_table_claims


def _cell(text, *, header=False, colspan=1, rowspan=1):
    return {"text": text, "header": header, "colspan": colspan, "rowspan": rowspan}


def _tables():
    return [
        {
            "label": "Table 1",
            "header_rows": 1,
            "rows": [
                [
                    _cell("Genotype", header=True),
                    _cell("Amylose (%)", header=True),
                    _cell("Gelatinization temp (C)", header=True),
                    _cell("Chalkiness (%)", header=True),
                ],
                [_cell("WT (ZH11)"), _cell("18.4"), _cell("76.2"), _cell("12.1")],
                [_cell("osmyb73"), _cell("14.9"), _cell("71.5"), _cell("26.8")],
                [_cell("osnf-yb1"), _cell("16.2"), _cell("73.0"), _cell("21.4")],
            ],
        },
        {
            "label": "Table 2",
            "header_rows": 2,
            "rows": [
                [
                    _cell("Genotype", header=True, rowspan=2),
                    _cell("Length (mm)", header=True, colspan=2),
                    _cell("Width (mm)", header=True, colspan=2),
                ],
                [
                    _cell("Control", header=True),
                    _cell("Heat", header=True),
                    _cell("Control", header=True),
                    _cell("Heat", header=True),
                ],
                [_cell("WT (ZH11)"), _cell("5.12"), _cell("4.71"), _cell("2.94"), _cell("2.78")],
                [_cell("osmyb73"), _cell("5.86"), _cell("5.02"), _cell("2.71"), _cell("2.55")],
                [_cell("osnf-yb1"), _cell("5.44"), _cell("4.88"), _cell("2.83"), _cell("2.61")],
            ],
        },
    ]


def test_table_facts_preserve_multilevel_header_coordinates_and_deltas():
    facts = table_facts(_tables())
    assert any(
        fact["row_key"] == "osmyb73"
        and fact["metric"] == "grain_length"
        and fact["condition"] == "heat"
        and fact["value"] == "5.02"
        for fact in facts
    )
    assert any(
        fact["row_key"] == "osmyb73"
        and fact["metric"] == "grain_length"
        and fact["condition"] == "delta"
        and fact["value"] == "0.84"
        for fact in facts
    )


def test_wrong_row_is_not_reinterpreted_as_heat_value():
    text = "ZH11 在正常条件下直链淀粉为 18.4%，热胁迫后下降至 14.9%。\nZH11 粒长从对照 5.12 mm 下降到热胁迫 4.71 mm。"
    cleaned, report = validate_table_claims(
        text,
        question="解释热胁迫下直链淀粉和粒长差异",
        tables=_tables(),
    )
    assert "14.9" not in cleaned
    assert "5.12" in cleaned and "4.71" in cleaned
    assert report["removed_sentence_count"] == 1
    assert report["reason_counts"] == {"CELL_COORDINATE_MISMATCH": 1}
    assert report["supporting_table_labels"] == ["Table 2"]


def test_missing_paired_chalkiness_refuses_ranking_without_values():
    cleaned, report = validate_table_claims(
        "osmyb73 热胁迫后垩白度为 26.8%，受影响最大。",
        question="哪个基因型的垩白度受热胁迫影响最大？",
        tables=_tables(),
    )
    assert report["status"] == "REFUSED_INCOMPLETE_OPERANDS"
    assert "无法进行该比较" in cleaned
    assert "26.8" not in cleaned


def test_unbacked_significance_is_removed():
    cleaned, report = validate_table_claims(
        "osmyb73 垩白度为 26.8%，显著高于 WT。",
        question="比较各基因型垩白度",
        tables=_tables(),
    )
    assert "26.8" not in cleaned
    assert report["reason_counts"] == {"SIGNIFICANCE_NOT_IN_TABLE": 1}


def test_qualitative_heat_claim_requires_conditioned_metric_columns():
    cleaned, report = validate_table_claims(
        "热胁迫导致直链淀粉下降和垩白度升高。热胁迫导致粒长和粒宽下降。",
        question="解释热胁迫下淀粉理化性状和籽粒维度差异",
        tables=_tables(),
    )
    assert "直链淀粉" not in cleaned
    assert "粒长和粒宽下降" in cleaned
    assert report["reason_counts"] == {"CONDITION_NOT_IN_TABLE": 1}
    assert report["supporting_table_labels"] == ["Table 2"]


def test_group_query_reports_missing_physicochemical_pairs():
    _cleaned, report = validate_table_claims(
        "Table 2 显示粒长变化。",
        question="解释热胁迫下淀粉理化性状和籽粒维度差异",
        tables=_tables(),
    )
    assert report["missing_pair_metrics"] == ["amylose", "chalkiness", "gelatinization_temperature"]
