"""CSV 数据产品纯逻辑单测：解析、严格 QA 校验、规范记录与投影。"""

from __future__ import annotations

import pytest

from yuxi.knowledge.evidence.glossary import fold_term_key
from yuxi.services.csv_dataset_service import (
    CsvDatasetValidationError,
    build_canonical_records,
    build_projection_markdown,
    detect_delimiter,
    detect_encoding,
    infer_column_stats,
    parse_csv_rows,
    schema_hash,
    suggest_column_mapping,
    validate_glossary_folds,
    validate_qa_mapping,
    validate_record_mapping,
)

CSV_RECORD_BYTES = ("gene_id,expression,note\nLOC_Os01g01010,3.42,正常表达\nLOC_Os01g01020,0.00,低表达\n").encode()

CSV_QA_BYTES = (
    "question,answer\n"
    "水稻基因组多大？,约 373 Mb（日本晴）\n"
    ",空问题行\n"
    "什么是穗粒数？,\n"
    "胚乳发育关键基因？,RSR1 调控淀粉合成\n"
).encode()


class TestParsing:
    def test_parse_utf8_csv(self):
        parsed = parse_csv_rows(CSV_RECORD_BYTES)
        assert parsed["encoding"] in ("utf-8-sig", "utf-8")
        assert parsed["delimiter"] == ","
        assert parsed["header"] == ["gene_id", "expression", "note"]
        assert parsed["row_count"] == 2

    def test_parse_gbk(self):
        raw = "question,answer\n问题,答案\n".encode("gbk")
        parsed = parse_csv_rows(raw)
        assert parsed["encoding"] == "gbk"

    def test_empty_file_rejected(self):
        with pytest.raises(CsvDatasetValidationError):
            parse_csv_rows(b"")

    def test_header_only_rejected(self):
        with pytest.raises(CsvDatasetValidationError):
            parse_csv_rows(b"a,b\n")

    def test_detect_delimiter_tsv(self):
        assert detect_delimiter("a\tb\tc") == "\t"

    def test_detect_encoding_unknown_rejected(self):
        with pytest.raises(CsvDatasetValidationError):
            detect_encoding(b"\xff\xfe\x00\x01")


class TestColumnStats:
    def test_infer_types(self):
        parsed = parse_csv_rows(CSV_RECORD_BYTES)
        stats = infer_column_stats(parsed["header"], parsed["rows"])
        by_name = {item["name"]: item for item in stats}
        assert by_name["expression"]["inferred_type"] == "numeric"
        assert by_name["note"]["inferred_type"] == "text"
        assert by_name["gene_id"]["fill_rate"] == 1.0


class TestQAStrictValidation:
    def test_no_mapping_is_fatal(self):
        parsed = parse_csv_rows(CSV_QA_BYTES)
        report = validate_qa_mapping(parsed["header"], parsed["rows"], None, None)
        assert report["fatal"]
        assert "前两列" in "".join(report["issues"])

    def test_unknown_column_is_fatal(self):
        parsed = parse_csv_rows(CSV_QA_BYTES)
        report = validate_qa_mapping(parsed["header"], parsed["rows"], "question", "answer_text")
        assert report["fatal"]

    def test_empty_qa_rows_excluded_and_counted(self):
        parsed = parse_csv_rows(CSV_QA_BYTES)
        report = validate_qa_mapping(parsed["header"], parsed["rows"], "question", "answer")
        assert report["valid_pair_count"] == 2
        assert report["invalid_row_count"] == 2
        reasons = {item["reason"] for item in report["invalid_rows"]}
        assert reasons == {"empty_question", "empty_answer"}

    def test_all_invalid_is_fatal(self):
        raw = b"question,answer\nx,\n,y\n"
        parsed = parse_csv_rows(raw)
        report = validate_qa_mapping(parsed["header"], parsed["rows"], "question", "answer")
        assert report["fatal"]
        assert report["valid_pair_count"] == 0


class TestSuggestMapping:
    def test_suggest_qa_columns(self):
        suggestion = suggest_column_mapping(["question", "answer"], "csv_qa")
        assert suggestion == {"question_col": "question", "answer_col": "answer"}

    def test_suggest_chinese_qa_columns(self):
        suggestion = suggest_column_mapping(["问题", "答案"], "csv_qa")
        assert suggestion == {"question_col": "问题", "answer_col": "答案"}

    def test_suggest_identity_column(self):
        suggestion = suggest_column_mapping(["gene_id", "value"], "csv_record")
        assert suggestion["identity_column"] == "gene_id"

    def test_no_identity_suggestion_notes_row_number(self):
        suggestion = suggest_column_mapping(["a", "b"], "csv_record")
        assert suggestion["identity_column"] is None
        assert "row_number" in suggestion["identity_note"]


class TestCanonicalRecords:
    def test_business_key_identity(self):
        parsed = parse_csv_rows(CSV_RECORD_BYTES)
        records = build_canonical_records(
            parsed["header"], parsed["rows"], contract_key="csv_record", identity_column="gene_id"
        )
        assert [record["record_key"] for record in records] == ["LOC_Os01g01010", "LOC_Os01g01020"]
        assert all(record["identity_strategy"] == "business_key" for record in records)
        assert records[0]["row_number"] == 2  # 第 1 行是表头
        assert records[0]["projection_hash"]

    def test_row_number_fallback_identity(self):
        parsed = parse_csv_rows(CSV_RECORD_BYTES)
        records = build_canonical_records(
            parsed["header"], parsed["rows"], contract_key="csv_record", identity_column=None
        )
        assert records[0]["record_key"] == "row:2"
        assert records[0]["identity_strategy"] == "row_number"

    def test_qa_records_skip_invalid_rows(self):
        parsed = parse_csv_rows(CSV_QA_BYTES)
        records = build_canonical_records(
            parsed["header"],
            parsed["rows"],
            contract_key="csv_qa",
            identity_column=None,
            question_col="question",
            answer_col="answer",
        )
        assert len(records) == 2
        assert records[0]["fields"]["__question__"] == "水稻基因组多大？"

    def test_projection_markdown_block_per_record(self):
        parsed = parse_csv_rows(CSV_RECORD_BYTES)
        records = build_canonical_records(
            parsed["header"], parsed["rows"], contract_key="csv_record", identity_column="gene_id"
        )
        markdown = build_projection_markdown(records, contract_key="csv_record", dataset_title="t.csv")
        assert "### LOC_Os01g01010" in markdown
        assert "来源行：2｜记录键：LOC_Os01g01010" in markdown
        # 空行分隔，保证 separator 分块一块一条记录
        assert "\n\n" in markdown


def test_schema_hash_changes_with_columns():
    assert schema_hash(["a", "b"]) != schema_hash(["a", "c"])
    assert schema_hash(["a", "b"]) == schema_hash(["a", "b"])


def test_validate_record_mapping_unknown_identity():
    report = validate_record_mapping(["a"], "missing")
    assert report["fatal"]
    report2 = validate_record_mapping(["a"], None)
    assert report2["identity_strategy"] == "row_number"


def test_glossary_requires_identity_and_builds_normalized_aliases():
    assert validate_record_mapping(["term", "aliases", "definition"], None, require_identity=True)["fatal"]
    rows = [["ＰＣＲ", "PCR|聚合酶链式反应", "一种扩增方法"]]
    records = build_canonical_records(
        ["term", "aliases", "definition"],
        rows,
        contract_key="glossary",
        identity_column="term",
    )

    assert records[0]["normalized_key"] == "pcr"
    assert records[0]["aliases"] == ["PCR", "聚合酶链式反应"]


def test_glossary_aliases_split_on_chinese_enum_comma_and_carry_fold_key():
    rows = [["frameshift", "frame-shift、reading-frame shift", "移码突变"]]
    records = build_canonical_records(
        ["term", "aliases", "definition"],
        rows,
        contract_key="glossary",
        identity_column="term",
    )

    assert records[0]["aliases"] == ["frame-shift", "reading-frame shift"]
    assert records[0]["fold_key"] == "frameshift"
    assert records[0]["fold_key"] == fold_term_key("frame shift")


def test_validate_glossary_folds_flags_cross_record_collision_as_fatal():
    records = [
        {"record_key": "frameshift", "row_number": 2, "aliases": ["frame-shift"]},
        {"record_key": "frame shift", "row_number": 3, "aliases": []},
    ]
    lint = validate_glossary_folds(records)
    assert lint["fatal"]
    assert any("frameshift" in issue for issue in lint["issues"])


def test_validate_glossary_folds_allows_same_record_variants_and_warns_on_parenthetical_enum():
    records = [
        {
            "record_key": "frameshift",
            "row_number": 2,
            "aliases": ["frame-shift", "PCR（也写作P.C.R）"],
        }
    ]
    lint = validate_glossary_folds(records)
    assert not lint["fatal"]
    assert lint["valid"]
    assert len(lint["warnings"]) == 1
    assert "括号" in lint["warnings"][0]
