"""临时脚本：查找 ricellmwiki KB 并做 Neo4j 投影全量导出，打印节点/关系计数。"""

import asyncio
import io
import json
import zipfile
from collections import Counter

from sqlalchemy import text

from yuxi.knowledge.graphs.graph_export_service import ManagedGraphExportService
from yuxi.storage.postgres.manager import pg_manager


async def find_kb():
    async with pg_manager.get_async_session_context() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT kb_id, name, kb_type FROM knowledge_bases "
                    "WHERE lower(name) LIKE '%llmwiki%' OR lower(name) LIKE '%ricellm%' "
                    "OR lower(name) LIKE '%rice%llm%'"
                )
            )
        ).all()
        return rows


async def main():
    kbs = await find_kb()
    print("=== matching knowledge bases ===")
    for row in kbs:
        print(dict(row._mapping) if hasattr(row, "_mapping") else row)
    if not kbs:
        print("NO_KB_FOUND")
        return

    # prefer name containing llmwiki
    target = None
    for row in kbs:
        mapping = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
        name = (mapping.get("name") or "").lower()
        if "llmwiki" in name:
            target = mapping
            break
    if target is None:
        mapping = dict(kbs[0]._mapping) if hasattr(kbs[0], "_mapping") else dict(kbs[0])
        target = mapping
    kb_id = target["kb_id"]
    print(f"\n=== exporting kb_id={kb_id} name={target.get('name')} ===")

    service = ManagedGraphExportService()
    package = await service.export(kb_id, variant="projection", exported_by="cli-test")
    manifest = package["manifest"]
    content = package["content"]

    print("\n=== manifest summary ===")
    print("filename:", package["filename"])
    print("media_type:", package["media_type"])
    print("zip_bytes:", len(content))
    print("schema_version:", manifest.get("schema_version"))
    print("source:", manifest.get("source"))
    print("scope:", json.dumps(manifest.get("scope"), ensure_ascii=False))
    print("counts:", json.dumps(manifest.get("counts"), ensure_ascii=False, indent=2))
    print("files:", json.dumps(manifest.get("files"), ensure_ascii=False, indent=2))
    print("verification:", json.dumps(manifest.get("verification"), ensure_ascii=False, indent=2))

    recon = manifest.get("reconciliation") or {}
    print("\n=== reconciliation summary ===")
    print("matched:", recon.get("matched"))
    for section in ("entities", "triples", "mentions", "chunks"):
        data = recon.get(section) or {}
        print(
            f"{section}: canonical={data.get('canonical_count')} "
            f"projected={data.get('projected_count')} "
            f"missing={data.get('missing_in_projection_count')} "
            f"extra={data.get('extra_in_projection_count')}"
            + (
                f" unflagged={data.get('projected_but_not_flagged_count')}"
                if section == "chunks"
                else ""
            )
        )

    # independently recount from zip members
    print("\n=== independent zip recount ===")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = set(archive.namelist())
        print("zip members:", sorted(names))
        node_lines = 0
        node_class = Counter()
        rel_lines = 0
        rel_type = Counter()
        with archive.open("nodes.jsonl") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                node_lines += 1
                row = json.loads(raw)
                node_class[row.get("node_class")] += 1
        with archive.open("relationships.jsonl") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                rel_lines += 1
                row = json.loads(raw)
                rel_type[row.get("relationship_type")] += 1
        print("nodes.jsonl lines:", node_lines, "by class:", dict(node_class))
        print("relationships.jsonl lines:", rel_lines, "by type:", dict(rel_type))

        # sha256 verify (all members listed in manifest)
        import hashlib

        files_meta = manifest.get("files") or {}
        for member in sorted(name for name in names if name != "manifest.json"):
            data = archive.read(member)
            digest = hashlib.sha256(data).hexdigest()
            meta = files_meta.get(member) or {}
            ok = digest == meta.get("sha256") and len(data) == meta.get("bytes")
            print(f"{member}: sha256_match={ok} bytes={len(data)} rows_meta={meta.get('rows')}")
            if member == "evidence.jsonl":
                ev_kind = Counter()
                ev_verify = Counter()
                ev_status = Counter()
                with archive.open(member) as fh:
                    for raw in fh:
                        if not raw.strip():
                            continue
                        row = json.loads(raw)
                        ev_kind[row.get("kind")] += 1
                        v = row.get("verification")
                        ev_verify[v.get("status") if isinstance(v, dict) else v] += 1
                        ev_status[row.get("review_status")] += 1
                print("  evidence by kind:", dict(ev_kind))
                print("  evidence verification:", {str(k): v for k, v in ev_verify.items()})
                print("  evidence review_status:", dict(ev_status))
            elif member == "chunks.jsonl":
                n = sum(1 for raw in archive.open(member) if raw.strip())
                print("  chunks lines:", n)
            elif member == "decisions.jsonl":
                n = sum(1 for raw in archive.open(member) if raw.strip())
                print("  decisions lines:", n)

    print("\n=== DONE ===")


if __name__ == "__main__":
    asyncio.run(main())
