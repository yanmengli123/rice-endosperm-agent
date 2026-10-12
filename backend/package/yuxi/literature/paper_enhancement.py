"""Server-owned paper options and governed evidence scoring; never imported by knowledge."""

from __future__ import annotations

import asyncio
import math
import uuid
from typing import Any

from yuxi.literature import llm_gateway
from yuxi.literature.session import validate_frozen_paper_scope
from yuxi.models.providers.cache import model_cache
from yuxi.storage.postgres.manager import pg_manager


def freeze_enhancement_options(snapshot: dict, app_config) -> dict:
    if not snapshot.get("literature_binding") or "literature_enhancement" in snapshot:
        return snapshot
    return {
        **snapshot,
        "literature_enhancement": {
            "version": "paper-enhancement.v2",
            "deepread_enabled": bool(app_config.literature_deepread_enabled),
            "coreference_enabled": bool(app_config.literature_coreference_enabled),
            "rerank_enabled": bool(app_config.literature_rerank_enabled),
            "rerank_model": str(app_config.reranker or ""),
        },
    }


async def freeze_rerank_credentials(db, snapshot: dict, *, uid: str, credential_policy: str, policy_version) -> dict:
    """Freeze references, not secrets; an unavailable optional reranker never falls back to platform BYOK."""
    options = snapshot.get("literature_enhancement") or {}
    if not options.get("rerank_enabled") or "rerank_credentials" in options or options.get("rerank_unavailable"):
        return snapshot
    from yuxi.services.user_credential_service import get_active_user_credential

    options = dict(options)
    model_spec = options["rerank_model"]
    info = model_cache.get_model_info(model_spec)
    if info is None or info.model_type != "rerank":
        options["rerank_unavailable"] = "model_unavailable"
    else:
        provider = model_spec.split(":", 1)[0]
        credential = (
            await get_active_user_credential(db, uid, provider) if credential_policy != "platform_only" else None
        )
        if credential_policy == "byok_required" and credential is None:
            options["rerank_unavailable"] = "byok_required"
        else:
            options["rerank_credentials"] = {
                "credential_source": "user_byok" if credential else "platform",
                "credential_id": int(credential.id) if credential else None,
                "provider_id": provider,
                "credential_policy": credential_policy,
                "policy_version": policy_version,
            }
    return {**snapshot, "literature_enhancement": options}


async def score_paper_evidence(
    question: str,
    evidence: list[dict[str, Any]],
    *,
    user,
    tenant_id: int,
    snapshot: dict,
    run_id: str | None,
) -> list[float]:
    """Optional provider failure may degrade; source revocation/version changes always propagate."""
    options = snapshot.get("literature_enhancement") or {}
    if options.get("rerank_unavailable") or not options.get("rerank_credentials"):
        raise ValueError("paper reranker has no frozen credential policy")
    binding = snapshot["literature_binding"]
    await validate_frozen_paper_scope(user=user, tenant_id=tenant_id, snapshot=snapshot)
    if binding.get("kb_id") not in snapshot.get("effective_kb_ids", []) or binding.get("file_id") not in (
        snapshot.get("document_scope_ceiling") or {}
    ).get("file_ids", []):
        raise PermissionError("本文重排超出运行作用域")
    for row in evidence:
        provenance = row.get("source_provenance") or {}
        revision_id = row.get("parse_revision_id") or provenance.get("parse_revision_id")
        if (row.get("kb_id"), row.get("file_id"), revision_id) != (
            binding.get("kb_id"),
            binding.get("file_id"),
            binding.get("parse_revision_id"),
        ):
            raise PermissionError("本文重排证据与冻结文献版本不一致")
    documents = [
        str(row.get("content") or row.get("evidence_quote") or row.get("quote") or row.get("text") or "")[:4000]
        for row in evidence
    ]
    query = question[:2000]
    policy = None
    try:
        async with pg_manager.get_async_session_context() as db:
            policy = await llm_gateway.freeze_literature_policy(
                db,
                tenant_id=tenant_id,
                uid=user.uid,
                operation="paper_evidence_rerank",
                resource_ref=f"{run_id}:rerank:{uuid.uuid4().hex}",
                model_spec=options["rerank_model"],
                model_type="rerank",
                frozen_credentials=options["rerank_credentials"],
            )
            await db.commit()
        async with llm_gateway.governed_runtime(policy):
            from yuxi.agents.models import apply_credential_override
            from yuxi.models.rerank import DashscopeReranker, OpenAIReranker

            info = model_cache.get_model_info(policy.model_spec)
            if info is None or info.model_type != "rerank":
                raise ValueError("冻结的重排模型不可用")
            # The parent chat may have activated another BYOK key for this same
            # provider. Only the child run's frozen policy may select credentials.
            if policy.credential_source == "user_byok":
                info = apply_credential_override(info)
            adapter = (
                DashscopeReranker
                if (info.extra.get("rerank_protocol") or info.extra.get("protocol")) == "dashscope"
                else OpenAIReranker
            )
            model = adapter(
                model_name=info.model_id,
                api_key=info.api_key,
                base_url=info.base_url,
                parameters=dict(info.extra.get("parameters") or {}),
            )
            extract = model._extract_results

            def strict_extract(payload):
                entries = extract(payload)
                indices = [entry.get("index") for entry in entries]
                if any(type(index) is not int for index in indices) or sorted(indices) != list(range(len(documents))):
                    raise ValueError("重排响应索引缺失或重复")
                if any("relevance_score" not in entry for entry in entries):
                    raise ValueError("重排响应缺少分数")
                return entries

            model._extract_results = strict_extract
            # Failed/timeout requests can also be charged: account attempted input as estimated.
            usage = llm_gateway._usage_context.get()
            usage.input_tokens += sum(max(1, math.ceil(len(text) / 4)) for text in [query, *documents])
            usage.estimated = True
            try:
                async with asyncio.timeout(3):
                    scores = await model._batch_rerank(query, documents, max_length=512)
                if len(scores) != len(documents) or not all(math.isfinite(score) for score in scores):
                    raise ValueError("重排结果数量或数值无效")
                return scores
            finally:
                await model.aclose()
    finally:
        if policy is not None:
            await llm_gateway.fail_unstarted_run(policy.run_id, error_type="paper_rerank_aborted")
        # This also runs after a provider failure, before any baseline evidence can be reused.
        await validate_frozen_paper_scope(user=user, tenant_id=tenant_id, snapshot=snapshot)
