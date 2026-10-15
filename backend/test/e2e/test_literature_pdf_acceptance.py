"""Opt-in live acceptance. Two synthetic pages use the actor's configured real model."""

import asyncio
import hashlib
import json
import os
import secrets
import time
from datetime import timedelta

import fitz
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import APIKey, User
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeFile,
    KnowledgeParseArtifact,
    KnowledgeParseRevision,
    LiteraturePaper,
    LiteraturePdfModelCall,
    LiteratureTranslationJob,
)
from yuxi.utils.datetime_utils import utc_now, utc_now_naive

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


@pytest_asyncio.fixture(autouse=True)
async def close_acceptance_pool():
    yield
    await pg_manager.close()


async def test_live_pdf_translation_governed_and_authenticated():
    if os.getenv("YUXI_RUN_PDF_E2E") != "1":
        pytest.skip("Live model acceptance is explicitly opt-in")
    pg_manager.initialize()
    tag = "pdf_acceptance_" + secrets.token_hex(6)
    plaintext = "yxkey_" + secrets.token_hex(24)
    from yuxi.services.principal import resolve_tenant_id
    from yuxi.literature.reading.service import RENDER_BUCKET
    from yuxi.storage.minio import get_minio_client

    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 80), "Rice endosperm", fontsize=16)
        page.insert_text((72, 120), "Abstract", fontsize=12)
        page.insert_text((72, 150), "Rice endosperm stores starch.", fontsize=11)
        page.insert_text((72, 170), "Amylose content was measured in 3 biological replicates.", fontsize=11)
        page = document.new_page()
        page.insert_text((72, 80), "Synthetic quality fixture", fontsize=16)
        page.insert_text((72, 120), "OsbZIP58 and LOC_Os07g00110 were retained as gene identifiers.", fontsize=10)
        page.insert_text((72, 150), "The treatment did not significantly increase amylose content.", fontsize=10)
        page.insert_text((72, 180), "The synthetic experiment used n=3 and p=0.08.", fontsize=10)
        source = document.tobytes()
    digest = hashlib.sha256(source).hexdigest()
    async with pg_manager.get_async_session_context() as db:
        users = (await db.execute(select(User).where(User.role == "superadmin").order_by(User.id))).scalars().all()
        user = next(actor for actor in users if not actor.is_disabled and not actor.is_deleted)
        tenant = await resolve_tenant_id(db, user.uid)
        key = APIKey(
            key_hash=hashlib.sha256(plaintext.encode()).hexdigest(),
            key_prefix=plaintext[:12],
            name=tag,
            user_id=user.id,
            department_id=user.department_id,
            purpose="external_agent",
            scopes=["*"],
            created_by=tag,
            tenant_id=tenant,
            is_enabled=True,
            expires_at=utc_now_naive() + timedelta(hours=1),
        )
        db.add(key)
        db.add(
            KnowledgeBase(
                kb_id=tag,
                name="PDF 翻译验收（合成样本）",
                kb_type="milvus",
                tenant_id=tenant,
                created_by=user.uid,
                share_config={"access_level": "private"},
            )
        )
        await db.flush()
        db.add(
            KnowledgeFile(
                kb_id=tag, file_id=tag, filename="pdf-translation-acceptance.pdf", active_parse_revision_id=tag
            )
        )
        db.add(
            KnowledgeParseRevision(
                revision_id=tag,
                tenant_id=tenant,
                kb_id=tag,
                file_id=tag,
                source_sha256=digest,
                parser_fingerprint=tag,
                pipeline_version="pdf_acceptance_v1",
                status="PARSED_ONLY",
                capabilities={"evidence_ready": False},
                article_summary={"title": "PDF 翻译验收（合成样本）"},
            )
        )
        await db.flush()
        db.add(
            KnowledgeParseArtifact(
                artifact_id=tag,
                revision_id=tag,
                kind="original_pdf",
                object_uri=f"tenants/{tenant}/documents/{digest}/source/original.pdf",
                sha256=digest,
                size_bytes=len(source),
            )
        )
        paper = LiteraturePaper(
            tenant_id=tenant,
            rid=tag,
            title="PDF 翻译验收（合成样本，非科研证据）",
            source_revision_id=tag,
            reading_version=1,
        )
        db.add(paper)
        await db.commit()
        paper_id = paper.id
    await get_minio_client().aupload_file(
        bucket_name=RENDER_BUCKET,
        object_name=f"tenants/{tenant}/documents/{digest}/source/original.pdf",
        data=source,
        content_type="application/pdf",
    )
    try:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:5050",
            headers={"Authorization": f"Bearer {plaintext}"},
            timeout=30,
            trust_env=False,
        ) as client:
            caps = await client.get("/api/literature/pdf-translation/capabilities")
            assert caps.status_code == 200 and caps.json()["enabled"], caps.text
            submitted = await client.post(f"/api/literature/papers/{tag}/translation?variant_kind=pdf_zh")
            assert submitted.status_code == 202, submitted.text
            job_id = submitted.json()["job_id"]
            replay = await client.post(f"/api/literature/papers/{tag}/translation?variant_kind=pdf_zh")
            assert replay.status_code == 202 and replay.json()["job_id"] == job_id
            until = time.monotonic() + 900
            while time.monotonic() < until:
                response = await client.get(f"/api/literature/papers/{tag}/pdf-translation")
                assert response.status_code == 200, response.text
                bundle = response.json()
                if bundle["status"] not in {"PENDING", "RUNNING"}:
                    break
                await asyncio.sleep(3)
            assert bundle["ready"] and bundle["status"] == "SUCCEEDED", bundle.get("reason")
            outputs = {}
            for kind, artifact in bundle["outputs"].items():
                response = await client.get("/api/literature/assets", params={"uri": artifact["uri"]})
                assert response.status_code == 200
                assert hashlib.sha256(response.content).hexdigest() == artifact["digest"]
                outputs[kind] = response.content
            with fitz.open(stream=outputs["pdf_zh"], filetype="pdf") as translated:
                assert translated.page_count == 2
                text = "".join(p.get_text() for p in translated)
                assert any("\u4e00" <= c <= "\u9fff" for c in text) and "3" in text
                compact = "".join(text.split())
                assert "OsbZIP58" in compact and "LOC_Os07g00110" in compact
                assert any(word in translated[1].get_text() for word in ("不", "未", "没有"))
            with fitz.open(stream=outputs["pdf_dual"], filetype="pdf") as dual:
                assert dual.page_count == 4
            assert json.loads(outputs["qa"])["status"] == "passed"
            assert json.loads(outputs["qa"])["checks"]["critical_tokens"] == "passed"
            async with httpx.AsyncClient(base_url="http://127.0.0.1:5050", trust_env=False) as anonymous:
                denied = await anonymous.get(
                    "/api/literature/assets", params={"uri": bundle["outputs"]["pdf_zh"]["uri"]}
                )
                assert denied.status_code in {401, 403}
            async with pg_manager.get_async_session_context() as db:
                job = await db.get(LiteratureTranslationJob, job_id)
                calls = (
                    (await db.execute(select(LiteraturePdfModelCall).where(LiteraturePdfModelCall.job_id == job_id)))
                    .scalars()
                    .all()
                )
                assert calls and all(call.status == "completed" for call in calls)
                assert sum(call.input_tokens + call.output_tokens for call in calls) > 0
            for _ in range(20):
                async with pg_manager.get_async_session_context() as db:
                    job = await db.get(LiteratureTranslationJob, job_id)
                    if job.execution_json.get("cleaned"):
                        break
                await asyncio.sleep(1)
            assert job.execution_json.get("cleaned"), "Remote PDF/cache/QA cleanup did not complete"
    finally:
        if "job_id" in locals():
            from yuxi.literature.pdf_translation import cancel_pdf_translation

            await cancel_pdf_translation(tenant, tag, user.uid, job_id)
        async with pg_manager.get_async_session_context() as db:
            key = (await db.execute(select(APIKey).where(APIKey.name == tag))).scalar_one()
            key.is_enabled = False
            paper = await db.get(LiteraturePaper, paper_id)
            paper.trashed_at = utc_now()
            paper.trashed_reason = "PDF 翻译验收合成样本；保留任务与用量审计"
            await db.commit()
        await pg_manager.async_engine.dispose()


async def test_existing_paid_pdf_outputs_and_ledger_without_model_respending():
    """Revalidate a completed synthetic fixture after interrupted test output."""
    job_id = int(os.getenv("YUXI_PDF_E2E_EXISTING_JOB", "0"))
    if not job_id:
        pytest.skip("Existing paid synthetic fixture verification is opt-in")
    from yuxi.services.principal import resolve_tenant_id
    from yuxi.storage.postgres.models_business import UsageLedger

    pg_manager.initialize()
    plaintext = "yxkey_" + secrets.token_hex(24)
    name = "pdf-revalidation-" + secrets.token_hex(6)
    async with pg_manager.get_async_session_context() as db:
        user = (await db.execute(select(User).where(User.role == "superadmin").order_by(User.id))).scalars().first()
        tenant = await resolve_tenant_id(db, user.uid)
        job = await db.get(LiteratureTranslationJob, job_id)
        assert job and job.status == "SUCCEEDED" and job.requested_by == user.uid
        paper = await db.get(LiteraturePaper, job.paper_id)
        assert paper.rid.startswith("pdf_acceptance_") and "合成样本" in paper.title
        original_trash = (paper.trashed_at, paper.trashed_reason)
        paper.trashed_at = None
        db.add(
            APIKey(
                key_hash=hashlib.sha256(plaintext.encode()).hexdigest(),
                key_prefix=plaintext[:12],
                name=name,
                user_id=user.id,
                department_id=user.department_id,
                purpose="external_agent",
                scopes=["literature:read"],
                created_by=name,
                tenant_id=tenant,
                is_enabled=True,
                expires_at=utc_now_naive() + timedelta(minutes=15),
            )
        )
        before_calls = (
            (await db.execute(select(LiteraturePdfModelCall).where(LiteraturePdfModelCall.job_id == job_id)))
            .scalars()
            .all()
        )
        assert before_calls and all(call.status == "completed" for call in before_calls)
        expected_tokens = sum(call.input_tokens + call.output_tokens for call in before_calls)
        ledger = (await db.execute(select(UsageLedger).where(UsageLedger.run_id == job.llm_run_id))).scalar_one()
        assert ledger.total_tokens == expected_tokens and not ledger.estimated
        await db.commit()
    try:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:5050",
            trust_env=False,
            timeout=60,
            headers={"Authorization": f"Bearer {plaintext}"},
        ) as client:
            response = await client.get(f"/api/literature/papers/{paper.rid}/pdf-translation")
            assert response.status_code == 200, response.text
            bundle = response.json()
            assert bundle["ready"] and bundle["status"] == "SUCCEEDED"
            outputs = {}
            for kind, artifact in bundle["outputs"].items():
                response = await client.get("/api/literature/assets", params={"uri": artifact["uri"]})
                assert response.status_code == 200
                assert hashlib.sha256(response.content).hexdigest() == artifact["digest"]
                outputs[kind] = response.content
            with fitz.open(stream=outputs["pdf_zh"], filetype="pdf") as document:
                assert len(document) == 2
                text = "".join(page.get_text() for page in document)
                compact = "".join(text.split())
                assert "OsbZIP58" in compact and "LOC_Os07g00110" in compact and "0.08" in compact
                assert any(word in document[1].get_text() for word in ("不", "未", "没有"))
            with fitz.open(stream=outputs["pdf_dual"], filetype="pdf") as document:
                assert len(document) == 4
            qa = json.loads(outputs["qa"])
            assert qa["status"] == "passed" and qa["checks"]["critical_tokens"] == "passed"
            assert qa["checks"]["scientific_semantics"] == "requires_human_review"
            async with httpx.AsyncClient(base_url="http://127.0.0.1:5050", trust_env=False, timeout=60) as anonymous:
                response = await anonymous.get(
                    "/api/literature/assets", params={"uri": bundle["outputs"]["pdf_zh"]["uri"]}
                )
                assert response.status_code in (401, 403)
        async with pg_manager.get_async_session_context() as db:
            assert (await db.get(LiteratureTranslationJob, job_id)).execution_json["cleaned"]
            after_calls = (
                (await db.execute(select(LiteraturePdfModelCall).where(LiteraturePdfModelCall.job_id == job_id)))
                .scalars()
                .all()
            )
            assert len(after_calls) == len(before_calls)
    finally:
        async with pg_manager.get_async_session_context() as db:
            (await db.execute(select(APIKey).where(APIKey.name == name))).scalar_one().is_enabled = False
            paper = await db.get(LiteraturePaper, paper.id)
            paper.trashed_at, paper.trashed_reason = original_trash
            await db.commit()
