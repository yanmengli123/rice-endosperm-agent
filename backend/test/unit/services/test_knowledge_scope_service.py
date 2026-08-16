from yuxi.services.knowledge_scope_service import compute_effective_scope_ids, replay_scope_member_audits


def test_inherit_global_applies_access_and_session_intersections():
    result = compute_effective_scope_ids(
        mode="INHERIT_GLOBAL",
        accessible_ids={"rice", "private"},
        global_ids={"rice", "forbidden"},
        custom_ids={"private"},
        session_kb_ids={"rice", "forbidden", "session-only"},
    )

    assert result == {"rice"}


def test_global_plus_custom_unions_base_before_permission_filter():
    result = compute_effective_scope_ids(
        mode="GLOBAL_PLUS_CUSTOM",
        accessible_ids={"global", "custom"},
        global_ids={"global", "no-access"},
        custom_ids={"custom", "no-access-custom"},
        session_kb_ids=None,
    )

    assert result == {"global", "custom"}


def test_empty_session_narrowing_cannot_expand_scope():
    result = compute_effective_scope_ids(
        mode="CUSTOM",
        accessible_ids={"a", "b"},
        global_ids={"a"},
        custom_ids={"b"},
        session_kb_ids=set(),
    )

    assert result == set()


def test_disabled_scope_is_empty_even_when_everything_is_accessible():
    result = compute_effective_scope_ids(
        mode="DISABLED",
        accessible_ids={"a", "b"},
        global_ids={"a"},
        custom_ids={"b"},
        session_kb_ids=None,
    )

    assert result == set()


def test_scope_history_replay_reconstructs_exact_member_policies():
    audits = [
        {"new_version": 2, "after": {"kb_id": "kb-a", "enabled": True, "priority": 100}},
        {"new_version": 3, "after": {"kb_id": "kb-b", "enabled": True, "priority": 50}},
        {"new_version": 4, "after": {"kb_id": "kb-a", "enabled": False, "priority": 100}},
    ]

    version_three = replay_scope_member_audits(audits, target_version=3)
    assert [item["kb_id"] for item in version_three] == ["kb-b", "kb-a"]
    assert next(item for item in version_three if item["kb_id"] == "kb-a")["enabled"] is True

    version_four = replay_scope_member_audits(audits, target_version=4)
    assert next(item for item in version_four if item["kb_id"] == "kb-a")["enabled"] is False
