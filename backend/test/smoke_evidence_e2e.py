"""证据组装器真实数据冒烟（容器内一次性运行）。

docker compose exec api uv run python test/smoke_evidence_e2e.py <run_id>

调试语境：``allowed_kb_ids`` 从该 run 实际触及的 chunk 推导，只投影本 run
冻结检索范围内的资源，不越过组装器的权限交集路径。
"""

import asyncio
import json
import sys

from sqlalchemy import select


async def _run_touched_kb_ids(db, run_id: str) -> set[str]:
    from yuxi.repositories.knowledge_retrieval_repository import KnowledgeRetrievalRepository
    from yuxi.storage.postgres.models_knowledge import KnowledgeChunk

    records = await KnowledgeRetrievalRepository(db).list_for_run(run_id)
    chunk_ids: list[str] = []
    for record in records:
        chunk_ids.extend(str(item) for item in (record.chunk_ids_json or []) if item)
    if not chunk_ids:
        return set()
    rows = (await db.execute(select(KnowledgeChunk.kb_id).where(KnowledgeChunk.chunk_id.in_(chunk_ids)))).all()
    return {str(row.kb_id) for row in rows if row.kb_id}


async def main() -> None:
    from yuxi.knowledge.evidence import assemble_evidence_for_run
    from yuxi.storage.postgres.manager import pg_manager

    pg_manager.initialize()
    run_id = sys.argv[1]
    async with pg_manager.get_async_session_context() as db:
        allowed_kb_ids = await _run_touched_kb_ids(db, run_id)
        result = await assemble_evidence_for_run(db, run_id, allowed_kb_ids=allowed_kb_ids)
    print("role:", result["evidence_role"], "| claim_binding:", result["claim_binding_status"])
    print("summary:", json.dumps(result["summary"], ensure_ascii=False))
    print("issues:", json.dumps(result["issues"], ensure_ascii=False)[:400])
    print("retrievals:", json.dumps(result["retrievals"], ensure_ascii=False)[:200])
    for item in result["evidence"][:2]:
        q = item["quote"]
        loc = item["locator"]
        src = item["source"]
        v = item["verification"]
        print("---")
        print("evidence_id:", item["evidence_id"], "| schema:", item["schema_version"])
        print("quote.exact:", (q["exact"] or "")[:70], "...")
        print(
            "chars:",
            q["start_char"],
            "-",
            q["end_char"],
            "| words:",
            q["start_word"],
            "-",
            q["end_word"],
            "| prefix/suffix:",
            bool(q["prefix"]),
            bool(q["suffix"]),
        )
        frag = loc["fragments"][0] if loc["fragments"] else None
        print(
            "page:",
            frag and frag["page_number"],
            "| bbox:",
            frag and frag["bbox"],
            "| quality:",
            loc["quality"],
            "| locatable:",
            loc["locatable"],
        )
        print(
            "source: parse_rev =",
            (src["parse_revision_id"] or "")[:14],
            "| sha:",
            (src["source_sha256"] or "")[:12],
            "| index_rev:",
            (src["index_revision_id"] or "")[:14],
        )
        print("verification:", v["status"], v["errors"][:2])
    if result["rejected"]:
        print("rejected sample:", result["rejected"][0]["verification"]["errors"])


asyncio.run(main())
