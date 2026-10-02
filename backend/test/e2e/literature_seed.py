"""文献域 e2e 种子脚本（容器内执行）：合成 PDF/full.md 工件 + 解析版本 + 测试文献
+ 测试 API key（yxkey_lit_e2e_*）+ review.json 双通道差异样本。

用法：docker compose exec api python /app/test/e2e/literature_seed.py [--cleanup]
输出：JSON（含 api_key 明文——仅供 e2e 捕获，不入库明文）。
幂等：重跑覆盖更新；--cleanup 删除测试文献级联行并禁用测试 Key。
可导入：``run_seed()`` 供 test/e2e 用例直接调用（返回 dict，不打印）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sys

TENANT = 1
TEST_DOI = "10.9999/lite2e.2026.001"
TEST_RID = "doi-10.9999_lite2e.2026.001"
REVISION_ID = "sir_lite2e_seed_0001"
SHA = "e2e" + "0" * 61  # 64 hex 占位（合成件，非真实 sha）

TITLE = "Endosperm starch accumulation in rice under post-anthesis heat"
PARAGRAPHS = [
    "Post-anthesis heat stress reduces grain filling rate in rice endosperm.",
    "We measured starch content, AGPase activity and OsNF-YB1 expression.",
    "Grain weight declined 12% under 35C day temperature within ten days.",
    "Sucrose transport genes were downregulated before starch enzymes.",
    "Results link heat tolerance to source-sink balance rather than sink capacity.",
]
FULL_MD = f"# {TITLE}\n\n" + "\n\n".join(f"{p}\n" for p in PARAGRAPHS)

_REVIEW_ITEMS = {
    "count": 1,
    "total": 1,
    "items": [
        {
            "report_idx": 0,
            "para_id": "P002",
            "blocking": True,
            "a_text": "We measured starch content, AGPase activity and OsNF-YB1 expression.",
            "b_text": "We measured starch content, AGPase activity and OsNF-YB1 expression level.",
        },
    ],
    "ai": {"source": "seed"},
}


async def run_seed(cleanup: bool = False) -> dict:
    from sqlalchemy import select, update

    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_business import APIKey, User
    from yuxi.storage.postgres.models_knowledge import (
        KnowledgeBase,
        KnowledgeFile,
        KnowledgeParseArtifact,
        KnowledgeParseRevision,
        LiteraturePaper,
    )

    async with pg_manager.get_async_session_context() as session:
        admin = (
            await session.execute(select(User).where(User.role == "superadmin").order_by(User.id).limit(1))
        ).scalar_one_or_none()
        if admin is None:
            return {"ok": False, "error": "no superadmin user"}
        admin_id = admin.id
        admin_dept = admin.department_id

    # ---- API key：复用或新建（明文仅本次打印） ----
    async with pg_manager.get_async_session_context() as session:
        existing = (await session.execute(select(APIKey).where(APIKey.name == "lit-e2e-test-key"))).scalar_one_or_none()
        if cleanup:
            if existing is not None:
                existing.is_enabled = False
            await session.execute(update(LiteraturePaper).where(LiteraturePaper.rid == TEST_RID).values(title=None))
            await session.execute(LiteraturePaper.__table__.delete().where(LiteraturePaper.rid == TEST_RID))
            await session.commit()
            return {"ok": True, "cleaned": True}
        # 哈希单向取不回明文：每次运行都重新签发（按 name upsert，旧 Key 自然失效）
        plaintext = "yxkey_lit_e2e_" + secrets.token_hex(16)
        if existing is None:
            existing = APIKey(
                key_hash="",
                key_prefix="",
                name="lit-e2e-test-key",
                user_id=admin_id,
                department_id=admin_dept,
                purpose="external_agent",
                scopes=["*"],
                created_by="lit-e2e",
                tenant_id=TENANT,
            )
            session.add(existing)
        existing.key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        existing.key_prefix = plaintext[:12]
        existing.user_id = admin_id
        existing.department_id = admin_dept
        existing.is_enabled = True
        existing.expires_at = None
        await session.commit()

    # ---- 合成 PDF（pymupdf）与工件 ----
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    y = 90
    page.insert_text((72, y), TITLE, fontsize=14, fontname="helv")
    y += 30
    for para in PARAGRAPHS:
        page.insert_text((72, y), para, fontsize=10, fontname="helv")
        y += 18
    pdf_bytes = doc.tobytes()
    doc.close()

    from yuxi.literature.reading.service import RENDER_BUCKET
    from yuxi.storage.minio import get_minio_client

    minio = get_minio_client()
    doc_prefix = f"tenants/{TENANT}/documents/{SHA}"
    await minio.aupload_file(
        bucket_name=RENDER_BUCKET,
        object_name=f"{doc_prefix}/source/original.pdf",
        data=pdf_bytes,
        content_type="application/pdf",
    )
    await minio.aupload_file(
        bucket_name=RENDER_BUCKET,
        object_name=f"{doc_prefix}/mineru/{REVISION_ID}/full.md",
        data=FULL_MD.encode("utf-8"),
        content_type="text/markdown",
    )
    # 双通道差异样本（G4 复核清单）：渲染前先放好 work/review.json
    render_prefix = f"{doc_prefix}/reader_render/{REVISION_ID}"
    await minio.aupload_file(
        bucket_name=RENDER_BUCKET,
        object_name=f"{render_prefix}/work/review.json",
        data=json.dumps(_REVIEW_ITEMS, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
    )

    # ---- KB / File / 解析版本 + 工件行（ACTIVE：渲染/编译输入可解析） ----
    async with pg_manager.get_async_session_context() as session:
        if (
            await session.execute(select(KnowledgeBase.id).where(KnowledgeBase.kb_id == "kb_lite2e"))
        ).scalar_one_or_none() is None:
            session.add(KnowledgeBase(kb_id="kb_lite2e", name="文献域 e2e", kb_type="milvus", tenant_id=TENANT))
            await session.flush()
        if (
            await session.execute(select(KnowledgeFile.id).where(KnowledgeFile.file_id == "kf_lite2e"))
        ).scalar_one_or_none() is None:
            session.add(KnowledgeFile(kb_id="kb_lite2e", file_id="kf_lite2e", filename="lit-e2e.pdf"))
            await session.flush()
        revision = (
            await session.execute(
                select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == REVISION_ID)
            )
        ).scalar_one_or_none()
        if revision is None:
            session.add(
                KnowledgeParseRevision(
                    revision_id=REVISION_ID,
                    tenant_id=TENANT,
                    kb_id="kb_lite2e",
                    file_id="kf_lite2e",
                    source_sha256=SHA,
                    parser_fingerprint="fp_lite2e_v1",
                    pipeline_version="scientific_pdf_v3.2",
                    status="ACTIVE",
                )
            )
        for artifact_id, kind, suffix in (
            ("spa_lite2e_md", "mineru_markdown", f"mineru/{REVISION_ID}/full.md"),
            ("spa_lite2e_pdf", "original_pdf", "source/original.pdf"),
        ):
            exists = (
                await session.execute(
                    select(KnowledgeParseArtifact.id).where(
                        KnowledgeParseArtifact.revision_id == REVISION_ID,
                        KnowledgeParseArtifact.kind == kind,
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                session.add(
                    KnowledgeParseArtifact(
                        artifact_id=artifact_id,
                        revision_id=REVISION_ID,
                        kind=kind,
                        object_uri=f"{doc_prefix}/{suffix}",
                        sha256=hashlib.sha256(FULL_MD.encode()).hexdigest(),
                        size_bytes=len(FULL_MD),
                    )
                )
        await session.commit()

    # ---- 文献登记（source_revision_id 挂上） ----
    from yuxi.literature.notes.service import register_literature_paper

    paper = await register_literature_paper(tenant_id=TENANT, doi=TEST_DOI, title=TITLE, source_revision_id=REVISION_ID)

    return {
        "ok": True,
        "api_key": plaintext,
        "rid": paper.rid,
        "revision_id": REVISION_ID,
        "paper_id": paper.id,
        "tenant": TENANT,
    }


async def main() -> int:
    result = await run_seed(cleanup="--cleanup" in sys.argv)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
