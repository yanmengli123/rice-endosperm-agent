from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuxi.services import knowledge_scope_service, wiki_runtime_service


def _user(uid="u1", role="user", dept=None):
    return SimpleNamespace(uid=uid, role=role, department_id=dept)


@pytest.fixture
def base_env(monkeypatch):
    scope = SimpleNamespace(scope_id="s1", slug="default", version=3, retrieval_mode="KB_ONLY", allow_web=False)
    repo = SimpleNamespace(
        ensure_default_scope=AsyncMock(return_value=scope),
        list_members=AsyncMock(return_value=[]),
        get_agent_config=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(knowledge_scope_service, "KnowledgeScopeRepository", lambda db: repo)
    agent = SimpleNamespace(slug="custom-agent", config_json={"context": {"knowledges": None}})
    return repo, agent


@pytest.fixture
def publication_env(base_env, monkeypatch):
    _, agent = base_env
    monkeypatch.setattr("yuxi.services.principal.resolve_tenant_id", AsyncMock(return_value=7))
    accessible = [
        {"kb_id": "wiki-a", "name": "wiki", "kb_type": "llmwiki"},
        {"kb_id": "src-a", "name": "source a", "kb_type": "milvus"},
        {"kb_id": "src-b", "name": "source b", "kb_type": "milvus"},
    ]
    monkeypatch.setattr(knowledge_scope_service, "_accessible_knowledge_bases", AsyncMock(return_value=accessible))
    wiki = SimpleNamespace(
        wiki_id="wiki-identity", current_publication_id="pub-1", status="ACTIVE", security_domain="s1"
    )
    manifest = {"source_kb_ids": ["src-b", "src-a"], "security_domain": "s1"}
    publication = SimpleNamespace(
        publication_id="pub-1",
        manifest_hash=wiki_runtime_service._digest(manifest),
        snapshot_id="snap-1",
        manifest_json=manifest,
        status="ACTIVE",
    )

    class FakeScalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

    class FakeResult:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

        def scalars(self):
            # knowledge_scope_service 冻结 KB 行（治理/契约身份）走 scalars().all()
            return FakeScalars(self.value if isinstance(self.value, list) else [])

    frozen_rows = [
        SimpleNamespace(
            kb_id=kb_id,
            governance_status="ACTIVE",
            contract_key=f"{kb_id}-contract",
            contract_version=1,
            contract_digest="d" * 16,
            active_release_id=None,
        )
        for kb_id in ("wiki-a", "src-a", "src-b")
    ]
    db = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                FakeResult(agent),
                FakeResult(frozen_rows),
                FakeResult(wiki),
                FakeResult(publication),
            ]
        )
    )
    monkeypatch.setattr(wiki_runtime_service, "_enabled_source_ids", AsyncMock(return_value=["src-a", "src-b"]))
    source_access = AsyncMock()
    monkeypatch.setattr(wiki_runtime_service, "assert_wiki_source_access", source_access)
    return db, agent, accessible, publication, source_access


@pytest.mark.parametrize(
    "restriction", ["no_access", "outside_base", "session_narrowed", "wiki_only", "derived_source", "empty_sources"]
)
async def test_wiki_member_requires_closed_publication_sources(publication_env, restriction):
    db, agent, accessible, publication, source_access = publication_env
    session_ids = None
    expected_raw_ids = ["src-a"]
    expected_filtered = [{"kb_id": "wiki-a", "reason": "WIKI_NAVIGATION_UNAVAILABLE"}]
    if restriction == "no_access":
        accessible.pop()
        agent.config_json["context"]["knowledges"] = ["wiki-a", "src-a", "src-b"]
        expected_filtered.append({"kb_id": "src-b", "reason": "NO_ACCESS"})
    elif restriction == "outside_base":
        agent.config_json["context"]["knowledges"] = ["wiki-a", "src-a"]
    elif restriction == "session_narrowed":
        session_ids = ["wiki-a", "src-a"]
        expected_filtered.append({"kb_id": "src-b", "reason": "SESSION_NARROWED"})
    elif restriction == "wiki_only":
        session_ids = ["wiki-a"]
        expected_raw_ids = []
        expected_filtered.extend({"kb_id": kb_id, "reason": "SESSION_NARROWED"} for kb_id in ["src-a", "src-b"])
    else:
        publication.manifest_json["source_kb_ids"] = ["wiki-a"] if restriction == "derived_source" else []
        publication.manifest_hash = wiki_runtime_service._digest(publication.manifest_json)
        expected_raw_ids = ["src-a", "src-b"]

    snapshot = await knowledge_scope_service.resolve_effective_knowledge_scope(
        db=db, user=_user(), agent_slug="custom-agent", session_kb_ids=session_ids
    )

    # 发布依赖不闭合时只排除 Wiki，不能补入来源，也不能丢掉其他过滤记录。
    assert snapshot["effective_kb_ids"] == expected_raw_ids
    assert [item["kb_id"] for item in snapshot["members"]] == expected_raw_ids
    assert sorted(snapshot["filtered_out"], key=lambda item: item["kb_id"]) == sorted(
        expected_filtered, key=lambda item: item["kb_id"]
    )
    source_access.assert_not_awaited()


async def test_wiki_member_closure_freezes_publication_identity(publication_env):
    db, _, _, publication, source_access = publication_env
    snapshot = await knowledge_scope_service.resolve_effective_knowledge_scope(
        db=db, user=_user(), agent_slug="custom-agent"
    )
    wiki_member = next(item for item in snapshot["members"] if item["kb_id"] == "wiki-a")
    assert snapshot["effective_kb_ids"] == ["src-a", "src-b", "wiki-a"]
    assert snapshot["filtered_out"] == []
    assert wiki_member["wiki_navigation_enabled"] is True
    assert wiki_member["wiki_id"] == "wiki-identity"
    assert wiki_member["publication_id"] == "pub-1"
    assert wiki_member["manifest_hash"] == publication.manifest_hash
    assert wiki_member["snapshot_id"] == "snap-1"
    assert wiki_member["wiki_source_kb_ids"] == ["src-a", "src-b"]
    assert "source_kb_ids" not in wiki_member
    source_access.assert_awaited_once_with(
        db,
        wiki_id="wiki-identity",
        tenant_id=7,
        permitted_source_kb_ids={"src-a", "src-b"},
        publication_id="pub-1",
    )
    # 快照的来源列表不与发布 manifest 共享可变对象。
    publication.manifest_json["source_kb_ids"].clear()
    assert wiki_member["wiki_source_kb_ids"] == ["src-a", "src-b"]
