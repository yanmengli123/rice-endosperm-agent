"""Publish validated Skill packages by changing one database content reference."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import uuid
from pathlib import Path

import yaml
from sqlalchemy import select

from yuxi.agents.skills import service
from yuxi.storage.postgres.models_business import Agent, Skill, SkillVersion
from yuxi.utils.datetime_utils import format_utc_datetime, utc_now

MAX_PACKAGE_BYTES = 50 * 1024 * 1024
MAX_PACKAGE_FILES = 2000


class SkillEditConflict(ValueError):
    pass


def directory_revision(root: Path) -> str:
    """Hash paths, bytes and executable bits; refuse links and oversized trees."""
    digest = hashlib.sha256()
    total = count = 0
    if root.is_symlink() or not root.is_dir():
        raise ValueError("技能目录不存在或不是普通目录")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("技能内容不允许符号链接")
        if not path.is_file():
            continue
        count += 1
        total += path.stat().st_size
        if count > MAX_PACKAGE_FILES or total > MAX_PACKAGE_BYTES:
            raise ValueError("技能内容超过 2000 个文件或 50 MiB")
        digest.update(json.dumps([path.relative_to(root).as_posix(), bool(path.stat().st_mode & 0o111)], ensure_ascii=False).encode())
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(65536), b""):
                digest.update(block)
    return digest.hexdigest()


async def package_state(db, *, slug: str, operator) -> dict:
    item = await service.get_management_readable_skill_or_raise(db, operator, slug)
    return {"skill": item.to_dict(), "revision": await asyncio.to_thread(directory_revision, service._resolve_skill_dir(item))}


async def _locked_skill(db, slug, operator, expected_revision):
    item = await service.get_manageable_skill_or_raise(db, operator, slug)
    service._ensure_non_builtin(item)
    await db.execute(select(Skill.id).where(Skill.id == item.id).with_for_update())
    await db.refresh(item)
    root = service._resolve_skill_dir(item)
    revision = await asyncio.to_thread(directory_revision, root)
    if not expected_revision or revision != expected_revision:
        raise SkillEditConflict("技能内容已变化，请重新加载；当前草稿未保存")
    return item, root


def _prepare_package(root, destination, changes, deletions, dependencies):
    directory_revision(root)
    shutil.copytree(root, destination)
    for relative in deletions:
        target, _ = service._resolve_relative_path(destination, relative)
        if target.name == "SKILL.md" and target.parent == destination:
            raise ValueError("不能删除根 SKILL.md")
        if not target.is_file():
            raise ValueError("仅支持删除普通文件")
        target.unlink()
    for relative, content in changes.items():
        target, _ = service._resolve_relative_path(destination, relative)
        if not service._is_text_path(target) or not isinstance(content, str):
            raise ValueError("仅支持 UTF-8 文本文件")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    root_file = destination / "SKILL.md"
    content = root_file.read_text(encoding="utf-8")
    slug, name, description, metadata = service._parse_skill_markdown(content)
    if dependencies is not None:
        metadata.update(dependencies)
        _, body = service._split_frontmatter(content)
        root_file.write_text("---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False) + "---\n" + body, encoding="utf-8")
    for key in ("tool_dependencies", "mcp_dependencies", "skill_dependencies"):
        if not isinstance(metadata.get(key, []), list):
            raise ValueError(f"{key} 必须是列表")
    return slug, name, description, metadata, directory_revision(destination)


async def save_package(db, *, slug, operator, expected_revision, files=None, deletions=None, dependencies=None, restore_version=None):
    item, root = await _locked_skill(db, slug, operator, expected_revision)
    if restore_version is not None:
        if item.bound_agent_id is None:
            raise ValueError("历史恢复仅适用于 Agent 专属 Skill")
        snapshot = (await db.execute(select(SkillVersion).where(SkillVersion.skill_id == item.id, SkillVersion.tenant_id == item.tenant_id, SkillVersion.version == restore_version))).scalar_one_or_none()
        if snapshot is None:
            raise ValueError("历史版本不存在")
        root = Path(service.sys_config.save_dir) / snapshot.dir_path
        if await asyncio.to_thread(directory_revision, root) != snapshot.content_hash:
            raise ValueError("历史版本内容校验失败")
    destination = service.get_skills_root_dir() / ".packages" / item.slug / uuid.uuid4().hex
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        new_slug, name, description, metadata, revision = await asyncio.to_thread(_prepare_package, root, destination, files or {}, deletions or [], dependencies)
        if new_slug != item.slug:
            raise ValueError("SKILL.md 的 slug 不能更改")
        visible = {skill.slug: skill for skill in await service.list_accessible_skills(db, operator)}
        tools, mcps, skills = await service._validate_dependencies(parent=item, available_skills=visible, **{key: metadata.get(key, []) for key in ("tool_dependencies", "mcp_dependencies", "skill_dependencies")})
        item.name, item.description = name, description
        item.tool_dependencies, item.mcp_dependencies, item.skill_dependencies = tools, mcps, skills
        item.dir_path = destination.relative_to(Path(service.sys_config.save_dir)).as_posix()
        item.content_hash = revision
        item.updated_by = str(operator.uid)
        await db.commit()
    except Exception:
        await db.rollback()
        # A commit error can be ambiguous. Keep the immutable package recoverable.
        raise
    return {"skill": item.to_dict(), "revision": revision}


async def list_versions(db, *, slug, operator):
    item = await service.get_management_readable_skill_or_raise(db, operator, slug)
    if item.bound_agent_id is None:
        raise ValueError("历史版本仅适用于 Agent 专属 Skill")
    rows = (await db.execute(select(SkillVersion).where(SkillVersion.skill_id == item.id, SkillVersion.tenant_id == item.tenant_id).order_by(SkillVersion.created_at.desc()))).scalars().all()
    return [{"version": row.version, "revision": row.content_hash, "created_by": row.created_by, "created_at": format_utc_datetime(row.created_at)} for row in rows]


async def release_version(db, *, slug, operator, expected_revision):
    item, root = await _locked_skill(db, slug, operator, expected_revision)
    if item.bound_agent_id is None:
        raise ValueError("历史版本仅适用于 Agent 专属 Skill")
    version = utc_now().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:12]
    destination = service.get_skills_root_dir() / ".history" / item.slug / version
    destination.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(shutil.copytree, root, destination)
    revision = await asyncio.to_thread(directory_revision, destination)
    db.add(SkillVersion(tenant_id=item.tenant_id, skill_id=item.id, version=version, content_hash=revision, dir_path=destination.relative_to(Path(service.sys_config.save_dir)).as_posix(), created_by=str(operator.uid)))
    await db.commit()
    return {"version": version, "revision": revision}


async def require_bound_agent(db, *, operator, agent_id=None, agent_slug=None, manage=False):
    from yuxi.repositories.agent_repository import resolve_creator_department, user_can_access_agent, user_can_manage_agent
    from yuxi.services.principal import resolve_tenant_id

    query = select(Agent).where(Agent.id == agent_id) if agent_id is not None else select(Agent).where(Agent.slug == agent_slug)
    agent = (await db.execute(query.with_for_update() if manage else query)).scalar_one_or_none()
    if agent is None or (operator.role != "superadmin" and agent.tenant_id != await resolve_tenant_id(db, str(operator.uid))):
        raise ValueError("智能体不存在或无权访问")
    department = await resolve_creator_department(db, agent.created_by) if manage else None
    permitted = user_can_manage_agent(operator, agent, creator_department_id=department) if manage else user_can_access_agent(operator, agent)
    if not permitted:
        raise ValueError("智能体不存在或无权访问")
    return agent


async def bound_skill(db, *, operator, agent_slug, create=False):
    agent = await require_bound_agent(db, operator=operator, agent_slug=agent_slug, manage=create)
    item = (await db.execute(select(Skill).where(Skill.bound_agent_id == agent.id))).scalar_one_or_none()
    if item is None and create:
        slug = "self-" + uuid.uuid4().hex[:16]
        directory = service.get_skills_root_dir() / ".packages" / slug / uuid.uuid4().hex
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("---\n" + yaml.safe_dump({"name": agent.name + "操作指南", "slug": slug, "description": "此智能体的专属工作指南", "tool_dependencies": [], "mcp_dependencies": [], "skill_dependencies": []}, allow_unicode=True, sort_keys=False) + "---\n\n# 操作指南\n\n在此维护可复用的工作步骤和验收要求。\n", encoding="utf-8")
        item = Skill(tenant_id=agent.tenant_id, bound_agent_id=agent.id, slug=slug, name=agent.name + "操作指南", description="此智能体的专属工作指南", source_type="upload", dir_path=directory.relative_to(Path(service.sys_config.save_dir)).as_posix(), content_hash=await asyncio.to_thread(directory_revision, directory), tool_dependencies=[], mcp_dependencies=[], skill_dependencies=[], share_config=agent.share_config or {}, created_by=str(operator.uid), updated_by=str(operator.uid), enabled=True)
        db.add(item)
        await db.commit()
        await db.refresh(item)
    if item is None:
        return {"skill": None, "revision": None}
    return {"skill": item.to_dict(), "revision": await asyncio.to_thread(directory_revision, service._resolve_skill_dir(item))}
