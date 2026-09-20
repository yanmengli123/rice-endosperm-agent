"""数据集原生示例问题：确定性构建（csv@1.2.0 dataset_sample_questions）。"""

from yuxi.services.csv_dataset_service import build_dataset_sample_questions


def test_csv_qa_samples_real_question_column():
    records = [
        {"record_key": f"row:{i}", "fields": {"__question__": f"GIF1 如何影响第{i}个性状？", "__answer__": f"答案{i}"}}
        for i in range(1, 31)
    ]
    questions = build_dataset_sample_questions(
        contract_key="csv_qa",
        dataset_title="qa.csv",
        records=records,
        columns=["question", "answer"],
        identity_column=None,
        count=5,
    )
    assert len(questions) == 5
    # 全部来自真实问题列，零编造
    pool = {record["fields"]["__question__"] for record in records}
    assert all(question in pool for question in questions)


def test_csv_record_uses_business_key_with_rotating_templates():
    records = [
        {"record_key": f"gene_{i:02d}", "fields": {"gene_id": f"gene_{i:02d}", "expression": str(i)}}
        for i in range(1, 10)
    ]
    questions = build_dataset_sample_questions(
        contract_key="csv_record",
        dataset_title="dic.csv",
        records=records,
        columns=["gene_id", "expression"],
        identity_column="gene_id",
        count=6,
    )
    assert 1 <= len(questions) <= 6
    # 业务主键进入问题（检索必命中该记录的行投影）
    assert any("gene_01" in question or "gene_05" in question for question in questions)
    # 模板轮换：不全是同一句式
    assert len(set(questions)) == len(questions)


def test_csv_record_row_number_strategy_and_column_fallback():
    records = [{"record_key": "row:2", "fields": {"abbr": "GIF1", "full": "Grain Incomplete Filling 1"}}]
    questions = build_dataset_sample_questions(
        contract_key="csv_record",
        dataset_title="dic.csv",
        records=records,
        columns=["abbr", "full"],
        identity_column=None,
        count=4,
    )
    # 行号策略：用首个非空字段值构造；不足 count 用字段名模板补齐
    assert any("GIF1" in question for question in questions)
    assert any("abbr" in question or "full" in question for question in questions)


def test_empty_records_returns_empty_for_qa():
    questions = build_dataset_sample_questions(
        contract_key="csv_qa",
        dataset_title="qa.csv",
        records=[{"record_key": "row:1", "fields": {}}],
        columns=["question", "answer"],
        identity_column=None,
        count=5,
    )
    assert questions == []
