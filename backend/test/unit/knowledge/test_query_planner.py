from yuxi.knowledge.planning.query_planner import plan_knowledge_query


def _plan(question: str):
    return plan_knowledge_query(question, strategy="KNOWLEDGE_FIRST", scope_nonempty=True)


def test_grain_size_gene_question_is_exact_exhaustive_enumeration():
    plan = _plan("对grain size进行调控的基因有哪些？")

    assert plan["intent"] == "PHENOTYPE_REGULATOR_ENUMERATION"
    assert plan["query_mode"] == "EXHAUSTIVE"
    assert plan["target_mention"] == "grain size"
    assert plan["retrieval_required"] is True
    assert plan["answer_mode"] == "HYBRID"


def test_endosperm_key_regulator_question_is_exact_exhaustive_enumeration():
    plan = _plan("水稻胚乳发育的关键调控基因有哪些？")

    assert plan["intent"] == "PHENOTYPE_REGULATOR_ENUMERATION"
    assert plan["query_mode"] == "EXHAUSTIVE"
    assert plan["target_mention"] == "水稻胚乳发育"
    assert plan["retrieval_required"] is True


def test_planner_is_stable_across_repeated_runs():
    plans = [_plan("列出所有调控 grain size 的基因") for _ in range(20)]

    assert all(plan == plans[0] for plan in plans)


def test_social_and_identity_turns_do_not_trigger_retrieval():
    assert _plan("hi")["retrieval_required"] is False
    assert _plan("你是谁？")["retrieval_required"] is False


def test_knowledge_first_defaults_ambiguous_scientific_question_to_retrieval():
    plan = _plan("水稻胚乳发育中淀粉合成有什么特点？")

    assert plan["intent"] == "GENERAL_KNOWLEDGE_QUERY"
    assert plan["retrieval_required"] is True


def test_disabled_scope_has_highest_priority():
    plan = plan_knowledge_query("grain size genes", strategy="DISABLED", scope_nonempty=True)

    assert plan["intent"] == "NO_RETRIEVAL"
    assert plan["retrieval_required"] is False
