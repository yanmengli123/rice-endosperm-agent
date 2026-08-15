from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections import defaultdict
from typing import Any

from yuxi.knowledge.graphs.managed_import_parser import (
    NORMALIZER_VERSION,
    SCHEMA_VERSION,
    parse_managed_graph_import,
)
from yuxi.knowledge.graphs.milvus_graph_vector_store import MilvusGraphVectorStore
from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository
from yuxi.repositories.knowledge_graph_import_repository import KnowledgeGraphImportRepository
from yuxi.storage.minio import get_minio_client
from yuxi.storage.neo4j import get_shared_neo4j_connection, neo4j_read, neo4j_write, safe_neo4j_label
from yuxi.utils import hashstr

GRAPH_IMPORT_TASK_TYPE = "knowledge_graph_import"
GRAPH_IMPORT_ROLLBACK_TASK_TYPE = "knowledge_graph_import_rollback"
GRAPH_IMPORT_BUCKET = "knowledgebases"


class ManagedGraphImportService:
    def __init__(
        self,
        *,
        repository: KnowledgeGraphImportRepository | None = None,
        kb_repository: KnowledgeBaseRepository | None = None,
        vector_store: MilvusGraphVectorStore | None = None,
        neo4j_connection=None,
        minio_client=None,
    ):
        self.repository = repository or KnowledgeGraphImportRepository()
        self.kb_repository = kb_repository or KnowledgeBaseRepository()
        self._vector_store = vector_store
        self._neo4j_connection = neo4j_connection
        self._minio_client = minio_client

    @property
    def vector_store(self) -> MilvusGraphVectorStore:
        if self._vector_store is None:
            self._vector_store = MilvusGraphVectorStore()
        return self._vector_store

    @property
    def neo4j_connection(self):
        if self._neo4j_connection is None:
            self._neo4j_connection = get_shared_neo4j_connection()
        return self._neo4j_connection

    @property
    def minio_client(self):
        if self._minio_client is None:
            self._minio_client = get_minio_client()
        return self._minio_client

    async def create_upload(
        self,
        *,
        kb_id: str,
        name: str,
        nodes_bytes: bytes,
        relationships_bytes: bytes,
        cypher_bytes: bytes | None,
        created_by: str,
        mapping_config: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        await self._require_milvus_kb(kb_id)
        mapping_config = mapping_config or {}
        checksums = {
            "nodes": _sha256(nodes_bytes),
            "relationships": _sha256(relationships_bytes),
            "cypher": _sha256(cypher_bytes) if cypher_bytes is not None else None,
        }
        idempotency_material = json.dumps(
            {
                "kb_id": kb_id,
                "checksums": checksums,
                "mapping": mapping_config,
                "schema_version": SCHEMA_VERSION,
                "normalizer_version": NORMALIZER_VERSION,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        idempotency_key = hashstr(idempotency_material, length=64)
        existing = await self.repository.get_by_idempotency_key(idempotency_key)
        if existing and existing.status != "ROLLED_BACK":
            return self.repository.import_to_dict(existing), True
        if existing:
            idempotency_key = hashstr(f"{idempotency_material}:reimport:{uuid.uuid4().hex}", length=64)

        import_id = f"gimp_{uuid.uuid4().hex}"
        prefix = f"{kb_id}/graph-imports/{import_id}"
        nodes_object = f"{prefix}/{checksums['nodes']}_nodes.csv"
        relationships_object = f"{prefix}/{checksums['relationships']}_relationships.csv"
        cypher_object = f"{prefix}/{checksums['cypher']}_description.cypher" if cypher_bytes is not None else None
        await self.minio_client.aupload_file(GRAPH_IMPORT_BUCKET, nodes_object, nodes_bytes, "text/csv")
        await self.minio_client.aupload_file(GRAPH_IMPORT_BUCKET, relationships_object, relationships_bytes, "text/csv")
        if cypher_bytes is not None and cypher_object:
            await self.minio_client.aupload_file(GRAPH_IMPORT_BUCKET, cypher_object, cypher_bytes, "text/plain")

        await self.repository.create(
            {
                "import_id": import_id,
                "kb_id": kb_id,
                "name": name.strip() or "CSV 图谱导入",
                "status": "UPLOADED",
                "schema_version": SCHEMA_VERSION,
                "normalizer_version": NORMALIZER_VERSION,
                "nodes_object_name": nodes_object,
                "relationships_object_name": relationships_object,
                "cypher_object_name": cypher_object,
                "nodes_sha256": checksums["nodes"],
                "relationships_sha256": checksums["relationships"],
                "cypher_sha256": checksums["cypher"],
                "idempotency_key": idempotency_key,
                "mapping_config": mapping_config,
                "created_by": created_by,
            }
        )
        report = await self.validate(import_id, {})
        refreshed = await self.repository.get(import_id)
        assert refreshed is not None
        payload = self.repository.import_to_dict(refreshed)
        payload["validation_report"] = report
        return payload, False

    async def validate(self, import_id: str, resolutions: dict[str, Any]) -> dict[str, Any]:
        record = await self._require_import(import_id)
        if record.status == "SUCCEEDED":
            raise ValueError("已完成批次的规范实体和路由方案不可变更；如需调整，请先回滚后重新上传")
        if record.status == "ROLLED_BACK":
            raise ValueError("已回滚批次不能重新预检；请重新上传原始文件")
        await self.repository.update_import(import_id, {"status": "PARSING", "error_message": None})
        nodes_bytes, relationships_bytes, cypher_bytes = await self._download_files(record)
        await self.repository.update_import(import_id, {"status": "VALIDATING"})
        parsed = parse_managed_graph_import(
            kb_id=record.kb_id,
            nodes_bytes=nodes_bytes,
            relationships_bytes=relationships_bytes,
            cypher_bytes=cypher_bytes,
            resolutions=resolutions,
        )
        report = _public_report(parsed)
        effective_resolutions = parsed.get("effective_resolutions", resolutions)
        status = parsed["status"]
        if status == "INVALID":
            status = "FAILED"
        await self.repository.update_import(
            import_id,
            {
                "status": status,
                "schema_version": SCHEMA_VERSION,
                "normalizer_version": NORMALIZER_VERSION,
                "validation_report": report,
                "resolution_config": effective_resolutions,
                "error_message": report["errors"][0]["message"] if report["errors"] else None,
            },
        )
        return report

    async def execute(self, import_id: str, resolutions: dict[str, Any], context=None) -> dict[str, Any]:
        record = await self._require_import(import_id)
        if record.status == "SUCCEEDED":
            return dict(record.result or {})
        if record.status == "ROLLED_BACK":
            raise ValueError("已回滚的导入批次不能再次执行，请重新上传")

        try:
            await _progress(context, 4, "读取并重新校验原始导入文件")
            nodes_bytes, relationships_bytes, cypher_bytes = await self._download_files(record)
            parsed = parse_managed_graph_import(
                kb_id=record.kb_id,
                nodes_bytes=nodes_bytes,
                relationships_bytes=relationships_bytes,
                cypher_bytes=cypher_bytes,
                resolutions=resolutions,
            )
            report = _public_report(parsed)
            effective_resolutions = parsed.get("effective_resolutions", resolutions)
            if not parsed["valid"] or parsed["plan"] is None:
                await self.repository.update_import(
                    import_id,
                    {
                        "status": parsed["status"] if parsed["status"] != "INVALID" else "FAILED",
                        "validation_report": report,
                        "resolution_config": effective_resolutions,
                    },
                )
                if parsed["conflicts"]:
                    raise ValueError(f"仍有 {len(parsed['conflicts'])} 个阻塞冲突未解决")
                raise ValueError(report["errors"][0]["message"] if report["errors"] else "预检未通过")

            await self.repository.update_import(
                import_id,
                {"status": "READY", "validation_report": report, "resolution_config": effective_resolutions},
            )
            await _progress(context, 15, "提交 PostgreSQL 规范实体、三元组、证据、来源和 Outbox")
            result = await self.repository.commit_plan(import_id, parsed["plan"], effective_resolutions)
            projection = await self._project_import(record.kb_id, import_id, context)
            result.update(projection)
            await self.repository.update_import(
                import_id,
                {"status": "SUCCEEDED", "result": result, "completed_at": _utc_now(), "error_message": None},
            )
            await _progress(context, 100, "图谱导入、双投影和 ID 对账全部完成")
            if context:
                await context.set_result(result)
            return result
        except asyncio.CancelledError:
            await self.repository.set_phase(import_id, "CANCELLED", error="任务已取消，可重新执行以继续投影")
            raise
        except Exception as exc:
            current = await self.repository.get(import_id)
            if current and current.status not in {"AWAITING_CONFLICT_RESOLUTION", "FAILED"}:
                await self.repository.set_phase(import_id, "FAILED", error=str(exc))
            raise

    async def rollback(self, import_id: str, context=None) -> dict[str, Any]:
        record = await self._require_import(import_id)
        if record.status not in {"SUCCEEDED", "FAILED", "ROLLED_BACK"}:
            raise ValueError("只有已完成或失败的导入批次可以回滚")
        if record.status == "ROLLED_BACK":
            return {"status": "ROLLED_BACK", "message": "该批次已经回滚"}
        try:
            await _progress(context, 10, "移除导入来源并计算失去全部来源的数据")
            canonical_result = await self.repository.rollback_canonical(import_id)
            await self.repository.start_outbox(import_id, "neo4j", "GRAPH_IMPORT_REBUILD")
            await _progress(context, 40, "清理并校准 Neo4j 投影")
            data = await self.repository.full_projection_data(record.kb_id)
            await asyncio.to_thread(
                self._rebuild_neo4j_projection,
                record.kb_id,
                data,
                canonical_result,
            )
            await self.repository.finish_outbox(import_id, "neo4j", event_type="GRAPH_IMPORT_REBUILD")

            await self.repository.start_outbox(import_id, "milvus", "GRAPH_IMPORT_REBUILD")
            await _progress(context, 70, "清理并校准 Milvus 向量投影")
            await self.vector_store.delete_graph_records(
                record.kb_id,
                entity_ids=canonical_result.get("orphan_entity_ids", []),
                triple_ids=canonical_result.get("orphan_triple_ids", []),
            )
            embedding_model_spec = await self.repository.get_embedding_model_spec(record.kb_id)
            await self.vector_store.insert_missing_graph_records(
                kb_id=record.kb_id,
                embedding_model_spec=embedding_model_spec,
                entities=data["entities"],
                triples=data["triples"],
            )
            await self.repository.finish_outbox(import_id, "milvus", event_type="GRAPH_IMPORT_REBUILD")
            result = {**canonical_result, "status": "ROLLED_BACK"}
            await self.repository.update_import(
                import_id,
                {"status": "ROLLED_BACK", "result": result, "completed_at": _utc_now(), "error_message": None},
            )
            await _progress(context, 100, "导入批次已安全回滚")
            if context:
                await context.set_result(result)
            return result
        except Exception as exc:
            await self.repository.set_phase(import_id, "FAILED", error=f"回滚失败：{exc}")
            raise

    async def _project_import(self, kb_id: str, import_id: str, context=None) -> dict[str, Any]:
        data = await self.repository.projection_data(import_id)
        await self.repository.start_outbox(import_id, "neo4j")
        await self.repository.set_phase(import_id, "PROJECTING_NEO4J")
        await _progress(context, 35, "写入 Neo4j 可重建投影")
        try:
            await asyncio.to_thread(self._upsert_neo4j_projection, kb_id, data)
            await self.repository.finish_outbox(import_id, "neo4j")
        except Exception as exc:
            await self.repository.finish_outbox(import_id, "neo4j", error=str(exc))
            raise

        await self.repository.start_outbox(import_id, "milvus")
        await self.repository.set_phase(import_id, "PROJECTING_MILVUS")
        await _progress(context, 62, "生成并写入 Milvus 向量投影")
        try:
            embedding_model_spec = await self.repository.get_embedding_model_spec(kb_id)
            entity_ids = [item["entity_id"] for item in data["entities"]]
            triple_ids = [item["triple_id"] for item in data["triples"]]
            await self.vector_store.delete_graph_records(kb_id, entity_ids=entity_ids, triple_ids=triple_ids)
            await self.vector_store.insert_missing_graph_records(
                kb_id=kb_id,
                embedding_model_spec=embedding_model_spec,
                entities=data["entities"],
                triples=data["triples"],
            )
            await self.repository.finish_outbox(import_id, "milvus")
        except Exception as exc:
            await self.repository.finish_outbox(import_id, "milvus", error=str(exc))
            raise

        await self.repository.set_phase(import_id, "RECONCILING")
        await _progress(context, 88, "对账 PostgreSQL、Neo4j 与 Milvus 的导入 ID 集合")
        reconciliation = await self._reconcile(kb_id, data)
        if not reconciliation["matched"]:
            raise RuntimeError("投影 ID 对账失败，导入不会标记为成功")
        return {
            "entity_count": len(data["entities"]),
            "triple_count": len(data["triples"]),
            "evidence_count": len(data["evidence"]),
            "reconciliation": reconciliation,
        }

    def _upsert_neo4j_projection(self, kb_id: str, data: dict[str, Any]) -> None:
        label = safe_neo4j_label(kb_id)
        evidence_aggregates = _aggregate_evidence(data["evidence"])
        entities = [
            {**item, "attributes_json": json.dumps(item.get("attributes") or {}, ensure_ascii=False)}
            for item in data["entities"]
        ]
        triples = [
            {**item, **evidence_aggregates.get(item["triple_id"], _empty_evidence_aggregate())}
            for item in data["triples"]
        ]

        def query(tx):
            for rows in _batches(entities):
                tx.run(
                    f"""
                    UNWIND $rows AS row
                    MERGE (e:Entity:MilvusKB:`{label}` {{entity_id: row.entity_id}})
                    SET e.kb_id = $kb_id,
                        e.normalized_name = row.normalized_name,
                        e.label = row.label,
                        e.name = row.name,
                        e.attributes = row.attributes_json,
                        e.managed_projection = true
                    """,
                    rows=rows,
                    kb_id=kb_id,
                ).consume()
            for rows in _batches(triples):
                tx.run(
                    f"""
                    UNWIND $rows AS row
                    MATCH (source:Entity:MilvusKB:`{label}` {{entity_id: row.source_entity_id}})
                    MATCH (target:Entity:MilvusKB:`{label}` {{entity_id: row.target_entity_id}})
                    MERGE (source)-[r:RELATION {{triple_id: row.triple_id}}]->(target)
                    SET r.kb_id = $kb_id,
                        r.type = row.relation_type,
                        r.text = row.content,
                        r.support_count = row.support_count,
                        r.literature_count = row.literature_count,
                        r.best_evidence_level = row.best_evidence_level,
                        r.consensus_direction = row.consensus_direction,
                        r.ambiguous_evidence_count = row.ambiguous_evidence_count,
                        r.pmids = row.pmids,
                        r.dois = row.dois,
                        r.managed_projection = true
                    """,
                    rows=rows,
                    kb_id=kb_id,
                ).consume()

        neo4j_write(self.neo4j_connection.driver, query)

    def _rebuild_neo4j_projection(
        self,
        kb_id: str,
        data: dict[str, Any],
        rollback_result: dict[str, Any],
    ) -> None:
        label = safe_neo4j_label(kb_id)
        orphan_entity_ids = rollback_result.get("orphan_entity_ids", [])
        orphan_triple_ids = rollback_result.get("orphan_triple_ids", [])

        def query(tx):
            for ids in _batches(orphan_triple_ids):
                tx.run(
                    f"MATCH (:Entity:MilvusKB:`{label}`)-[r:RELATION]->(:Entity:MilvusKB:`{label}`) "
                    "WHERE r.triple_id IN $ids DELETE r",
                    ids=ids,
                ).consume()
            for ids in _batches(orphan_entity_ids):
                tx.run(
                    f"MATCH (e:Entity:MilvusKB:`{label}`) WHERE e.entity_id IN $ids AND NOT (e)--() DELETE e",
                    ids=ids,
                ).consume()

        neo4j_write(self.neo4j_connection.driver, query)
        self._upsert_neo4j_projection(kb_id, data)

    async def _reconcile(self, kb_id: str, data: dict[str, Any]) -> dict[str, Any]:
        expected_entities = sorted(item["entity_id"] for item in data["entities"])
        expected_triples = sorted(item["triple_id"] for item in data["triples"])
        neo4j_entities, neo4j_triples = await asyncio.to_thread(
            self._neo4j_existing_ids,
            kb_id,
            expected_entities,
            expected_triples,
        )
        milvus_entities, milvus_triples = await self.vector_store.existing_graph_record_ids(
            kb_id,
            entity_ids=expected_entities,
            triple_ids=expected_triples,
        )
        result = {
            "expected": {"entities": len(expected_entities), "triples": len(expected_triples)},
            "neo4j": {"entities": len(neo4j_entities), "triples": len(neo4j_triples)},
            "milvus": {"entities": len(milvus_entities), "triples": len(milvus_triples)},
            "missing": {
                "neo4j_entities": sorted(set(expected_entities) - neo4j_entities),
                "neo4j_triples": sorted(set(expected_triples) - neo4j_triples),
                "milvus_entities": sorted(set(expected_entities) - milvus_entities),
                "milvus_triples": sorted(set(expected_triples) - milvus_triples),
            },
        }
        result["matched"] = not any(result["missing"].values())
        return result

    def _neo4j_existing_ids(
        self,
        kb_id: str,
        entity_ids: list[str],
        triple_ids: list[str],
    ) -> tuple[set[str], set[str]]:
        label = safe_neo4j_label(kb_id)
        entity_rows = neo4j_read(
            self.neo4j_connection.driver,
            f"MATCH (e:Entity:MilvusKB:`{label}`) WHERE e.entity_id IN $ids RETURN e.entity_id AS id",
            ids=entity_ids,
        )
        triple_rows = neo4j_read(
            self.neo4j_connection.driver,
            f"MATCH (:Entity:MilvusKB:`{label}`)-[r:RELATION]->(:Entity:MilvusKB:`{label}`) "
            "WHERE r.triple_id IN $ids RETURN DISTINCT r.triple_id AS id",
            ids=triple_ids,
        )
        return {row["id"] for row in entity_rows}, {row["id"] for row in triple_rows}

    async def _download_files(self, record) -> tuple[bytes, bytes, bytes | None]:
        nodes, relationships = await asyncio.gather(
            self.minio_client.adownload_file(GRAPH_IMPORT_BUCKET, record.nodes_object_name),
            self.minio_client.adownload_file(GRAPH_IMPORT_BUCKET, record.relationships_object_name),
        )
        cypher = None
        if record.cypher_object_name:
            cypher = await self.minio_client.adownload_file(GRAPH_IMPORT_BUCKET, record.cypher_object_name)
        return nodes, relationships, cypher

    async def _require_import(self, import_id: str):
        record = await self.repository.get(import_id)
        if record is None:
            raise ValueError("图谱导入批次不存在")
        return record

    async def _require_milvus_kb(self, kb_id: str):
        kb = await self.kb_repository.get_by_kb_id(kb_id)
        if kb is None:
            raise ValueError("知识库不存在")
        if (kb.kb_type or "").lower() != "milvus":
            raise ValueError("托管图谱导入仅支持 Milvus 知识库")
        return kb


def _aggregate_evidence(evidence: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evidence:
        grouped[item["triple_id"]].append(item)
    result = {}
    for triple_id, items in grouped.items():
        aligned_items = [item for item in items if item.get("evidence_alignment_status", "ALIGNED") == "ALIGNED"]
        directions = {item.get("direction") or "UNKNOWN" for item in items}
        known_directions = directions - {"UNKNOWN", ""}
        consensus = (
            next(iter(known_directions))
            if len(known_directions) == 1
            else "CONFLICTED"
            if known_directions
            else "UNKNOWN"
        )
        levels = [item.get("evidence_level") for item in items if item.get("evidence_level")]
        literature_ids = {identity for item in aligned_items for identity in _exact_literature_keys(item) if identity}
        result[triple_id] = {
            "support_count": len({item["evidence_id"] for item in items}),
            "literature_count": len(literature_ids),
            "best_evidence_level": min(levels, key=_evidence_level_rank) if levels else None,
            "consensus_direction": consensus,
            "ambiguous_evidence_count": len(items) - len(aligned_items),
            "pmids": sorted(
                {pmid for item in items for pmid in ((item.get("metadata_json") or {}).get("pmids") or []) if pmid}
            ),
            "dois": sorted(
                {doi for item in items for doi in ((item.get("metadata_json") or {}).get("dois") or []) if doi}
            ),
        }
    return result


def _empty_evidence_aggregate() -> dict[str, Any]:
    return {
        "support_count": 0,
        "literature_count": 0,
        "best_evidence_level": None,
        "consensus_direction": "UNKNOWN",
        "ambiguous_evidence_count": 0,
        "pmids": [],
        "dois": [],
    }


def _evidence_level_rank(value: str) -> tuple[int, str]:
    digits = "".join(character for character in value if character.isdigit())
    return (int(digits) if digits else 999, value)


def _exact_literature_keys(evidence: dict[str, Any]) -> list[str]:
    metadata = evidence.get("metadata_json") or {}
    pmids = metadata.get("pmids") or []
    dois = metadata.get("dois") or []
    if pmids and dois and len(pmids) == len(dois):
        return [f"pmid:{pmid}|doi:{doi}" for pmid, doi in zip(pmids, dois, strict=True)]
    if pmids:
        return [f"pmid:{pmid}" for pmid in pmids]
    return [f"doi:{doi}" for doi in dois]


def _batches(items: list[Any], size: int = 500):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _public_report(parsed: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in parsed.items() if key not in {"plan", "effective_resolutions"}}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _progress(context, progress: float, message: str) -> None:
    if context:
        await context.set_progress(progress, message)


def _utc_now():
    from yuxi.utils.datetime_utils import utc_now_naive

    return utc_now_naive()
