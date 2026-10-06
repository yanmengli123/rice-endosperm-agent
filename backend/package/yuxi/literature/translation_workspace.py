"""Durable owner-private draft translations and explicit-cost adaptive probes.

These tasks never create LiteraturePaper, activate a parse revision, compile,
publish, or write evidence. Only derived bytes and task/accounting records.
"""

import hashlib
import json
import logging
import re
from datetime import timedelta

from sqlalchemy import select, func

from yuxi.literature.access import access_scope
from yuxi.literature.translate.settings import require_owner, translation_options, model_options, options_digest
from yuxi.literature.translate.service import (
    _translate_document,
    _is_refusal,
    renew_translation_lease,
    _newer_translation_submission,
)
from yuxi.literature.provenance import StaleLiteratureInput
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import LiteratureTranslationTask
from yuxi.utils.datetime_utils import utc_now

logger = logging.getLogger(__name__)
TASK_JOB = "process_literature_translation_task"
PROBE_SIZES = (3000, 6000, 12000, 24000, 48000)


async def enqueue_task(
    tenant_id: int,
    uid: str,
    *,
    source_kind: str,
    source_ref: str,
    task_kind: str = "draft",
    profile_id: str | None = None,
    cost_ack: bool = False,
) -> dict:
    require_owner(tenant_id, uid)
    if task_kind not in {"draft", "probe"} or source_kind not in {"draft", "paper", "auto"}:
        raise ValueError("无效翻译任务或来源类型")
    if task_kind == "draft" and source_kind != "draft":
        raise ValueError("草稿翻译只能使用仅解析来源")
    if task_kind == "probe" and not cost_ack:
        raise ValueError("自适应探测最多调用 5 次真实翻译，必须明确确认费用和内容传输")
    source, digest = await _source(tenant_id, source_kind, source_ref)
    options = await translation_options(tenant_id, uid, profile_id=profile_id)
    if task_kind == "draft":
        options = {**options, "source_title": source["title"]}
    async with pg_manager.get_async_session_context() as db:
        from yuxi.storage.postgres.models_business import User
        from yuxi.literature.llm_gateway import freeze_literature_policy

        await db.execute(select(User.uid).where(User.uid == uid).with_for_update())
        rows = list(
            (
                await db.execute(
                    select(LiteratureTranslationTask)
                    .where(
                        LiteratureTranslationTask.tenant_id == tenant_id,
                        LiteratureTranslationTask.requested_by == uid,
                        LiteratureTranslationTask.task_kind == task_kind,
                        LiteratureTranslationTask.source_kind == source_kind,
                        LiteratureTranslationTask.source_ref == source_ref,
                    )
                    .order_by(LiteratureTranslationTask.id.desc())
                    .limit(20)
                )
            ).scalars()
        )
        existing = next(
            (
                row
                for row in rows
                if row.source_digest == digest
                and row.options_json == options
                and row.status in ("PENDING", "RUNNING", "SUCCEEDED")
                and (task_kind == "probe" or row.id == rows[0].id)
                and (task_kind == "draft" or row.status != "SUCCEEDED")
            ),
            None,
        )
        if existing:
            return {"task_id": existing.id, "status": existing.status, "reused": True}
        policy = await freeze_literature_policy(
            db,
            tenant_id=tenant_id,
            uid=uid,
            operation=f"translation_{task_kind}",
            resource_ref=f"{uid}:{task_kind}:{source_ref}:{digest}:{options_digest(options)}",
            model_spec=options["model_spec"],
        )
        row = LiteratureTranslationTask(
            tenant_id=tenant_id,
            requested_by=uid,
            task_kind=task_kind,
            source_kind=source_kind,
            source_ref=source_ref,
            source_digest=digest,
            options_json=options,
            llm_run_id=policy.run_id,
            model_spec=policy.model_spec,
            credential_source=policy.credential_source,
            credential_id=policy.credential_id,
            provider_id=policy.provider_id,
            policy_version=policy.policy_version,
        )
        db.add(row)
        await db.flush()
        task_id = row.id
        await db.commit()
    queued = await _dispatch(task_id)
    return {"task_id": task_id, "status": "PENDING", "reused": False, "enqueue_deferred": not queued}


async def get_task(tenant_id: int, uid: str, task_id: int, *, content: bool = False) -> dict:
    require_owner(tenant_id, uid)
    async with pg_manager.get_async_session_context() as db:
        row = (
            await db.execute(
                select(LiteratureTranslationTask).where(
                    LiteratureTranslationTask.id == task_id,
                    LiteratureTranslationTask.tenant_id == tenant_id,
                    LiteratureTranslationTask.requested_by == uid,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        raise ValueError("翻译任务不存在或无权访问")
    _, digest = await _source(tenant_id, row.source_kind, row.source_ref)
    if digest != row.source_digest:
        raise ValueError("任务来源正文已变更，请重新提交")
    result = {
        "task_id": row.id,
        "status": row.status,
        "task_kind": row.task_kind,
        "source_ref": row.source_ref,
        "attempt": row.attempt,
        "result": row.result_json,
        "error": row.error,
        "model_spec": row.model_spec,
        "options": row.options_json,
        "evidence": False,
    }
    if content:
        if row.status != "SUCCEEDED" or not row.variant_uri:
            raise ValueError("草稿译文尚未完成")
        data = await get_minio_client().adownload_file(RENDER_BUCKET, row.variant_uri.removeprefix("kbasset://"))
        if hashlib.sha256(data).hexdigest() != row.content_digest:
            raise ValueError("草稿译文内容校验失败")
        if (await _source(tenant_id, row.source_kind, row.source_ref))[1] != digest:
            raise PermissionError("读取期间草稿版本或权限已变更")
        variant = json.loads(data.decode("utf-8"))
        result["markdown"] = variant["markdown"]
        result["alignment"] = variant["alignment"]
    return result


async def latest_draft_task(tenant_id: int, uid: str, revision_id: str) -> dict | None:
    require_owner(tenant_id, uid)
    # Source authorization precedes even task metadata visibility.
    _, digest = await _source(tenant_id, "draft", revision_id)
    async with pg_manager.get_async_session_context() as db:
        task_id = (
            await db.execute(
                select(LiteratureTranslationTask.id)
                .where(
                    LiteratureTranslationTask.tenant_id == tenant_id,
                    LiteratureTranslationTask.requested_by == uid,
                    LiteratureTranslationTask.task_kind == "draft",
                    LiteratureTranslationTask.source_ref == revision_id,
                    LiteratureTranslationTask.source_digest == digest,
                )
                .order_by(LiteratureTranslationTask.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return await get_task(tenant_id, uid, task_id) if task_id is not None else None


async def process_literature_translation_task(ctx: dict, task_id: int) -> dict | None:
    async with pg_manager.get_async_session_context() as db:
        row = await db.get(LiteratureTranslationTask, task_id, with_for_update=True)
        if row is None or row.status != "PENDING":
            return None
        row.status, row.updated_at = "RUNNING", utc_now()
        attempt = row.attempt
        await db.commit()
    try:
        from yuxi.literature.workflow import _request_user
        from yuxi.literature.llm_gateway import governed_runtime, policy_for_job_attempt

        user = await _request_user(row)
        async with access_scope(user, int(row.tenant_id)):
            source, digest = await _source(row.tenant_id, row.source_kind, row.source_ref)
            if digest != row.source_digest:
                raise PermissionError("翻译输入已变更")
            paragraphs = _paragraphs(source["markdown"])
            if not paragraphs:
                raise ValueError("正文没有可翻译段落")
            policy = await policy_for_job_attempt(row, operation=f"translation_{row.task_kind}")
            async with governed_runtime(policy, model_options=model_options(row.options_json)):
                if row.task_kind == "probe":
                    result = await _probe(paragraphs, task_id, attempt)
                    uri, content_digest = None, None
                else:
                    translations = await _translate_document(
                        paragraphs,
                        max_chars=row.options_json["batch_chars"],
                        checkpoint=lambda: renew_translation_lease(row, attempt),
                    )
                    markdown = f"# {source['title']}\n\n" + "\n\n".join(translations) + "\n"
                    data = json.dumps(
                        {
                            "schema": "draft-translation-v1",
                            "markdown": markdown,
                            "alignment": [
                                {"para_id": f"P{i:03d}", "original": original, "translated": translated}
                                for i, (original, translated) in enumerate(
                                    zip(paragraphs, translations, strict=True), 1
                                )
                            ],
                        },
                        ensure_ascii=False,
                    ).encode()
                    content_digest = hashlib.sha256(data).hexdigest()
                    uri = f"kbasset://tenants/{row.tenant_id}/literature/draft_variants/{row.requested_by}/{task_id}/{content_digest}.json"
                    await get_minio_client().aupload_file(
                        bucket_name=RENDER_BUCKET,
                        object_name=uri.removeprefix("kbasset://"),
                        data=data,
                        content_type="application/json",
                    )
                    result = {"paragraphs": len(paragraphs), "evidence": False}
            # Rebuild the user's access after external calls; no stale scope publication.
        async with access_scope(user, int(row.tenant_id)):
            if (await _source(row.tenant_id, row.source_kind, row.source_ref))[1] != row.source_digest:
                raise PermissionError("翻译期间来源版本或权限已变更，未发布")
        async with pg_manager.get_async_session_context() as db:
            fresh = await db.get(LiteratureTranslationTask, task_id, with_for_update=True)
            if fresh is None or fresh.status != "RUNNING" or fresh.attempt != attempt:
                return {"task_id": task_id, "status": "STALE"}
            if row.task_kind == "draft":
                newer = (await db.execute(_newer_translation_submission(row))).scalar_one_or_none()
                if newer is not None:
                    fresh.status, fresh.error = "STALE", "已有较新的草稿翻译任务，旧结果未发布"
                    await db.commit()
                    return {"task_id": task_id, "status": "STALE"}
            fresh.status, fresh.result_json = "SUCCEEDED", result
            fresh.variant_uri, fresh.content_digest, fresh.error = uri, content_digest, None
            await db.commit()
        return {"task_id": task_id, "status": "SUCCEEDED"}
    except Exception as exc:  # noqa: BLE001 - bounded durable retry, sanitized public errors
        from yuxi.literature.llm_gateway import fail_unstarted_run

        await fail_unstarted_run(row.llm_run_id, error_type=type(exc).__name__)
        async with pg_manager.get_async_session_context() as db:
            fresh = await db.get(LiteratureTranslationTask, task_id, with_for_update=True)
            if fresh is None or fresh.status != "RUNNING" or fresh.attempt != attempt:
                return {"task_id": task_id, "status": "STALE"}
            fresh.attempt += 1
            fresh.status = (
                "STALE"
                if isinstance(exc, (PermissionError, StaleLiteratureInput))
                else ("DEAD" if row.task_kind == "probe" or fresh.attempt >= 3 else "PENDING")
            )
            fresh.error = f"{type(exc).__name__}: 翻译任务失败，请检查来源权限、模型和存储"
            fresh.next_retry_at = utc_now() + timedelta(minutes=2 ** min(fresh.attempt, 3))
            await db.commit()
            status = fresh.status
        logger.warning("翻译工作台任务失败: task=%s error=%s", task_id, type(exc).__name__)
        return {"task_id": task_id, "status": status}


async def reconcile_literature_translation_tasks(ctx=None) -> int:
    async with pg_manager.get_async_session_context() as db:
        # Probes must never silently multiply the explicit five-call budget.
        stale = (
            (
                await db.execute(
                    select(LiteratureTranslationTask)
                    .where(
                        LiteratureTranslationTask.status == "RUNNING",
                        LiteratureTranslationTask.updated_at < utc_now() - timedelta(minutes=30),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        for row in stale:
            row.attempt += 1
            row.status = "DEAD" if row.task_kind == "probe" or row.attempt >= 3 else "PENDING"
            row.error = "执行租约失效；探测需显式重新提交" if row.task_kind == "probe" else "执行租约失效，等待重试"
        await db.commit()
        ids = list(
            (
                await db.execute(
                    select(LiteratureTranslationTask.id)
                    .where(
                        LiteratureTranslationTask.status == "PENDING",
                        (LiteratureTranslationTask.next_retry_at.is_(None))
                        | (LiteratureTranslationTask.next_retry_at <= utc_now()),
                    )
                    .order_by(LiteratureTranslationTask.id)
                    .limit(100)
                )
            ).scalars()
        )
    return sum([int(await _dispatch(task_id)) for task_id in ids])


async def _source(tenant_id: int, kind: str, ref: str) -> tuple[dict, str]:
    if kind == "auto":
        sources = await probe_sources(tenant_id)
        if not sources:
            raise ValueError("当前权限内没有已解析的探测语料")
        parts, identities, pool_bytes = [], [], 0
        for item in sources:
            document, digest = await _source(tenant_id, item["source_kind"], item["source_ref"])
            paragraphs = _paragraphs(document["markdown"])
            pool_bytes += sum(len(paragraph.encode()) + 2 for paragraph in paragraphs)
            if pool_bytes > 5 * 1024 * 1024:
                raise ValueError("探测语料池超过 5 MiB，请选择单篇来源")
            parts.extend(paragraphs)
            identities.append([item["source_kind"], item["source_ref"], digest])
        source = {"title": "当前有权已解析语料（最多 30 篇）", "markdown": "\n\n".join(parts)}
        if len(source["markdown"].encode()) > 5 * 1024 * 1024:
            raise ValueError("探测语料池超过 5 MiB，请选择单篇来源")
        return source, hashlib.sha256(json.dumps(identities, sort_keys=True).encode()).hexdigest()
    if kind == "draft":
        from yuxi.literature.drafts import read_draft

        source = await read_draft(tenant_id, ref)
    else:
        from yuxi.literature.reader_bundle import get_reader_bundle
        from yuxi.literature.access import current_access, build_access

        scope = current_access()
        if scope is None or ref not in (await build_access(scope.user, tenant_id)).rids:
            raise PermissionError("探测来源无权访问")
        source = await get_reader_bundle(tenant_id, ref)
        if not source or not source.get("ready"):
            raise ValueError("来源阅读正文尚未准备好")
    if len(source["markdown"].encode()) > 5 * 1024 * 1024:
        raise ValueError("翻译来源超过 5 MiB")
    return source, hashlib.sha256(
        json.dumps(
            [kind, ref, source.get("revision_id"), source["title"], source["markdown"]], ensure_ascii=False
        ).encode()
    ).hexdigest()


async def probe_sources(tenant_id: int) -> list[dict]:
    """Bounded corpus inventory sorted by size, ACL before any text reads."""
    from yuxi.literature.drafts import list_drafts
    from yuxi.literature.access import paper_visibility
    from yuxi.literature.original_search import current_projection
    from yuxi.storage.postgres.models_knowledge import (
        LiteraturePaper,
        LiteratureOriginalSearch,
        KnowledgeParseArtifact,
    )

    drafts = {item["revision_id"]: item for item in await list_drafts(tenant_id) if item["ready"]}
    async with pg_manager.get_async_session_context() as db:
        papers = (
            await db.execute(
                select(
                    LiteraturePaper.rid, LiteraturePaper.title, func.length(LiteratureOriginalSearch.body).label("size")
                )
                .join(LiteratureOriginalSearch, LiteratureOriginalSearch.paper_id == LiteraturePaper.id)
                .where(paper_visibility(tenant_id), current_projection())
                .order_by(func.length(LiteratureOriginalSearch.body).desc(), LiteraturePaper.rid)
                .limit(30)
            )
        ).all()
        parsed = (
            await db.execute(
                select(KnowledgeParseArtifact.revision_id, KnowledgeParseArtifact.size_bytes)
                .where(
                    KnowledgeParseArtifact.revision_id.in_(drafts), KnowledgeParseArtifact.kind == "indexed_markdown"
                )
                .order_by(KnowledgeParseArtifact.size_bytes.desc(), KnowledgeParseArtifact.revision_id)
                .limit(30)
            )
        ).all()
    sources = [
        {"source_kind": "paper", "source_ref": rid, "title": title or rid, "size": size} for rid, title, size in papers
    ]
    sources.extend(
        {"source_kind": "draft", "source_ref": revision, "title": drafts[revision]["title"], "size": size}
        for revision, size in parsed
    )
    return sorted(sources, key=lambda item: (-item["size"], item["source_kind"], item["source_ref"]))[:30]


def _paragraphs(markdown: str) -> list[str]:
    # Same corpus-only discipline as upstream probe: no section labels, image
    # paths or reference list in the test input; retain complete body paragraphs.
    paragraphs = []
    for block in re.split(r"\n\s*\n", markdown):
        text = block.strip()
        if re.match(r"^#{1,6}\s*(references|bibliography|参考文献)\b", text, re.I):
            break
        if text and not text.startswith(("#", "![")):
            paragraphs.append(text)
    return paragraphs


async def _probe(paragraphs: list[str], task_id: int, attempt: int) -> dict:
    from yuxi.literature.llm_gateway import call
    from yuxi.literature.notes.prompts import parse_compile_json
    from yuxi.literature.translate import prompts

    trials, passed = [], 0
    for size in PROBE_SIZES:
        sample, chars = [], 0
        for paragraph in paragraphs:
            if len(paragraph) > size:
                continue
            if chars + len(paragraph) > size:
                break
            sample.append(paragraph)
            chars += len(paragraph)
        # Never claim 48K capacity from a 3K source. Not enough corpus is distinct
        # from a failed model trial, and produces no API call.
        if chars < int(size * 0.8):
            trials.append({"batch_chars": size, "status": "INSUFFICIENT_CORPUS", "input_chars": chars})
            break
        prefix, _ = prompts.build_shared_prefix_with_math(sample)
        ids = [f"P{i:03d}" for i in range(1, len(sample) + 1)]
        system, user = prompts.split_task(prompts.with_task(prefix, prompts.translate_task(ids)))
        try:
            response = await call([{"role": "system", "content": system}, {"role": "user", "content": user}])
            if str((response.response_metadata or {}).get("finish_reason") or "").lower() in {"length", "max_tokens"}:
                raise ValueError("output_truncated")
            data = parse_compile_json(str(response.content or ""))
            translations = {
                str(item.get("para_id")): str(item.get("zh") or "")
                for item in data.get("translations", [])
                if isinstance(item, dict)
            }
            if len(translations) != len(data.get("translations", [])) or set(translations) != set(ids):
                raise ValueError("paragraph_identity_mismatch")
            labeled, _ = prompts.label_paragraphs(sample)
            if any(
                not translations.get(pid)
                or _is_refusal(translations[pid])
                or re.findall(r"\[\[MATH\d+\]\]", translations[pid]) != re.findall(r"\[\[MATH\d+\]\]", text)
                for pid, text in zip(ids, labeled, strict=True)
            ):
                raise ValueError("paragraph_or_math_missing")
            trial = {"batch_chars": size, "status": "PASSED", "input_chars": chars, "paragraphs": len(ids)}
            passed = size
        except Exception as exc:  # noqa: BLE001 - record first failure without raw model/credential payloads
            trial = {"batch_chars": size, "status": "FAILED", "input_chars": chars, "error_code": type(exc).__name__}
        trials.append(trial)
        async with pg_manager.get_async_session_context() as db:
            row = await db.get(LiteratureTranslationTask, task_id, with_for_update=True)
            if row is None or row.status != "RUNNING" or row.attempt != attempt:
                raise PermissionError("探测任务租约已变更")
            row.result_json = {
                "trials": trials,
                "suggested_batch_chars": max(1000, int(passed * 0.6)) if passed else None,
            }
            await db.commit()
        if trial["status"] == "FAILED":
            break
    return {
        "trials": trials,
        "suggested_batch_chars": max(1000, int(passed * 0.6)) if passed else None,
        "calls": sum(t["status"] in {"PASSED", "FAILED"} for t in trials),
        "auto_saved": False,
    }


async def _dispatch(task_id: int) -> bool:
    from yuxi.services.run_queue_service import get_arq_pool

    try:
        queue = await get_arq_pool()
        slot = int(utc_now().timestamp()) // 60
        await queue.enqueue_job(TASK_JOB, task_id, _job_id=f"lit-translate-task:{task_id}:{slot}")
        return True
    except Exception:  # noqa: BLE001 - durable PENDING with reconciler
        return False
