from __future__ import annotations

import time

import pytest

from yuxi.knowledge.planning.query_planner import plan_knowledge_query


CASES = {
    "PHENOTYPE_REGULATOR_ENUMERATION": [
        "对grain size进行调控的基因有哪些？",
        "列出所有调控 grain size 的基因",
        "哪些基因调控 grain size？",
        "List all genes that regulate grain size.",
        "Which genes affect grain weight?",
        "影响 chalkiness 的基因有哪些？",
        "控制 grain filling 的基因有什么？",
        "参与 starch synthesis 的基因有哪些？",
        "所有影响 seed size 的 genes 有哪些？",
        "What genes control yield?",
    ],
    "SOCIAL": [
        "hi",
        "Hello!",
        "hey",
        "你好",
        "您好！",
        "嗨。",
        "早上好",
        "谢谢",
        "Thank you!",
        "再见",
    ],
    "IDENTITY": [
        "你是谁？",
        "你叫什么？",
        "你叫什么名字",
        "请问你是谁",
        "Who are you?",
        "what is your name?",
        "What's your name?",
        "告诉我你是谁",
        "稻芯智析，你是谁",
        "你的名字是什么，你是谁？",
    ],
    "TRANSFORMATION": [
        "翻译这句话",
        "请翻译以下摘要",
        "改写这一段",
        "请改写下面内容",
        "润色论文标题",
        "请润色这句话",
        "校对下面的英文",
        "Translate this sentence",
        "Rewrite this paragraph",
        "Polish the abstract",
    ],
    "MECHANISM_EXPLANATION": [
        "OsSUT1如何影响胚乳发育？",
        "GS3通过什么机制影响粒型？",
        "Wx参与哪个机制？",
        "解释淀粉合成通路",
        "OsNF-YB1怎么调控灌浆？",
        "How does GS3 affect grain size?",
        "What is the mechanism of FLO2?",
        "Describe the starch synthesis pathway",
        "OsPPDKB如何响应高温？",
        "chalkiness形成机制是什么？",
    ],
    "RELATION_LOOKUP": [
        "GS3是否调控grain size？",
        "Wx和直链淀粉是什么关系？",
        "OsSUT1与蔗糖运输是否关联？",
        "FLO2和胚乳发育的关系",
        "OsPPDKB是否影响产量？",
        "Does GS3 regulate grain size?",
        "relationship between Wx and amylose",
        "OsNF-YB1是否影响灌浆",
        "chalkiness与高温是否关联",
        "ISA1和淀粉结构是什么关系？",
    ],
    "ENTITY_LOOKUP": [
        "OsSUT1的RAP ID是什么？",
        "GS3的MSU ID",
        "Wx的基因编号是什么？",
        "FLO2是什么基因？",
        "给出OsNF-YB1 identifier",
        "RAP ID for GS3",
        "MSU ID of Wx",
        "OsPPDKB是什么基因",
        "ISA1的基因编号",
        "identifier of OsSGL",
    ],
    "DOCUMENT_EVIDENCE_SEARCH": [
        "这篇文章的主要结论是什么？",
        "本文报道了哪些基因？",
        "检索相关文献",
        "这篇论文用了什么材料？",
        "查找grain size文章",
        "Summarize this article",
        "What does this paper report?",
        "Find the relevant document",
        "论文中的实验条件是什么？",
        "文献里是否提供PMID？",
    ],
}


@pytest.mark.parametrize(
    ("expected_intent", "question"),
    [(intent, question) for intent, questions in CASES.items() for question in questions],
)
def test_eighty_question_planner_regression(expected_intent: str, question: str):
    plan = plan_knowledge_query(question, strategy="KNOWLEDGE_FIRST", scope_nonempty=True)

    assert plan["intent"] == expected_intent
    assert plan["retrieval_required"] is (expected_intent not in {"SOCIAL", "IDENTITY", "TRANSFORMATION"})


def test_eighty_question_planner_benchmark_is_bounded():
    started = time.perf_counter()
    plans = [
        plan_knowledge_query(question, strategy="KNOWLEDGE_FIRST", scope_nonempty=True)
        for questions in CASES.values()
        for question in questions
    ]

    assert len(plans) == 80
    assert time.perf_counter() - started < 1.0
