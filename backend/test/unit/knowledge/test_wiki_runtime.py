from contextlib import asynccontextmanager
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from yuxi.storage.postgres.models_knowledge import KnowledgeBase, KnowledgeWiki, WikiPublication, WikiSourceBinding
from yuxi.services.wiki_service import _digest, _security_domain

from yuxi.knowledge.orchestration.retrieval_orchestrator import _merge_gateway_results
from yuxi.services.subagent_run_service import narrow_child_scope_to_parent


@pytest.fixture
async def wiki_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for model in (KnowledgeBase, KnowledgeWiki, WikiPublication, WikiSourceBinding):
            await connection.run_sync(model.__table__.create)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        domain = _security_domain(7, {"access_level": "global"})
        manifest = {
            "source_kb_ids": ["a", "b"],
            "security_domain": domain,
            "navigation_entries": [
                {"title": "Waxy", "page_key": "entity:w", "source_kb_ids": ["a"], "expansion_terms": ["GBSSI"]}
            ],
        }
        db.add_all(
            [
                KnowledgeBase(kb_id=x, name=x, kb_type="milvus", tenant_id=7, share_config={"access_level": "global"})
                for x in ("a", "b")
            ]
        )
        db.add(
            KnowledgeWiki(
                id=1,
                wiki_id="w",
                kb_id="wiki",
                tenant_id=7,
                security_domain=domain,
                created_by="u",
                current_publication_id="old",
                status="PUBLISHED",
            )
        )
        db.add(
            WikiPublication(
                id=1,
                publication_id="old",
                wiki_id="w",
                tenant_id=7,
                snapshot_id="s",
                build_id="build",
                status="ACTIVE",
                manifest_hash=_digest(manifest),
                manifest_json=manifest,
            )
        )
        for i, x in enumerate(("a", "b"), 1):
            db.add(
                WikiSourceBinding(
                    id=i,
                    binding_id=x,
                    wiki_id="w",
                    source_kb_id=x,
                    security_domain=domain,
                    created_by="u",
                    enabled=True,
                )
            )
        await db.commit()
        yield db
    await engine.dispose()


@pytest.mark.parametrize("sources,expected", [(["a"], ["a"]), (["a", "b"], ["a", "b", "wiki"])])
async def test_freeze_source_closure_never_expands(wiki_db, sources, expected):
    from yuxi.services.wiki_runtime_service import freeze_scope_wikis

    scope = {
        "tenant_id": 7,
        "effective_kb_ids": [*sources, "wiki"],
        "members": [
            *[{"kb_id": x, "kb_type": "milvus"} for x in sources],
            {"kb_id": "wiki", "kb_type": "llmwiki", "wiki_navigation_enabled": True},
        ],
    }
    result = await freeze_scope_wikis(wiki_db, snapshot=scope)
    assert result["effective_kb_ids"] == expected
    if "wiki" in expected:
        assert result["members"][-1]["publication_id"] == "old"
        assert result["members"][-1]["snapshot_id"] == "s"
    else:
        assert result["filtered_out"][-1]["kb_id"] == "wiki"


@pytest.mark.parametrize(
    "change,allowed",
    [
        ("superseded", True),
        ("revoked", False),
        ("deleted", False),
        ("binding", False),
        ("acl", False),
        ("tenant", False),
    ],
)
async def test_frozen_release_rechecks_security_without_following_pointer(wiki_db, change, allowed):
    from sqlalchemy import select
    from yuxi.services.wiki_runtime_service import freeze_scope_wikis

    scope = {
        "tenant_id": 7,
        "effective_kb_ids": ["a", "b", "wiki"],
        "members": [{"kb_id": x, "kb_type": "milvus"} for x in ("a", "b")]
        + [{"kb_id": "wiki", "kb_type": "llmwiki", "wiki_navigation_enabled": True}],
    }
    frozen = await freeze_scope_wikis(wiki_db, snapshot=scope)
    wiki = (await wiki_db.execute(select(KnowledgeWiki))).scalar_one()
    publication = (await wiki_db.execute(select(WikiPublication))).scalar_one()
    wiki.current_publication_id = "new"
    publication.status = "SUPERSEDED"
    if change == "revoked":
        publication.status = "REVOKED"
    elif change == "deleted":
        wiki.status = "DELETED"
    elif change == "binding":
        (
            await wiki_db.execute(select(WikiSourceBinding).where(WikiSourceBinding.source_kb_id == "b"))
        ).scalar_one().enabled = False
    elif change in {"acl", "tenant"}:
        source = (await wiki_db.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == "b"))).scalar_one()
        if change == "acl":
            source.share_config = {"access_level": "user", "user_uids": ["u"]}
        else:
            source.tenant_id = 8
    await wiki_db.commit()
    result = await freeze_scope_wikis(wiki_db, snapshot=frozen, require_frozen=True)
    assert ("wiki" in result["effective_kb_ids"]) is allowed
    if allowed:
        assert result["members"][-1]["publication_id"] == "old"


@pytest.mark.parametrize("require_frozen", [False, True])
async def test_existing_frozen_identity_does_not_rebind_to_new_active_publication(wiki_db, require_frozen):
    from sqlalchemy import select
    from yuxi.services.wiki_runtime_service import freeze_scope_wikis

    wiki = (await wiki_db.execute(select(KnowledgeWiki))).scalar_one()
    old = (await wiki_db.execute(select(WikiPublication))).scalar_one()
    member = {
        "kb_id": "wiki",
        "kb_type": "llmwiki",
        "wiki_navigation_enabled": True,
        "wiki_id": "w",
        "publication_id": old.publication_id,
        "manifest_hash": old.manifest_hash,
        "snapshot_id": old.snapshot_id,
        "wiki_source_kb_ids": ["a", "b"],
    }
    scope = {"tenant_id": 7, "members": [{"kb_id": x, "kb_type": "milvus"} for x in ("a", "b")] + [member]}
    old.status = "SUPERSEDED"
    wiki.current_publication_id = "new"
    wiki_db.add(
        WikiPublication(
            id=2,
            publication_id="new",
            wiki_id="w",
            tenant_id=7,
            snapshot_id="s-new",
            build_id="build-new",
            status="ACTIVE",
            manifest_hash=old.manifest_hash,
            manifest_json=old.manifest_json,
        )
    )
    await wiki_db.commit()

    result = await freeze_scope_wikis(wiki_db, snapshot=scope, require_frozen=require_frozen)

    assert result["effective_kb_ids"] == ["a", "b", "wiki"]
    assert result["members"][-1] == member
    assert result["filtered_out"] == []


@pytest.fixture
async def frozen_scope(wiki_db):
    from sqlalchemy import select

    publication = (await wiki_db.execute(select(WikiPublication))).scalar_one()
    return {
        "tenant_id": 7,
        "effective_kb_ids": ["a", "b", "wiki"],
        "members": [{"kb_id": x, "kb_type": "milvus"} for x in ("a", "b")]
        + [
            {
                "kb_id": "wiki",
                "kb_type": "llmwiki",
                "wiki_navigation_enabled": True,
                "wiki_id": "w",
                "publication_id": "old",
                "manifest_hash": publication.manifest_hash,
                "snapshot_id": "s",
                "wiki_source_kb_ids": ["b", "a"],
            }
        ],
        "filtered_out": [{"kb_id": "unrelated", "reason": "SESSION_NARROWED"}],
    }


@pytest.mark.parametrize("require_frozen", [False, True])
@pytest.mark.parametrize("field", ["wiki_id", "publication_id", "manifest_hash", "snapshot_id", "wiki_source_kb_ids"])
async def test_partial_identity_never_falls_back_to_current(wiki_db, frozen_scope, field, require_frozen):
    from yuxi.services.wiki_runtime_service import freeze_scope_wikis

    frozen_scope["members"][-1].pop(field)
    result = await freeze_scope_wikis(wiki_db, snapshot=frozen_scope, require_frozen=require_frozen)
    assert result["effective_kb_ids"] == ["a", "b"]
    assert result["filtered_out"] == [
        *frozen_scope["filtered_out"],
        {
            "kb_id": "wiki",
            "reason": "WIKI_VERSION_NOT_FROZEN",
        },
    ]


async def test_require_frozen_rejects_wholly_unfrozen_member(wiki_db):
    from yuxi.services.wiki_runtime_service import freeze_scope_wikis

    scope = {
        "tenant_id": 7,
        "members": [{"kb_id": x, "kb_type": "milvus"} for x in ("a", "b")]
        + [{"kb_id": "wiki", "kb_type": "llmwiki", "wiki_navigation_enabled": True}],
    }
    result = await freeze_scope_wikis(wiki_db, snapshot=scope, require_frozen=True)
    assert result["effective_kb_ids"] == ["a", "b"]
    assert result["filtered_out"] == [{"kb_id": "wiki", "reason": "WIKI_VERSION_NOT_FROZEN"}]


@pytest.mark.parametrize(
    "change",
    [
        "source_deleted",
        "source_missing",
        "source_derived",
        "source_tenant",
        "binding",
        "source_acl",
        "narrowed",
        "disabled_source",
        "manifest",
        "snapshot",
        "source_identity",
        "wiki_identity",
        "publication_tenant",
        "wiki_tenant",
    ],
)
async def test_frozen_failure_only_removes_affected_wiki(wiki_db, frozen_scope, change):
    from sqlalchemy import select
    from yuxi.services.wiki_runtime_service import freeze_scope_wikis

    publication = (await wiki_db.execute(select(WikiPublication))).scalar_one()
    wiki = (await wiki_db.execute(select(KnowledgeWiki))).scalar_one()
    source = (await wiki_db.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == "b"))).scalar_one()
    # A second Wiki depends only on a, and must survive failures affecting b/the first Wiki.
    manifest = {"source_kb_ids": ["a"], "security_domain": wiki.security_domain}
    wiki_db.add_all(
        [
            KnowledgeWiki(
                id=2,
                wiki_id="w2",
                kb_id="wiki2",
                tenant_id=7,
                security_domain=wiki.security_domain,
                created_by="u",
                current_publication_id="p2",
                status="PUBLISHED",
            ),
            WikiPublication(
                id=2,
                publication_id="p2",
                wiki_id="w2",
                tenant_id=7,
                snapshot_id="s2",
                build_id="build2",
                status="ACTIVE",
                manifest_hash=_digest(manifest),
                manifest_json=manifest,
            ),
            WikiSourceBinding(
                id=3,
                binding_id="w2-a",
                wiki_id="w2",
                source_kb_id="a",
                security_domain=wiki.security_domain,
                created_by="u",
                enabled=True,
            ),
        ]
    )
    frozen_scope["members"].append(
        {
            "kb_id": "wiki2",
            "kb_type": "llmwiki",
            "wiki_navigation_enabled": True,
            "wiki_id": "w2",
            "publication_id": "p2",
            "manifest_hash": _digest(manifest),
            "snapshot_id": "s2",
            "wiki_source_kb_ids": ["a"],
        }
    )
    frozen_scope["effective_kb_ids"].append("wiki2")
    if change == "source_deleted":
        source.additional_params = {"deleted_at": "2026-09-01"}
    elif change == "source_missing":
        await wiki_db.delete(source)
    elif change == "source_derived":
        source.kb_type = "llmwiki"
    elif change == "source_tenant":
        source.tenant_id = 8
    elif change == "source_acl":
        source.share_config = {"access_level": "unknown"}
    elif change == "binding":
        (
            await wiki_db.execute(select(WikiSourceBinding).where(WikiSourceBinding.source_kb_id == "b"))
        ).scalar_one().enabled = False
    elif change == "narrowed":
        frozen_scope["members"].pop(1)
        frozen_scope["effective_kb_ids"].remove("b")
    elif change == "disabled_source":
        frozen_scope["members"][1]["enabled"] = False
    elif change == "manifest":
        publication.manifest_json = {**publication.manifest_json, "navigation_entries": []}
    elif change == "snapshot":
        frozen_scope["members"][-2]["snapshot_id"] = "other"
    elif change == "source_identity":
        frozen_scope["members"][-2]["wiki_source_kb_ids"] = ["a"]
    elif change == "wiki_identity":
        frozen_scope["members"][-2]["wiki_id"] = "other"
    elif change == "publication_tenant":
        publication.tenant_id = 8
    elif change == "wiki_tenant":
        wiki.tenant_id = 8
    await wiki_db.commit()
    before = deepcopy(frozen_scope)

    result = await freeze_scope_wikis(wiki_db, snapshot=frozen_scope, require_frozen=True)

    assert result["members"] == [item for item in before["members"] if item["kb_id"] != "wiki"]
    assert result["effective_kb_ids"] == [item["kb_id"] for item in result["members"]]
    assert result["filtered_out"][0] == before["filtered_out"][0]
    assert [item["kb_id"] for item in result["filtered_out"]] == ["unrelated", "wiki"]
    assert frozen_scope == before


@pytest.fixture
async def runtime_session(wiki_db, monkeypatch):
    from yuxi.services import wiki_runtime_service
    from yuxi.storage.postgres.models_business import User

    await wiki_db.run_sync(lambda db: User.__table__.create(db.connection()))
    user = User(account_scope_id="test-account", uid="u", username="test", password_hash="unused", role="user")
    wiki_db.add(user)
    wiki_db.add(
        KnowledgeBase(
            kb_id="wiki", name="wiki", kb_type="llmwiki", tenant_id=7, share_config={"access_level": "global"}
        )
    )
    await wiki_db.commit()

    @asynccontextmanager
    async def session():
        yield wiki_db

    monkeypatch.setattr(wiki_runtime_service, "navigation_session", session)
    principal = AsyncMock(return_value=7)
    monkeypatch.setattr("yuxi.services.principal.resolve_tenant_id", principal)
    return user, principal


@pytest.mark.parametrize(
    "change",
    ["superseded", "acl", "deleted", "disabled_user", "deleted_user", "unknown_user", "tenant", "unknown_tenant"],
)
async def test_resume_and_navigation_recheck_current_principal(wiki_db, frozen_scope, runtime_session, change):
    from sqlalchemy import select
    from yuxi.services.wiki_runtime_service import navigate_frozen_scope, reauthorize_scope
    from yuxi.services.principal import PrincipalResolutionError

    user, principal = runtime_session
    wiki = (await wiki_db.execute(select(KnowledgeWiki))).scalar_one()
    publication = (await wiki_db.execute(select(WikiPublication))).scalar_one()
    wiki.current_publication_id = "new"
    publication.status = "SUPERSEDED"
    uid = "u"
    if change in {"acl", "deleted"}:
        source = (await wiki_db.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == "b"))).scalar_one()
        if change == "acl":
            source.share_config = {"access_level": "user", "user_uids": ["someone-else"]}
        else:
            source.additional_params = {"deleted_at": "2026-09-01"}
    elif change == "disabled_user":
        user.is_disabled = True
    elif change == "deleted_user":
        user.is_deleted = 1
    elif change == "unknown_user":
        uid = "missing"
    elif change == "tenant":
        principal.return_value = 8
    elif change == "unknown_tenant":
        principal.side_effect = PrincipalResolutionError("unknown")
    await wiki_db.commit()

    result = await reauthorize_scope(snapshot=frozen_scope, uid=uid)
    hits, statuses = await navigate_frozen_scope(snapshot=frozen_scope, uid=uid, question="Waxy")
    if change == "superseded":
        assert result["members"] == frozen_scope["members"]
        assert result["filtered_out"] == frozen_scope["filtered_out"]
        assert hits and {hit["publication_id"] for hit in hits} == {"old"}
        assert statuses[0]["publication_id"] == "old"
    else:
        assert result["effective_kb_ids"] == (["a"] if change in {"acl", "deleted"} else [])
        assert not hits
        assert statuses[0]["capability_status"] == "UNAVAILABLE"


@pytest.mark.parametrize("missing_parent_sources", [False, True])
async def test_child_copies_frozen_sources_and_fails_closed_if_missing(wiki_db, frozen_scope, missing_parent_sources):
    from yuxi.services.wiki_runtime_service import freeze_scope_wikis

    parent = deepcopy(frozen_scope)
    child_scope = deepcopy(frozen_scope)
    child_scope["members"][-1].update(publication_id="new", wiki_source_kb_ids=["a"])
    if missing_parent_sources:
        parent["members"][-1].pop("wiki_source_kb_ids")
    child = narrow_child_scope_to_parent(child_scope, parent)
    member = child["members"][-1]
    if missing_parent_sources:
        assert "wiki_source_kb_ids" not in member
    else:
        assert member["wiki_source_kb_ids"] == ["b", "a"]
        assert member["wiki_source_kb_ids"] is not parent["members"][-1]["wiki_source_kb_ids"]
    result = await freeze_scope_wikis(wiki_db, snapshot=child, require_frozen=True)
    assert result["effective_kb_ids"] == (["a", "b"] if missing_parent_sources else ["a", "b", "wiki"])


def test_merge_preserves_both_paths_ranks_and_baseline_payload():
    baseline = {"evidence": [{"evidence_id": "a", "evidence_quote": "authority"}, {"evidence_id": "b"}]}
    guided = {"evidence": [{"evidence_id": "a", "evidence_quote": "do not overwrite"}, {"evidence_id": "c"}]}
    result = _merge_gateway_results(baseline, guided, limit=3)
    assert result["evidence"][0]["evidence_id"] == "a"
    assert result["evidence"][0]["evidence_quote"] == "authority"
    assert result["evidence"][0]["retrieval_paths"] == ["BASELINE", "WIKI_GUIDED"]
    assert result["evidence"][0]["retrieval_ranks"] == {"BASELINE": 1, "WIKI_GUIDED": 1}
    assert result["retrieval_summary"]["wiki_guided_unique_candidates"] == 1
    assert result["retrieval_summary"]["wiki_guided_unique_retained"] == 1
    assert result["retrieval_summary"]["wiki_overlap_count"] == 1
    assert "retrieval_paths" not in baseline["evidence"][0]


def test_child_inherits_parent_publication_and_navigation_capability():
    parent_member = {
        "kb_id": "wiki",
        "wiki_navigation_enabled": False,
        "wiki_id": "w",
        "publication_id": "old",
        "manifest_hash": "hash-old",
        "snapshot_id": "snapshot-old",
    }
    child_member = {**parent_member, "wiki_navigation_enabled": True, "publication_id": "new"}
    child = narrow_child_scope_to_parent(
        {"effective_kb_ids": ["wiki"], "members": [child_member]},
        {"effective_kb_ids": ["wiki"], "members": [parent_member]},
    )
    assert child["members"][0]["publication_id"] == "old"
    assert child["members"][0]["wiki_navigation_enabled"] is False
