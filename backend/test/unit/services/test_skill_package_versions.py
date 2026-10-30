from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.agents.skills import edit, service
from yuxi.storage.postgres.models_business import Agent, Base, Skill, SkillVersion, User


@pytest.fixture
async def skill_package(tmp_path, monkeypatch):
    monkeypatch.setattr(service.sys_config, "save_dir", str(tmp_path))

    async def no_mcps(**_kwargs):
        return []

    monkeypatch.setattr(service, "get_enabled_mcp_server_slugs", no_mcps)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda conn: Base.metadata.create_all(
                conn, tables=[User.__table__, Agent.__table__, Skill.__table__, SkillVersion.__table__]
            )
        )
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        db.add(
            Agent(
                id=1,
                tenant_id=1,
                slug="research",
                backend_id="chatbot",
                name="研究助手",
                created_by="owner",
                share_config={"access_level": "user", "user_uids": ["owner"]},
            )
        )
        await db.commit()
        actor = SimpleNamespace(uid="owner", role="superadmin", department_id=1)
        state = await edit.bound_skill(db, operator=actor, agent_slug="research", create=True)
        yield db, actor, state
    await engine.dispose()


async def test_complete_snapshot_restore_and_stale_editor_conflict(skill_package):
    db, actor, state = skill_package
    slug, revision = state["skill"]["slug"], state["revision"]
    version = await edit.release_version(db, slug=slug, operator=actor, expected_revision=revision)
    changed = await edit.save_package(
        db, slug=slug, operator=actor, expected_revision=revision, files={"notes/method.md": "new method"}
    )
    assert changed["revision"] != revision
    with pytest.raises(edit.SkillEditConflict):
        await edit.save_package(
            db, slug=slug, operator=actor, expected_revision=revision, files={"notes/method.md": "stale overwrite"}
        )
    await db.rollback()
    current = await edit.package_state(db, slug=slug, operator=actor)
    restored = await edit.save_package(
        db, slug=slug, operator=actor, expected_revision=current["revision"], restore_version=version["version"]
    )
    assert restored["revision"] == revision
    item = (await db.execute(select(Skill).where(Skill.slug == slug))).scalar_one()
    assert not (service._resolve_skill_dir(item) / "notes/method.md").exists()
    assert len(await edit.list_versions(db, slug=slug, operator=actor)) == 1


async def test_rejected_path_never_changes_published_skill(skill_package):
    db, actor, state = skill_package
    slug = state["skill"]["slug"]
    with pytest.raises(ValueError):
        await edit.save_package(
            db, slug=slug, operator=actor, expected_revision=state["revision"], files={"../escape.txt": "escape"}
        )
    current = await edit.package_state(db, slug=slug, operator=actor)
    assert current["revision"] == state["revision"]
    assert current["skill"]["dir_path"] == state["skill"]["dir_path"]


async def test_corrupt_snapshot_is_rejected(skill_package):
    db, actor, state = skill_package
    slug = state["skill"]["slug"]
    version = await edit.release_version(db, slug=slug, operator=actor, expected_revision=state["revision"])
    row = (await db.execute(select(SkillVersion))).scalar_one()
    (Path(service.sys_config.save_dir) / row.dir_path / "SKILL.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="校验失败"):
        await edit.save_package(
            db, slug=slug, operator=actor, expected_revision=state["revision"], restore_version=version["version"]
        )


def test_directory_revision_rejects_symlinks_and_tracks_all_files(tmp_path):
    (tmp_path / "SKILL.md").write_text("content", encoding="utf-8")
    previous = edit.directory_revision(tmp_path)
    (tmp_path / "helper.py").write_text("print('new')", encoding="utf-8")
    assert edit.directory_revision(tmp_path) != previous
    (tmp_path / "alias").symlink_to(tmp_path / "SKILL.md")
    with pytest.raises(ValueError, match="符号链接"):
        edit.directory_revision(tmp_path)


async def test_legacy_create_and_delete_publish_complete_packages(skill_package):
    db, actor, state = skill_package
    slug = state["skill"]["slug"]
    created = await edit.save_package(
        db,
        slug=slug,
        operator=actor,
        expected_revision=state["revision"],
        files={"scripts/test.py": "print('method')"},
        create_paths=["scripts/test.py"],
    )
    assert created["skill"]["dir_path"] != state["skill"]["dir_path"]
    assert (Path(service.sys_config.save_dir) / state["skill"]["dir_path"] / "SKILL.md").exists()
    with pytest.raises(ValueError, match="已存在"):
        await edit.save_package(
            db,
            slug=slug,
            operator=actor,
            expected_revision=created["revision"],
            files={"scripts/test.py": "overwrite"},
            create_paths=["scripts/test.py"],
        )
    current = await edit.package_state(db, slug=slug, operator=actor)
    deleted = await edit.save_package(
        db, slug=slug, operator=actor, expected_revision=current["revision"], deletions=["scripts"]
    )
    assert not (Path(service.sys_config.save_dir) / deleted["skill"]["dir_path"] / "scripts").exists()


async def test_frontmatter_and_dependency_form_conflicts_are_explicit(skill_package):
    db, actor, state = skill_package
    root = Path(service.sys_config.save_dir) / state["skill"]["dir_path"]
    changed = (
        (root / "SKILL.md")
        .read_text(encoding="utf-8")
        .replace("tool_dependencies: []", "tool_dependencies: [calculator]")
    )
    with pytest.raises(ValueError, match="修改不一致"):
        await edit.save_package(
            db,
            slug=state["skill"]["slug"],
            operator=actor,
            expected_revision=state["revision"],
            files={"SKILL.md": changed},
            dependencies={"tool_dependencies": [], "mcp_dependencies": [], "skill_dependencies": []},
        )
    assert (await edit.package_state(db, slug=state["skill"]["slug"], operator=actor))["revision"] == state["revision"]
