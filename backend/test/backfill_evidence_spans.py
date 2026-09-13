"""对存量解析版本回填 evidence_spans(一次性运维脚本,幂等)。

用法: docker exec api-dev uv run --no-sync python test/backfill_evidence_spans.py <revision_id>
正文来源: unified_article artifact(article.markdown,与解析管线同源)。
"""

import asyncio
import json
import sys


async def main() -> None:
    from sqlalchemy import select

    from yuxi.knowledge.evidence.span_builder import build_evidence_spans
    from yuxi.storage.minio import get_minio_client
    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_knowledge import (
        EvidenceAnchorRecord,
        KnowledgeParseArtifact,
        KnowledgeParseRevision,
    )

    revision_id = sys.argv[1]
    pg_manager.initialize()
    async with pg_manager.get_async_session_context() as db:
        revision = (
            await db.execute(select(KnowledgeParseRevision).where(KnowledgeParseRevision.revision_id == revision_id))
        ).scalar_one_or_none()
        if revision is None:
            print(f"revision not found: {revision_id}")
            return
        artifact = (
            (
                await db.execute(
                    select(KnowledgeParseArtifact)
                    .where(
                        KnowledgeParseArtifact.revision_id == revision_id,
                        KnowledgeParseArtifact.kind == "unified_article",
                    )
                    .order_by(KnowledgeParseArtifact.id.desc())
                )
            )
            .scalars()
            .first()
        )
        if artifact is None:
            print(f"no unified_article artifact for {revision_id}")
            return
        from yuxi.knowledge.utils.kb_utils import parse_minio_url as _parse

        bucket, object_name = _parse(artifact.object_uri)
        payload = await get_minio_client().adownload_file(bucket, object_name)
        article = json.loads(payload.decode("utf-8"))
        body = str(article.get("markdown") or "")
        if not body:
            print("article markdown empty")
            return
        anchors = list(
            (
                await db.execute(
                    select(EvidenceAnchorRecord).where(EvidenceAnchorRecord.parse_revision_id == revision_id)
                )
            )
            .scalars()
            .all()
        )
        summary = await build_evidence_spans(db, revision=revision, anchors=anchors, markdown_body=body)
        print("span summary:", summary)


asyncio.run(main())
