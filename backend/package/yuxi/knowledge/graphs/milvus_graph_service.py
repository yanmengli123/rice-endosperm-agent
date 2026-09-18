from __future__ import annotations

import asyncio
import json
import weakref
from typing import Any

from yuxi.knowledge.graphs.extractors import GraphExtractor, GraphExtractorFactory, normalize_extraction_result
from yuxi.knowledge.graphs.graph_utils import (
    RELATION_EVIDENCE_FIELDS,
    build_graph_payload,
    compute_entity_id,
    compute_triple_id,
    cypher_merge_chunk,
    cypher_merge_entity_mention,
    cypher_merge_relation,
    normalize_entity_name,
)
from yuxi.knowledge.graphs.milvus_graph_vector_store import MilvusGraphVectorStore
from yuxi.knowledge.graphs.review_overlay import (
    KIND_ENTITY,
    KIND_TRIPLE,
    STATUS_APPROVED,
    STATUS_CANONICAL,
    STATUS_REJECTED,
    ReviewDecisionIndex,
    plan_replay,
)
from yuxi.models.providers.cache import model_cache
from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository
from yuxi.repositories.knowledge_chunk_repository import KnowledgeChunkRepository
from yuxi.repositories.knowledge_graph_repository import KnowledgeGraphRepository
from yuxi.repositories.knowledge_graph_review_repository import KnowledgeGraphReviewRepository
from yuxi.storage.neo4j import (
    Neo4jConnectionManager,
    get_shared_neo4j_connection,
    neo4j_read,
    neo4j_write,
    safe_neo4j_label,
)
from yuxi.utils import logger
from yuxi.utils.datetime_utils import utc_isoformat

GRAPH_CONFIG_KEY = "graph_build_config"
GRAPH_TASK_TYPE = "knowledge_graph_index"
GRAPH_INDEX_MAX_ATTEMPTS = 3
NEO4J_QUERY_OFFLOAD_LIMIT = 8
# 走聊天模型的抽取器：configure 需校验 chat 模型、并发数取 concurrency_count
LLM_EXTRACTOR_TYPES = frozenset({"llm", "llm_scientific"})
# 图查询审核策略：候选边显示（默认，靠徽标区分）/ 只显示 APPROVED 与 CANONICAL（企业严格模式）
REVIEW_POLICY_CANDIDATES_VISIBLE = "candidates_visible"
REVIEW_POLICY_APPROVED_ONLY = "approved_only"
REVIEW_POLICIES = frozenset({REVIEW_POLICY_CANDIDATES_VISIBLE, REVIEW_POLICY_APPROVED_ONLY})
_VISIBLE_UNDER_APPROVED_ONLY = frozenset({STATUS_APPROVED, STATUS_CANONICAL})
# 全图模式的硬安全上限：超过即截断并置 truncated 标志，保护浏览器渲染与 Neo4j 查询
FULL_GRAPH_NODE_CAP = 3000
FULL_GRAPH_EDGE_CAP = 6000
_neo4j_query_offload_semaphore_refs: dict[
    int,
    tuple[weakref.ReferenceType[asyncio.AbstractEventLoop], weakref.ReferenceType[asyncio.Semaphore]],
] = {}


class GraphBuildIncompleteError(RuntimeError):
    def __init__(self, result: dict[str, Any]):
        self.result = result
        failed_details = result.get("failed_details") or []
        detail = failed_details[0].get("error") if failed_details else "存在未完成的 Chunk"
        super().__init__(f"图谱索引未全部完成，仍有 {result['remaining']} 个待索引 Chunk：{detail}")


def _dedupe_edges_by_semantic_key(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按语义键去重边：优先 triple_id，否则 (source, type, target)。

    chunk 抽取会把同一规范三元组按来源 chunk 各投影一条边；全图模式承担
    「与规范层一致的统计可视化」职责，必须折叠为每个三元组一条边。
    """
    seen: set[tuple] = set()
    deduped = []
    for edge in edges:
        properties = edge.get("properties") or {}
        triple_id = properties.get("triple_id")
        key = (
            ("triple", triple_id)
            if triple_id
            else (
                "st",
                edge.get("source_id"),
                edge.get("type"),
                edge.get("target_id"),
            )
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)
    return deduped


def _finalize_full_graph_result(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    node_cap: int,
    edge_cap: int,
) -> dict[str, Any]:
    """全图结果收尾（纯函数）：截断判定 -> 节点截断 -> 边按端点过滤 + 截断。

    查询层已按 cap+1 取数，因此 len > cap 即视为触及上限。
    """
    truncated = len(nodes) > node_cap or len(edges) > edge_cap
    final_nodes = nodes[:node_cap]
    node_ids = {node["id"] for node in final_nodes}
    bounded_edges = [edge for edge in edges if edge.get("source_id") in node_ids and edge.get("target_id") in node_ids]
    final_edges = bounded_edges[:edge_cap]
    return {"nodes": final_nodes, "edges": final_edges, "truncated": truncated}


def _merge_extraction_stats(total: dict[str, Any], metadata: dict[str, Any]) -> None:
    """把单个 chunk 的科研抽取 metadata（gates/windows/llm_calls）累加进构建级统计。

    通用 llm 抽取器没有 gates 字段，直接跳过；幻觉率在累加后按总候选重新计算，
    避免对各 chunk 的比率求平均。
    """
    gates = metadata.get("gates")
    if not isinstance(gates, dict):
        return
    total["chunks_with_stats"] = total.get("chunks_with_stats", 0) + 1
    for key in ("windows", "llm_calls"):
        total[key] = total.get(key, 0) + int(metadata.get(key) or 0)
    for key in ("entity_candidates", "relation_candidates", "accepted_entities", "accepted_relations"):
        total[key] = total.get(key, 0) + int(gates.get(key) or 0)
    rejected = total.setdefault("rejected", {})
    for code, count in (gates.get("rejected") or {}).items():
        rejected[code] = rejected.get(code, 0) + int(count or 0)
    relation_candidates = total.get("relation_candidates", 0)
    verbatim_failures = rejected.get("G2_VERBATIM_QUOTE", 0) + rejected.get("G2_MISSING_ENDPOINT", 0)
    total["hallucination_rate"] = round(verbatim_failures / relation_candidates, 4) if relation_candidates else None
    total["identifier_violations"] = rejected.get("G3_IDENTIFIER_PROVENANCE", 0)


def _bind_quote_offsets(
    chunk_content: str, entity_records: list[dict[str, Any]], triple_records: list[dict[str, Any]]
) -> None:
    """把实体/三元组的逐字引文定位到 chunk 原文（偏移供面板高亮；找不到时留 None，显示时做空白折叠校验）。"""
    for record in entity_records:
        quote = record.get("mention_quote") or ""
        position = chunk_content.find(quote) if quote else -1
        record["mention_quote_start"] = position if position >= 0 else None
    for record in triple_records:
        quote = record.get("text") or ""
        position = chunk_content.find(quote) if quote else -1
        record["quote_start_char"] = position if position >= 0 else None


def _assert_mention_evidence(entity_records: list[dict[str, Any]], triple_records: list[dict[str, Any]]) -> None:
    """科研抽取轨写入断言（不变式 I1/I2）：无原文引文的节点/边不允许进入图谱，fail-fast 不回退。"""
    missing_entities = [record["name"] for record in entity_records if not record.get("mention_quote")]
    missing_triples = [record["triple_id"] for record in triple_records if not (record.get("text") or "").strip()]
    if missing_entities or missing_triples:
        raise ValueError(
            f"科研抽取轨拒绝写入缺少原文引文的图元素：实体 {missing_entities[:5]}，三元组 {missing_triples[:5]}"
        )


def _group_by_status(status_by_id: dict[str, str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for target_id, status in status_by_id.items():
        grouped.setdefault(status, []).append(target_id)
    return grouped


def normalized_result_from_snapshot(
    payload: dict[str, Any], quote: str, *, extractor_type: str = "human_pinned"
) -> dict[str, Any]:
    """APPROVE 决策快照 → 与抽取器同形状的 normalized_result（走同一条写入路径重建对象）。"""

    def entity_from(snapshot: dict[str, Any]) -> dict[str, Any]:
        attributes = snapshot.get("attributes")
        return {
            "text": snapshot["name"],
            "label": snapshot["label"],
            "attributes": list(attributes) if isinstance(attributes, list) else [],
            "mention_quote": quote,
        }

    entities: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    if payload.get("entity"):
        entities.append(entity_from(payload["entity"]))
    if payload.get("triple") and payload.get("source") and payload.get("target"):
        source = entity_from(payload["source"])
        target = entity_from(payload["target"])
        entities.extend([source, target])
        relations.append(
            {
                "source": source,
                "target": target,
                "text": quote,
                "label": payload["triple"]["relation_type"],
                "confidence": 1.0,
                "hedge": False,
            }
        )
    return {
        "entities": entities,
        "relations": relations,
        "metadata": {"extractor_type": extractor_type, "schema_version": 2},
    }


def filter_edges_by_policy(result: dict[str, Any], policy: str) -> dict[str, Any]:
    """approved_only 下隐藏 CANDIDATE 边；缺 review_status 属性的边（托管导入投影）视为 CANONICAL 可见。"""
    if policy != REVIEW_POLICY_APPROVED_ONLY:
        return result
    edges = [
        edge
        for edge in result.get("edges") or []
        if ((edge.get("properties") or {}).get("review_status") or STATUS_CANONICAL) in _VISIBLE_UNDER_APPROVED_ONLY
    ]
    return {**result, "edges": edges}


def _get_neo4j_query_offload_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    entry = _neo4j_query_offload_semaphore_refs.get(loop_id)
    if entry is not None:
        loop_ref, semaphore_ref = entry
        semaphore = semaphore_ref()
        if loop_ref() is loop and semaphore is not None:
            return semaphore

    semaphore = asyncio.Semaphore(NEO4J_QUERY_OFFLOAD_LIMIT)

    def cleanup(ref, stale_loop_id=loop_id):
        current_entry = _neo4j_query_offload_semaphore_refs.get(stale_loop_id)
        if current_entry is not None and current_entry[1] is ref:
            _neo4j_query_offload_semaphore_refs.pop(stale_loop_id, None)

    _neo4j_query_offload_semaphore_refs[loop_id] = (weakref.ref(loop), weakref.ref(semaphore, cleanup))
    return semaphore


async def _run_neo4j_query_io(func, /, *args, **kwargs):
    semaphore = _get_neo4j_query_offload_semaphore()
    await semaphore.acquire()
    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))

    def release_capacity(completed_task: asyncio.Task):
        semaphore.release()
        if completed_task.cancelled():
            return
        completed_task.exception()

    task.add_done_callback(release_capacity)
    return await asyncio.shield(task)


class MilvusGraphService:
    def __init__(
        self,
        *,
        kb_id: str | None = None,
        kb_repo: KnowledgeBaseRepository | None = None,
        chunk_repo: KnowledgeChunkRepository | None = None,
        graph_repo: KnowledgeGraphRepository | None = None,
        review_repo: KnowledgeGraphReviewRepository | None = None,
        graph_vector_store: MilvusGraphVectorStore | None = None,
        neo4j_connection: Neo4jConnectionManager | None = None,
        review_policy: str | None = None,
    ):
        self.kb_id = kb_id
        self.kb_repo = kb_repo or KnowledgeBaseRepository()
        self.chunk_repo = chunk_repo or KnowledgeChunkRepository()
        self.graph_repo = graph_repo or KnowledgeGraphRepository()
        self.review_repo = review_repo or KnowledgeGraphReviewRepository()
        self._graph_vector_store = graph_vector_store
        self._connection = neo4j_connection
        # 图查询的审核策略：candidates_visible（默认，候选边显示 + 徽标）
        # / approved_only（主图与检索只含 APPROVED/CANONICAL）
        self.review_policy = review_policy

    @property
    def connection(self) -> Neo4jConnectionManager:
        if self._connection is None:
            self._connection = get_shared_neo4j_connection()
        return self._connection

    @property
    def graph_vector_store(self) -> MilvusGraphVectorStore:
        if self._graph_vector_store is None:
            self._graph_vector_store = MilvusGraphVectorStore()
        return self._graph_vector_store

    @property
    def driver(self):
        return self.connection.driver

    async def get_status(self, kb_id: str, *, tasker: Any = None) -> dict[str, Any]:
        kb = await self._get_milvus_kb(kb_id)
        params = dict(kb.additional_params or {})
        config = params.get(GRAPH_CONFIG_KEY) or {}
        total_chunks, pending_chunks, indexed_chunks, graph_counts = await asyncio.gather(
            self.chunk_repo.count_by_kb_id(kb_id),
            self.chunk_repo.count_graph_pending_by_kb_id(kb_id),
            self.chunk_repo.count_graph_indexed_by_kb_id(kb_id),
            self.graph_repo.count_by_kb_id(kb_id),
        )
        entity_count, relationship_count = graph_counts

        build_task_status = None
        build_task_progress = 0
        build_task_id = None
        build_task_message = None
        build_task_error = None
        build_task_result = None
        if tasker is not None:
            latest_task = await tasker.find_task_by_payload(
                task_type=GRAPH_TASK_TYPE,
                payload_match={"kb_id": kb_id},
            )
            if latest_task:
                build_task_status = latest_task.status
                build_task_progress = round(latest_task.progress)
                build_task_id = latest_task.id
                build_task_message = latest_task.message
                build_task_error = latest_task.error
                build_task_result = latest_task.result
                if latest_task.status == "success" and pending_chunks > 0:
                    build_task_status = "failed"
                    build_task_message = f"历史任务未完成：仍有 {pending_chunks} 个 Chunk 待索引"

        return {
            "kb_id": kb_id,
            "kb_type": kb.kb_type,
            "configured": bool(config),
            "locked": bool(config.get("locked")),
            "config": self._public_config(config),
            "total_chunks": total_chunks,
            "pending_chunks": pending_chunks,
            "indexed_chunks": indexed_chunks,
            "entity_count": entity_count,
            "relationship_count": relationship_count,
            "build_task_id": build_task_id,
            "build_task_status": build_task_status,
            "build_task_progress": build_task_progress,
            "build_task_message": build_task_message,
            "build_task_error": build_task_error,
            "build_task_result": build_task_result,
        }

    async def configure(
        self,
        kb_id: str,
        extractor_type: str,
        extractor_options: dict[str, Any],
        created_by: str,
    ) -> dict:
        kb = await self._get_milvus_kb(kb_id)
        await self._require_graph_contract(kb_id)
        additional_params = dict(kb.additional_params or {})
        existing_config = additional_params.get(GRAPH_CONFIG_KEY) or {}
        normalized_extractor_type = (extractor_type or "").lower()
        if existing_config.get("locked"):
            existing_extractor_type = (existing_config.get("extractor_type") or "").lower()
            if normalized_extractor_type != existing_extractor_type:
                raise ValueError("图谱抽取器类型已锁定，只能修改模型、Schema 等抽取参数")

        extractor_options = extractor_options or {}
        if normalized_extractor_type in LLM_EXTRACTOR_TYPES and extractor_options.get("prompt"):
            raise ValueError("LLM 图谱抽取器不支持自定义完整 Prompt，请使用 schema 配置抽取约束")
        if normalized_extractor_type in LLM_EXTRACTOR_TYPES:
            model_spec = str(extractor_options.get("model_spec") or "").strip()
            model_info = model_cache.get_model_info(model_spec)
            if not model_info or model_info.model_type != "chat":
                raise ValueError(f"不支持的聊天模型: {model_spec or '未选择'}")
            extractor_options = {**extractor_options, "model_spec": model_spec}
        GraphExtractorFactory.create(normalized_extractor_type, extractor_options)
        config = {
            "locked": True,
            "extractor_type": normalized_extractor_type,
            "extractor_options": extractor_options or {},
            "created_at": existing_config.get("created_at") or utc_isoformat(),
            "created_by": existing_config.get("created_by") or created_by,
        }
        if existing_config.get("locked"):
            config["updated_at"] = utc_isoformat()
            config["updated_by"] = created_by
        additional_params[GRAPH_CONFIG_KEY] = config
        await self.kb_repo.update(kb_id, {"additional_params": additional_params})
        return config

    async def build_pending_chunks(
        self,
        kb_id: str,
        *,
        batch_size: int,
        context=None,
        model_spec: str | None = None,
    ) -> dict[str, Any]:
        kb = await self._get_milvus_kb(kb_id)
        await self._require_graph_contract(kb_id)
        config = self._get_locked_config(kb.additional_params or {})
        extractor_options = self._runtime_extractor_options(config)
        if model_spec:
            extractor_options["model_spec"] = model_spec
        extractor = GraphExtractorFactory.create(config["extractor_type"], extractor_options)
        worker_count = self._get_worker_count(config)
        total_pending = await self.chunk_repo.count_graph_pending_by_kb_id(kb_id)
        processed = 0
        attempt_counts: dict[str, int] = {}
        last_errors: dict[str, str] = {}
        extraction_stats: dict[str, Any] = {}
        # 人工审核决策一次性加载进内存（不逐 chunk 打 PG），每 chunk 写入后幂等重放
        review_index = ReviewDecisionIndex(await self.review_repo.list_decisions(kb_id))
        review_replay: dict[str, int] = {"decisions_loaded": len(review_index)}
        write_lock = asyncio.Lock()

        while True:
            if context is not None:
                await context.raise_if_cancelled()
            attempted_in_pass = 0
            after_id: int | None = None

            while True:
                chunks = await self.chunk_repo.list_graph_pending_by_kb_id(
                    kb_id,
                    batch_size,
                    after_id=after_id,
                )
                if not chunks:
                    break
                after_id = chunks[-1].id
                retryable = [
                    chunk for chunk in chunks if attempt_counts.get(chunk.chunk_id, 0) < GRAPH_INDEX_MAX_ATTEMPTS
                ]
                if not retryable:
                    continue

                queue: asyncio.Queue[Any] = asyncio.Queue()
                for chunk in retryable:
                    queue.put_nowait(chunk)

                async def worker() -> None:
                    nonlocal attempted_in_pass, processed
                    while True:
                        if context is not None:
                            await context.raise_if_cancelled()
                        try:
                            chunk = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        attempt_counts[chunk.chunk_id] = attempt_counts.get(chunk.chunk_id, 0) + 1
                        attempted_in_pass += 1
                        try:
                            extraction_result = await self._get_chunk_extraction_result(kb_id, chunk, extractor)
                            async with write_lock:
                                entities, triples = await asyncio.to_thread(
                                    self.write_chunk_graph,
                                    kb_id,
                                    chunk,
                                    extraction_result,
                                )
                                await self.graph_repo.upsert_chunk_graph(
                                    kb_id=kb_id,
                                    file_id=chunk.file_id,
                                    chunk_id=chunk.chunk_id,
                                    entities=entities,
                                    triples=triples,
                                )
                                await self.graph_vector_store.insert_missing_graph_records(
                                    kb_id=kb_id,
                                    embedding_model_spec=kb.embedding_model_spec,
                                    entities=entities,
                                    triples=triples,
                                )
                                await self.chunk_repo.mark_graph_indexed(
                                    chunk.chunk_id,
                                    ent_ids=[entity["entity_id"] for entity in entities],
                                )
                                _merge_extraction_stats(extraction_stats, extraction_result.get("metadata") or {})
                                replayed = await self.replay_review_for_chunk(
                                    kb, chunk, entities, triples, review_index
                                )
                                for key, count in replayed.items():
                                    review_replay[key] = review_replay.get(key, 0) + count
                            processed += 1
                            last_errors.pop(chunk.chunk_id, None)
                        except Exception as exc:
                            last_errors[chunk.chunk_id] = str(exc)
                            logger.error(
                                "Chunk 图谱构建失败 chunk_id={} attempt={}/{}: {}",
                                chunk.chunk_id,
                                attempt_counts[chunk.chunk_id],
                                GRAPH_INDEX_MAX_ATTEMPTS,
                                exc,
                            )
                        finally:
                            queue.task_done()

                        if context is not None:
                            progress = 5.0 + min(90.0, processed / max(total_pending, 1) * 90.0)
                            await context.set_progress(
                                progress,
                                f"图谱索引成功 {processed}/{total_pending}，失败项将自动重试",
                            )

                workers = [asyncio.create_task(worker()) for _ in range(min(worker_count, len(retryable)))]
                try:
                    await asyncio.gather(*workers)
                except Exception:
                    for task in workers:
                        task.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
                    raise

            remaining = await self.chunk_repo.count_graph_pending_by_kb_id(kb_id)
            if remaining == 0:
                return {
                    "kb_id": kb_id,
                    "model_spec": extractor_options.get("model_spec"),
                    "extractor_type": extractor.extractor_type,
                    "success": processed,
                    "failed": 0,
                    "remaining": 0,
                    "failure_attempts": sum(attempt_counts.values()) - processed,
                    "extraction_stats": extraction_stats,
                    "review_replay": review_replay,
                }
            if attempted_in_pass == 0:
                break
            if context is not None:
                await context.set_message(
                    f"仍有 {remaining} 个待索引 Chunk，正在自动重试（最多 {GRAPH_INDEX_MAX_ATTEMPTS} 次）"
                )

        failed_details = [
            {"chunk_id": chunk_id, "error": error[:500]} for chunk_id, error in list(last_errors.items())[:20]
        ]
        result = {
            "kb_id": kb_id,
            "model_spec": extractor_options.get("model_spec"),
            "extractor_type": extractor.extractor_type,
            "success": processed,
            "failed": remaining,
            "remaining": remaining,
            "failure_attempts": sum(attempt_counts.values()) - processed,
            "failed_details": failed_details,
            "extraction_stats": extraction_stats,
            "review_replay": review_replay,
        }
        if context is not None:
            await context.set_result(result)
            await context.set_progress(99.0, f"图谱索引未完成，仍有 {remaining} 个 Chunk 待处理")
        raise GraphBuildIncompleteError(result)

    @staticmethod
    def _get_worker_count(config: dict[str, Any]) -> int:
        if (config.get("extractor_type") or "").lower() not in LLM_EXTRACTOR_TYPES:
            return 1
        try:
            worker_count = int((config.get("extractor_options") or {}).get("concurrency_count") or 1)
        except (TypeError, ValueError):
            return 1
        return max(1, min(worker_count, 1000))

    @staticmethod
    def _runtime_extractor_options(config: dict[str, Any]) -> dict[str, Any]:
        options = dict(config.get("extractor_options") or {})
        options.pop("prompt", None)
        return options

    async def _get_chunk_extraction_result(self, kb_id: str, chunk, extractor: GraphExtractor) -> dict[str, Any]:
        extractor_type = extractor.extractor_type
        if chunk.extraction_result:
            return normalize_extraction_result(chunk.extraction_result, extractor_type)

        extraction_result = await extractor.extract(
            chunk.content,
            chunk_metadata={
                "kb_id": kb_id,
                "chunk_id": chunk.chunk_id,
                "file_id": chunk.file_id,
                "chunk_index": chunk.chunk_index,
            },
        )
        normalized_result = normalize_extraction_result(extraction_result, extractor_type)
        await self.chunk_repo.update_extraction_result(chunk.chunk_id, normalized_result)
        return normalized_result

    # ── 人工审核决策叠加层：重放与投影 ─────────────────────────────

    async def replay_review_for_chunk(
        self,
        kb,
        chunk,
        entity_records: list[dict[str, Any]],
        triple_records: list[dict[str, Any]],
        index: ReviewDecisionIndex,
    ) -> dict[str, int]:
        """chunk 写入后按决策幂等重放：快照恢复 → 缓存状态 → pinned 证据 → Neo4j/Milvus 投影清理。

        决策按内容哈希身份匹配：同一句话再抽一次得到同一 ID，REJECT 决策再次生效、APPROVE 决策
        再次固定证据；本次再生成没产出但决策 pinned 在本 chunk 的对象按快照补回。
        """
        if len(index) == 0:
            return {}
        kb_id = chunk.kb_id
        plan = plan_replay(
            index,
            chunk_id=chunk.chunk_id,
            chunk_content=chunk.content or "",
            entity_records=entity_records,
            triple_records=triple_records,
        )
        if plan.is_empty:
            return {}
        summary: dict[str, int] = {}
        for decision in plan.restore_entities:
            await self.restore_from_decision(kb, chunk, decision)
            summary["entities_restored"] = summary.get("entities_restored", 0) + 1
        for decision in plan.restore_triples:
            await self.restore_from_decision(kb, chunk, decision)
            summary["triples_restored"] = summary.get("triples_restored", 0) + 1
        for status, ids in _group_by_status(plan.triple_status).items():
            await self.review_repo.set_review_status(KIND_TRIPLE, ids, status)
            summary[f"triples_{status.lower()}"] = summary.get(f"triples_{status.lower()}", 0) + len(ids)
        for status, ids in _group_by_status(plan.entity_status).items():
            await self.review_repo.set_review_status(KIND_ENTITY, ids, status)
            summary[f"entities_{status.lower()}"] = summary.get(f"entities_{status.lower()}", 0) + len(ids)
        for kind, target_id in plan.repin:
            decision = index.get(kind, target_id)
            await self.review_repo.pin_mention(
                kind, target_id, chunk.chunk_id, (decision or {}).get("actor_uid") or "system"
            )
        if plan.repin:
            summary["mentions_repinned"] = summary.get("mentions_repinned", 0) + len(plan.repin)
        await asyncio.to_thread(
            self.apply_review_projection,
            kb_id,
            triple_status=plan.triple_status,
            reject_triple_ids=plan.reject_triple_ids,
            reject_entity_ids=plan.reject_entity_ids,
            entity_overrides=plan.entity_overrides,
        )
        if plan.reject_triple_ids or plan.reject_entity_ids:
            await self.graph_vector_store.delete_graph_records(
                kb_id, entity_ids=list(plan.reject_entity_ids), triple_ids=list(plan.reject_triple_ids)
            )
        return summary

    async def restore_from_decision(self, kb, chunk, decision: dict[str, Any]) -> tuple[list[dict], list[dict]]:
        """按 APPROVE 决策快照重建三元组/实体（含 human_pinned 引文 mention），走与抽取相同的写入路径。"""
        normalized = normalized_result_from_snapshot(decision.get("payload") or {}, decision.get("pinned_quote") or "")
        kb_id = chunk.kb_id
        entities, triples = await asyncio.to_thread(self.write_chunk_graph, kb_id, chunk, normalized)
        await self.graph_repo.upsert_chunk_graph(
            kb_id=kb_id, file_id=chunk.file_id, chunk_id=chunk.chunk_id, entities=entities, triples=triples
        )
        await self.graph_vector_store.insert_missing_graph_records(
            kb_id=kb_id, embedding_model_spec=kb.embedding_model_spec, entities=entities, triples=triples
        )
        actor = decision.get("actor_uid") or "system"
        for entity in entities:
            await self.review_repo.pin_mention(KIND_ENTITY, entity["entity_id"], chunk.chunk_id, actor)
        for triple in triples:
            await self.review_repo.pin_mention(KIND_TRIPLE, triple["triple_id"], chunk.chunk_id, actor)
        return entities, triples

    def apply_review_projection(
        self,
        kb_id: str,
        *,
        triple_status: dict[str, str] | None = None,
        reject_triple_ids: list[str] | tuple[str, ...] = (),
        reject_entity_ids: list[str] | tuple[str, ...] = (),
        entity_overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Neo4j 投影按审核态刷新：边属性 review_status 一次刷所有平行边（MERGE 键含 chunk_id），
        REJECTED 边直接删除，REJECTED 实体 DETACH DELETE，RENAME/RETYPE 只写展示属性不改身份。"""
        label = safe_neo4j_label(kb_id)
        status_updates = {t: s for t, s in (triple_status or {}).items() if s != STATUS_REJECTED}
        if not (status_updates or reject_triple_ids or reject_entity_ids or entity_overrides):
            return
        edge_pattern = (
            f"MATCH (:Entity:MilvusKB:`{label}`)-[r:RELATION {{kb_id: $kb_id, triple_id: $triple_id}}]->"
            f"(:Entity:MilvusKB:`{label}`) "
        )

        def query(tx):
            for triple_id, status in status_updates.items():
                tx.run(edge_pattern + "SET r.review_status = $status", kb_id=kb_id, triple_id=triple_id, status=status)
            for triple_id in reject_triple_ids:
                tx.run(edge_pattern + "DELETE r", kb_id=kb_id, triple_id=triple_id)
            for entity_id in reject_entity_ids:
                tx.run(
                    f"MATCH (e:Entity:MilvusKB:`{label}` {{kb_id: $kb_id, entity_id: $entity_id}}) DETACH DELETE e",
                    kb_id=kb_id,
                    entity_id=entity_id,
                )
            for entity_id, override in (entity_overrides or {}).items():
                tx.run(
                    f"MATCH (e:Entity:MilvusKB:`{label}` {{kb_id: $kb_id, entity_id: $entity_id}}) "
                    "SET e.display_name = $display_name, e.label_override = $label_override",
                    kb_id=kb_id,
                    entity_id=entity_id,
                    display_name=override.get("display_name"),
                    label_override=override.get("label"),
                )

        neo4j_write(self.driver, query)

    def count_projected_edges(self, kb_id: str, triple_ids: list[str]) -> int:
        """I4 审计：统计这些三元组在 Neo4j 投影中的平行边数（REJECTED 应为 0）。"""
        if not triple_ids:
            return 0
        label = safe_neo4j_label(kb_id)
        cypher = (
            f"MATCH (:Entity:MilvusKB:`{label}`)-[r:RELATION {{kb_id: $kb_id}}]->(:Entity:MilvusKB:`{label}`) "
            "WHERE r.triple_id IN $triple_ids RETURN count(r) AS edge_count"
        )
        result = neo4j_read(self.driver, cypher, kb_id=kb_id, triple_ids=triple_ids)
        return int(result[0]["edge_count"]) if result else 0

    def delete_chunk_graph_from_neo4j(self, kb_id: str, chunk_id: str) -> None:
        """单 chunk 重抽前清掉该 chunk 的平行边、MENTIONS 与孤儿实体节点（重建时按需再生）。"""
        label = safe_neo4j_label(kb_id)

        def query(tx):
            tx.run(
                f"MATCH (:Entity:MilvusKB:`{label}`)-[r:RELATION {{kb_id: $kb_id, chunk_id: $chunk_id}}]->"
                f"(:Entity:MilvusKB:`{label}`) DELETE r",
                kb_id=kb_id,
                chunk_id=chunk_id,
            )
            tx.run(
                f"MATCH (c:Chunk:MilvusKB:`{label}` {{chunk_id: $chunk_id}})-[m:MENTIONS]->"
                f"(e:Entity:MilvusKB:`{label}`) "
                "DELETE m WITH DISTINCT e WHERE NOT ()-[:MENTIONS]->(e) DETACH DELETE e",
                chunk_id=chunk_id,
            )
            tx.run(f"MATCH (c:Chunk:MilvusKB:`{label}` {{chunk_id: $chunk_id}}) DETACH DELETE c", chunk_id=chunk_id)

        neo4j_write(self.driver, query)

    async def resolve_review_policy(self, kb_id: str) -> str:
        """图查询的审核策略：显式注入 > 知识库 graph_view_settings.review_policy > candidates_visible。"""
        if self.review_policy:
            return self.review_policy
        record = await self.kb_repo.get_by_kb_id(kb_id)
        settings = getattr(record, "graph_view_settings", None) or {}
        policy = settings.get("review_policy") if isinstance(settings, dict) else None
        return policy if policy in REVIEW_POLICIES else REVIEW_POLICY_CANDIDATES_VISIBLE

    def write_chunk_graph(
        self,
        kb_id: str,
        chunk,
        normalized_result: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """将单个 chunk 的抽取结果写入 Neo4j。"""
        label = safe_neo4j_label(kb_id)
        graph_payload = build_graph_payload(normalized_result)
        relation_extractor_type = graph_payload["metadata"].get("extractor_type", "unknown")
        entities = graph_payload["entities"]
        relations = graph_payload["relations"]
        entity_by_id = {entity["id"]: entity for entity in entities}
        entity_records = self._build_entity_records(kb_id, entities)
        entity_record_by_local_id = {
            entity["id"]: record for entity, record in zip(entities, entity_records, strict=True)
        }
        triple_records = self._build_triple_records(kb_id, relations, entity_record_by_local_id, graph_payload)
        _bind_quote_offsets(chunk.content or "", entity_records, triple_records)
        if relation_extractor_type == "llm_scientific":
            _assert_mention_evidence(entity_records, triple_records)
        content_preview = (chunk.content or "")[:300]

        # 预构建 Cypher 模板（同一 chunk 内复用）
        merge_chunk_cypher = cypher_merge_chunk(label)
        merge_entity_cypher = cypher_merge_entity_mention(label)
        merge_relation_cypher = cypher_merge_relation(label)

        def query(tx):
            # 1. MERGE Chunk 节点
            tx.run(
                merge_chunk_cypher,
                chunk_id=chunk.chunk_id,
                file_id=chunk.file_id,
                kb_id=kb_id,
                chunk_index=chunk.chunk_index,
                content_preview=content_preview,
                start_char_pos=chunk.start_char_pos,
                end_char_pos=chunk.end_char_pos,
            )

            # 2. MERGE Entity 节点 + Chunk→Entity (MENTIONS)
            for entity in entities:
                entity_record = entity_record_by_local_id[entity["id"]]
                tx.run(
                    merge_entity_cypher,
                    chunk_id=chunk.chunk_id,
                    file_id=chunk.file_id,
                    kb_id=kb_id,
                    entity_id=entity_record["entity_id"],
                    normalized_name=normalize_entity_name(entity["text"]),
                    entity_label=entity.get("label") or "Entity",
                    name=entity["text"],
                    attributes=json.dumps(entity.get("attributes") or [], ensure_ascii=False),
                )

            # 3. MERGE Entity→Entity (RELATION) 边
            for relation in relations:
                source = entity_by_id[relation["source"]]
                target = entity_by_id[relation["target"]]
                source_record = entity_record_by_local_id[relation["source"]]
                target_record = entity_record_by_local_id[relation["target"]]
                relation_type = relation.get("label") or "RELATED_TO"
                triple_id = compute_triple_id(
                    kb_id,
                    source_record["normalized_name"],
                    source_record["label"],
                    relation_type,
                    target_record["normalized_name"],
                    target_record["label"],
                )
                tx.run(
                    merge_relation_cypher,
                    kb_id=kb_id,
                    chunk_id=chunk.chunk_id,
                    file_id=chunk.file_id,
                    source_name=normalize_entity_name(source["text"]),
                    source_label=source.get("label") or "Entity",
                    target_name=normalize_entity_name(target["text"]),
                    target_label=target.get("label") or "Entity",
                    relation_type=relation_type,
                    triple_id=triple_id,
                    text=relation["text"],
                    extractor_type=relation_extractor_type,
                )

        neo4j_write(self.driver, query)
        return entity_records, triple_records

    def _build_entity_records(self, kb_id: str, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records = []
        for entity in entities:
            label = entity.get("label") or "Entity"
            normalized_name = normalize_entity_name(entity["text"])
            entity_id = compute_entity_id(kb_id, normalized_name, label)
            records.append(
                {
                    "entity_id": entity_id,
                    "kb_id": kb_id,
                    "canonical_identity": f"name:{normalized_name}",
                    "normalized_name": normalized_name,
                    "label": label,
                    "name": entity["text"],
                    "attributes": entity.get("attributes") or [],
                    "aliases": list(entity.get("aliases") or []),
                    "mention_quote": entity.get("mention_quote") or "",
                    "content": normalized_name,
                }
            )
        return records

    def _build_triple_records(
        self,
        kb_id: str,
        relations: list[dict[str, Any]],
        entity_record_by_local_id: dict[str, dict[str, Any]],
        graph_payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        records = []
        seen_triple_ids: set[str] = set()
        extractor_type = graph_payload["metadata"].get("extractor_type", "unknown")
        for relation in relations:
            source_record = entity_record_by_local_id[relation["source"]]
            target_record = entity_record_by_local_id[relation["target"]]
            relation_type = relation.get("label") or "RELATED_TO"
            triple_id = compute_triple_id(
                kb_id,
                source_record["normalized_name"],
                source_record["label"],
                relation_type,
                target_record["normalized_name"],
                target_record["label"],
            )
            if triple_id in seen_triple_ids:
                continue
            seen_triple_ids.add(triple_id)
            content = f"{source_record['normalized_name']} → {relation_type} → {target_record['normalized_name']}"
            record = {
                "triple_id": triple_id,
                "kb_id": kb_id,
                "source_entity_id": source_record["entity_id"],
                "target_entity_id": target_record["entity_id"],
                "relation_type": relation_type,
                "content": content,
                "text": relation["text"],
                "extractor_type": extractor_type,
            }
            for field in RELATION_EVIDENCE_FIELDS:
                if relation.get(field) is not None:
                    record[field] = relation[field]
            records.append(record)
        return records

    async def reset(self, kb_id: str, *, clear_extraction_result: bool, clear_config: bool) -> dict[str, Any]:
        kb = await self._get_milvus_kb(kb_id)
        await asyncio.to_thread(self.delete_graph, kb_id)
        await self.graph_repo.delete_by_kb_id(kb_id)
        reset_chunks = await self.chunk_repo.reset_graph_state_by_kb_id(kb_id, clear_extraction_result)
        if clear_config:
            additional_params = dict(kb.additional_params or {})
            additional_params.pop(GRAPH_CONFIG_KEY, None)
            await self.kb_repo.update(kb_id, {"additional_params": additional_params})
        return {
            "message": "图谱构建状态已重置",
            "status": "success",
            "reset_chunks": reset_chunks,
            "clear_extraction_result": clear_extraction_result,
            "clear_config": clear_config,
        }

    def delete_graph(self, kb_id: str) -> None:
        label = safe_neo4j_label(kb_id)

        def query(tx):
            tx.run(f"MATCH (n:MilvusKB:`{label}`) DETACH DELETE n")

        neo4j_write(self.driver, query)
        self.graph_vector_store.drop_graph_collections(kb_id)

    async def delete_file_graph(self, kb_id: str, file_id: str) -> None:
        orphan_entity_ids, orphan_triple_ids = await self.graph_repo.delete_file_references(file_id)
        await self.graph_vector_store.delete_graph_records(
            kb_id,
            entity_ids=orphan_entity_ids,
            triple_ids=orphan_triple_ids,
        )
        await asyncio.to_thread(self._delete_file_graph_from_neo4j, kb_id, file_id)

    def _delete_file_graph_from_neo4j(self, kb_id: str, file_id: str) -> None:
        label = safe_neo4j_label(kb_id)

        def query(tx):
            tx.run(
                f"""
                MATCH (:Entity:MilvusKB:`{label}`)-[r:RELATION {{kb_id: $kb_id, file_id: $file_id}}]->
                    (:Entity:MilvusKB:`{label}`)
                DELETE r
                """,
                kb_id=kb_id,
                file_id=file_id,
            )
            tx.run(
                f"""
                MATCH (:Chunk:MilvusKB:`{label}` {{kb_id: $kb_id, file_id: $file_id}})-[m:MENTIONS]->
                    (e:Entity:MilvusKB:`{label}`)
                DELETE m
                WITH DISTINCT e
                WHERE NOT ()-[:MENTIONS]->(e)
                DETACH DELETE e
                """,
                kb_id=kb_id,
                file_id=file_id,
            )
            tx.run(
                f"""
                MATCH (c:Chunk:MilvusKB:`{label}` {{kb_id: $kb_id, file_id: $file_id}})
                DETACH DELETE c
                """,
                kb_id=kb_id,
                file_id=file_id,
            )

        neo4j_write(self.driver, query)

    async def query_nodes(
        self,
        kb_id: str | None = None,
        *,
        keyword: str = "",
        max_depth: int = 1,
        max_nodes: int = 50,
        exclude_chunk: bool = False,
    ) -> dict[str, Any]:
        effective_kb_id = kb_id or self.kb_id
        if not effective_kb_id:
            return {"nodes": [], "edges": []}

        label = safe_neo4j_label(effective_kb_id)
        limit = max_nodes
        try:
            policy = await self.resolve_review_policy(effective_kb_id)
            result = await _run_neo4j_query_io(
                self._query_nodes_sync,
                effective_kb_id,
                label,
                keyword,
                limit,
                max_depth,
                exclude_chunk,
            )
            return filter_edges_by_policy(result, policy)
        except Exception as e:
            logger.error(f"Milvus graph query failed: {e}")
            return {"nodes": [], "edges": []}

    def _query_nodes_sync(
        self,
        kb_id: str,
        label: str,
        keyword: str,
        limit: int,
        max_depth: int,
        exclude_chunk: bool,
    ) -> dict[str, Any]:
        with self.driver.session() as session:
            query_params: dict[str, Any] = {
                "keyword": keyword,
                "limit": limit,
            }
            if max_depth > 0:
                max_depth = min(max_depth, 3)
                query_params["path_limit"] = max(limit, 1) * 10
            result = session.run(
                self._build_query(label, keyword, limit, max_depth, exclude_chunk),
                **query_params,
            )
            if max_depth <= 0:
                return self._process_query_result(result, limit, kb_id, exclude_chunk)
            record = result.single()
            if not record:
                return {"nodes": [], "edges": []}
            return self._process_subgraph_record(record, limit, kb_id)

    async def query_full_graph(
        self,
        kb_id: str | None = None,
        *,
        exclude_chunk: bool = False,
    ) -> dict[str, Any]:
        """全图查询：不做种子抽样/路径预算/深度截断，仅受硬安全上限保护。

        与 query_nodes 的可视化子图不同，本方法用于「统计口径与画布一致的全量渲染」，
        返回 truncated 标志提示是否触及硬上限。
        """
        effective_kb_id = kb_id or self.kb_id
        if not effective_kb_id:
            return {"nodes": [], "edges": [], "truncated": False}
        label = safe_neo4j_label(effective_kb_id)
        try:
            policy = await self.resolve_review_policy(effective_kb_id)
            result = await _run_neo4j_query_io(self._query_full_graph_sync, effective_kb_id, label, exclude_chunk)
            return filter_edges_by_policy(result, policy)
        except Exception as e:
            logger.error(f"Milvus full graph query failed: {e}")
            return {"nodes": [], "edges": [], "truncated": False}

    def _query_full_graph_sync(self, kb_id: str, label: str, exclude_chunk: bool) -> dict[str, Any]:
        node_where = "WHERE NOT n:Chunk" if exclude_chunk else ""
        edge_where = "WHERE NOT a:Chunk AND NOT b:Chunk" if exclude_chunk else ""
        node_cypher = f"""
        MATCH (n:MilvusKB:`{label}`)
        {node_where}
        RETURN n
        LIMIT {FULL_GRAPH_NODE_CAP + 1}
        """
        edge_cypher = f"""
        MATCH (a:MilvusKB:`{label}`)-[r]->(b:MilvusKB:`{label}`)
        {edge_where}
        RETURN a, r, b
        LIMIT {FULL_GRAPH_EDGE_CAP + 1}
        """
        with self.driver.session() as session:
            raw_nodes = [record["n"] for record in session.run(node_cypher)]
            raw_edges = [(record["r"]) for record in session.run(edge_cypher)]

        nodes = []
        node_ids = set()
        for raw_node in raw_nodes:
            node = self._normalize_node(raw_node, kb_id)
            if node and node["id"] not in node_ids:
                nodes.append(node)
                node_ids.add(node["id"])
        edges = []
        edge_ids = set()
        for raw_edge in raw_edges:
            edge = self._normalize_edge(raw_edge)
            if edge and edge["id"] not in edge_ids:
                edges.append(edge)
                edge_ids.add(edge["id"])
        edges = _dedupe_edges_by_semantic_key(edges)
        result = _finalize_full_graph_result(nodes, edges, FULL_GRAPH_NODE_CAP, FULL_GRAPH_EDGE_CAP)
        # 去重可能把超限的原始取数压回上限内；截断判定必须以原始取数为准
        if len(raw_nodes) > FULL_GRAPH_NODE_CAP or len(raw_edges) > FULL_GRAPH_EDGE_CAP:
            result["truncated"] = True
        return result

    async def query_seed_subgraph(
        self,
        kb_id: str,
        *,
        entity_ids: list[str],
        max_nodes: int,
    ) -> dict[str, Any]:
        if not entity_ids:
            return {"nodes": [], "edges": []}
        seed_entity_ids = list(dict.fromkeys(entity_ids))
        label = safe_neo4j_label(kb_id)
        cypher = f"""
        MATCH (seed:Entity:MilvusKB:`{label}`)
        WHERE seed.entity_id IN $entity_ids
        MATCH p = (seed)-[*1..2]-(n:MilvusKB:`{label}`)
        WITH p LIMIT $path_limit
        WITH collect(p) AS paths
        UNWIND paths AS node_path
        UNWIND nodes(node_path) AS node
        WITH paths, collect(DISTINCT node) AS graph_nodes
        UNWIND paths AS rel_path
        UNWIND relationships(rel_path) AS rel
        RETURN graph_nodes AS nodes, collect(DISTINCT rel) AS edges
        """
        try:
            policy = await self.resolve_review_policy(kb_id)
            result = await _run_neo4j_query_io(
                self._query_seed_subgraph_sync,
                kb_id,
                cypher,
                seed_entity_ids,
                max_nodes,
            )
            return filter_edges_by_policy(result, policy)
        except Exception as e:
            logger.error(f"Milvus seed subgraph query failed: {e}")
            return {"nodes": [], "edges": []}

    def _query_seed_subgraph_sync(
        self,
        kb_id: str,
        cypher: str,
        entity_ids: list[str],
        max_nodes: int,
    ) -> dict[str, Any]:
        with self.driver.session() as session:
            record = session.run(
                cypher,
                entity_ids=entity_ids,
                path_limit=max(max_nodes, 1) * 4,
            ).single()
            if not record:
                return {"nodes": [], "edges": []}
            return self._process_subgraph_record(record, max_nodes, kb_id)

    async def query_and_rank_chunks_by_ppr(
        self,
        kb_id: str,
        seed_weights: dict[str, float],
        *,
        max_nodes: int,
        top_k: int,
        damping: float,
    ) -> list[tuple[str, float]]:
        if not seed_weights:
            return []
        subgraph = await self.query_seed_subgraph(
            kb_id,
            entity_ids=list(seed_weights.keys()),
            max_nodes=max_nodes,
        )
        return self.rank_chunks_by_ppr(subgraph, seed_weights, top_k=top_k, damping=damping)

    @staticmethod
    def rank_chunks_by_ppr(
        subgraph: dict[str, Any],
        seed_weights: dict[str, float],
        *,
        top_k: int,
        damping: float,
    ) -> list[tuple[str, float]]:
        nodes = subgraph.get("nodes") or []
        edges = subgraph.get("edges") or []
        if not nodes:
            return []

        try:
            import igraph as ig
        except ImportError:
            logger.error("Graph retrieval requires python-igraph. Please install igraph.")
            return []

        node_ids = [node["id"] for node in nodes]
        index_by_id = {node_id: index for index, node_id in enumerate(node_ids)}
        edge_indices = [
            (index_by_id[edge["source_id"]], index_by_id[edge["target_id"]])
            for edge in edges
            if edge.get("source_id") in index_by_id and edge.get("target_id") in index_by_id
        ]
        if not edge_indices:
            return []

        graph = ig.Graph(n=len(nodes), edges=edge_indices, directed=False)
        reset = [0.0] * len(nodes)
        chunk_node_indexes: list[tuple[int, str]] = []
        for index, node in enumerate(nodes):
            properties = node.get("properties") or {}
            if node.get("type") == "Chunk" and properties.get("chunk_id"):
                chunk_node_indexes.append((index, properties["chunk_id"]))
                continue
            entity_id = properties.get("entity_id")
            if entity_id in seed_weights:
                reset[index] = seed_weights[entity_id]

        reset_total = sum(reset)
        if reset_total <= 0 or not chunk_node_indexes:
            return []
        reset = [value / reset_total for value in reset]
        scores = graph.personalized_pagerank(damping=min(max(damping, 0.1), 0.99), reset=reset)
        ranked = sorted(
            ((chunk_id, float(scores[index])) for index, chunk_id in chunk_node_indexes),
            key=lambda item: item[1],
            reverse=True,
        )
        return ranked[:top_k]

    async def get_labels(self, kb_id: str | None = None) -> list[str]:
        effective_kb_id = kb_id or self.kb_id
        if not effective_kb_id:
            return []
        label = safe_neo4j_label(effective_kb_id)

        cypher = f"""
        MATCH (n:MilvusKB:`{label}`)
        UNWIND labels(n) AS node_label
        WITH DISTINCT node_label
        WHERE node_label <> 'MilvusKB' AND node_label <> $kb_id
        RETURN node_label
        ORDER BY node_label
        """
        try:
            records = await _run_neo4j_query_io(self._get_labels_sync, cypher, effective_kb_id)
            return [record["node_label"] for record in records]
        except Exception as e:
            logger.error(f"Failed to get Milvus graph labels: {e}")
            return []

    def _get_labels_sync(self, cypher: str, kb_id: str) -> list[Any]:
        return neo4j_read(self.driver, cypher, kb_id=kb_id)

    async def get_stats(self, kb_id: str | None = None) -> dict[str, Any]:
        effective_kb_id = kb_id or self.kb_id
        if not effective_kb_id:
            return {"total_nodes": 0, "total_edges": 0, "entity_types": []}
        label = safe_neo4j_label(effective_kb_id)

        stats_cypher = f"""
        MATCH (n:MilvusKB:`{label}`)
        WITH count(n) AS node_count
        OPTIONAL MATCH (:MilvusKB:`{label}`)-[r]->(:MilvusKB:`{label}`)
        RETURN node_count, count(r) AS edge_count
        """
        label_cypher = f"""
        MATCH (n:Entity:MilvusKB:`{label}`)
        WITH n.label AS entity_label, count(*) AS count
        RETURN entity_label, count
        ORDER BY count DESC
        """
        try:
            return await _run_neo4j_query_io(self._get_stats_sync, stats_cypher, label_cypher)
        except Exception as e:
            logger.error(f"Failed to get Milvus graph stats: {e}")
            return {"total_nodes": 0, "total_edges": 0, "entity_types": []}

    def _get_stats_sync(self, stats_cypher: str, label_cypher: str) -> dict[str, Any]:
        with self.driver.session() as session:
            stats = session.run(stats_cypher).single()
            label_stats = session.run(label_cypher)
            return {
                "total_nodes": stats["node_count"] if stats else 0,
                "total_edges": stats["edge_count"] if stats else 0,
                "entity_types": [{"type": row["entity_label"], "count": row["count"]} for row in label_stats],
            }

    async def _get_milvus_kb(self, kb_id: str):
        """知识库存在性/类型门禁（只读，get_status/reset 同样经过此处）。

        契约门禁在 :meth:`_require_graph_contract`——只拦截「产生 LLM 自动
        抽图」的写入口（configure/build_pending_chunks）；状态查询与重置
        清理永远可用（2026-09 事故：门禁误放此处导致 pdf_evidence 库的
        索引管理面板 get_status 直接 500）。
        """
        kb = await self.kb_repo.get_by_kb_id(kb_id)
        if kb is None:
            raise ValueError(f"知识库 {kb_id} 不存在")
        if (kb.kb_type or "").lower() != "milvus":
            raise ValueError("仅 Milvus 知识库支持独立图谱构建")
        return kb

    async def _require_graph_contract(self, kb_id: str) -> None:
        """LLM 自动图谱构建的源契约写门禁。

        managed_graph 契约禁止普通 LLM graph-build（设计第七节：规范图谱
        与自动抽取图必须隔离）；pdf_evidence / csv_* 严格契约同样拒绝；
        generic_document 只生成非权威导航投影，legacy 契约保留旧行为。
        仅在产生新抽取内容的写入口调用。
        """
        from yuxi.knowledge.source_contracts import (
            COMMAND_LLM_GRAPH_BUILD,
            SourceContractError,
            load_kb_contract,
        )

        try:
            spec = await load_kb_contract(kb_id)
        except SourceContractError as exc:
            raise ValueError(f"[{exc.error_code}] {exc}") from exc
        if COMMAND_LLM_GRAPH_BUILD not in spec.allowed_commands:
            raise ValueError(
                f"知识源契约 {spec.contract_ref} 不接受 LLM 自动图谱构建；"
                "规范图谱请使用托管 CSV 导入，自动抽图能力即将以独立投影形式提供"
            )

    def _get_locked_config(self, additional_params: dict[str, Any]) -> dict[str, Any]:
        config = additional_params.get(GRAPH_CONFIG_KEY) or {}
        if not config.get("locked"):
            raise ValueError("请先确认并锁定图谱抽取配置")
        if not config.get("extractor_type"):
            raise ValueError("图谱抽取配置缺少 extractor_type")
        return config

    def _public_config(self, config: dict[str, Any]) -> dict[str, Any] | None:
        if not config:
            return None
        return {
            "locked": bool(config.get("locked")),
            "extractor_type": config.get("extractor_type"),
            "extractor_options": self._runtime_extractor_options(config),
            "created_at": config.get("created_at"),
            "created_by": config.get("created_by"),
            "updated_at": config.get("updated_at"),
            "updated_by": config.get("updated_by"),
        }

    @staticmethod
    def _build_where(exclude_chunk: bool, keyword: str) -> str:
        clauses = []
        if exclude_chunk:
            clauses.append("NOT n:Chunk")
        if keyword and keyword != "*":
            clauses.append(
                "(toLower(coalesce(n.name, '')) CONTAINS toLower($keyword)"
                " OR toLower(coalesce(n.content_preview, '')) CONTAINS toLower($keyword)"
                " OR toLower(coalesce(n.chunk_id, '')) CONTAINS toLower($keyword))"
            )
        return "WHERE " + " AND ".join(clauses) if clauses else ""

    def _build_query(self, label: str, keyword: str, limit: int, max_depth: int, exclude_chunk: bool = False) -> str:
        where = self._build_where(exclude_chunk, keyword)

        if max_depth <= 0:
            return f"""
            MATCH (n:MilvusKB:`{label}`)
            {where}
            RETURN n AS h, null AS r, null AS t
            LIMIT $limit
            """

        path_node_filter = f"path_node:MilvusKB AND path_node:`{label}`"
        if exclude_chunk:
            path_node_filter += " AND NOT path_node:Chunk"

        return f"""
        MATCH (n:MilvusKB:`{label}`)
        {where}
        WITH n LIMIT $limit
        WITH collect(n) AS seeds
        UNWIND seeds AS seed
        OPTIONAL MATCH p = (seed)-[*1..{max_depth}]-(m:MilvusKB:`{label}`)
        WHERE all(path_node IN nodes(p) WHERE {path_node_filter})
        WITH seeds, p
        LIMIT $path_limit
        WITH seeds, collect(p) AS paths
        RETURN reduce(path_nodes = [], path IN paths | path_nodes + nodes(path)) + seeds AS nodes,
               reduce(path_edges = [], path IN paths | path_edges + relationships(path)) AS edges
        """

    def _process_query_result(self, result, limit: int, kb_id: str, exclude_chunk: bool = False) -> dict[str, Any]:
        nodes = []
        edges = []
        node_ids = set()
        edge_ids = set()

        for record in result:
            for key in ("h", "t"):
                raw_node = record.get(key)
                if raw_node is None:
                    continue
                node = self._normalize_node(raw_node, kb_id)
                if not node or node["id"] in node_ids:
                    continue
                if exclude_chunk and node.get("type") == "Chunk":
                    continue
                nodes.append(node)
                node_ids.add(node["id"])
            raw_edge = record.get("r")
            if raw_edge is not None:
                edge = self._normalize_edge(raw_edge)
                if edge and edge["id"] not in edge_ids:
                    edges.append(edge)
                    edge_ids.add(edge["id"])
            if len(nodes) >= limit:
                break

        return self._finalize_subgraph_result(nodes, edges, limit)

    def _process_subgraph_record(self, record: Any, limit: int, kb_id: str) -> dict[str, Any]:
        nodes = []
        edges = []
        node_ids = set()
        edge_ids = set()

        for raw_node in record.get("nodes") or []:
            node = self._normalize_node(raw_node, kb_id)
            if not node or node["id"] in node_ids:
                continue
            nodes.append(node)
            node_ids.add(node["id"])
            if len(nodes) >= limit:
                break

        for raw_edge in record.get("edges") or []:
            edge = self._normalize_edge(raw_edge)
            if not edge or edge["id"] in edge_ids:
                continue
            if edge["source_id"] not in node_ids or edge["target_id"] not in node_ids:
                continue
            edges.append(edge)
            edge_ids.add(edge["id"])

        return self._finalize_subgraph_result(nodes, edges, limit)

    @staticmethod
    def _finalize_subgraph_result(
        nodes: list[dict[str, Any]], edges: list[dict[str, Any]], limit: int
    ) -> dict[str, Any]:
        limit = max(0, limit)
        final_nodes = nodes[:limit]
        node_ids = {node["id"] for node in final_nodes}
        final_edges = [
            edge for edge in edges if edge.get("source_id") in node_ids and edge.get("target_id") in node_ids
        ]
        return {"nodes": final_nodes, "edges": final_edges[: limit * 2]}

    def _normalize_node(self, raw_node: Any, kb_id: str | None = None) -> dict[str, Any]:
        if hasattr(raw_node, "element_id"):
            node_id = raw_node.element_id
            labels = list(raw_node.labels)
            properties = dict(raw_node.items())
        elif isinstance(raw_node, dict):
            node_id = raw_node.get("id") or raw_node.get("element_id")
            labels = raw_node.get("labels", [])
            properties = raw_node.get("properties") or {k: v for k, v in raw_node.items() if k not in {"id", "labels"}}
        else:
            return {}

        effective_kb_id = kb_id or self.kb_id
        db_label = properties.get("kb_id") or effective_kb_id
        filtered_labels = [label for label in labels if label not in {"MilvusKB", db_label}]
        # RENAME/RETYPE 决策只写展示属性（display_name/label_override），不改内容哈希身份
        entity_type = (
            "Chunk" if "Chunk" in labels else properties.get("label_override") or properties.get("label", "Entity")
        )
        name = (
            properties.get("display_name")
            or properties.get("name")
            or properties.get("content_preview")
            or properties.get("chunk_id")
            or "Unknown"
        )
        return {
            "id": node_id,
            "name": name,
            "original_id": node_id,
            "type": entity_type,
            "labels": filtered_labels,
            "properties": properties,
            "normalized": {
                "name": name,
                "type": entity_type,
                "source": "milvus",
            },
            "graph_type": "milvus",
        }

    def _normalize_edge(self, raw_edge: Any) -> dict[str, Any]:
        if hasattr(raw_edge, "element_id"):
            edge_id = raw_edge.element_id
            edge_type = raw_edge.type
            source_id = raw_edge.start_node.element_id
            target_id = raw_edge.end_node.element_id
            properties = dict(raw_edge.items())
            edge_type = properties.get("type") or edge_type
        elif isinstance(raw_edge, dict):
            edge_id = raw_edge.get("id")
            edge_type = raw_edge.get("type")
            source_id = raw_edge.get("source_id")
            target_id = raw_edge.get("target_id")
            properties = raw_edge.get("properties", {})
        else:
            return {}

        return {
            "id": edge_id,
            "source_id": source_id,
            "target_id": target_id,
            "type": edge_type,
            "properties": properties,
            "normalized": {
                "type": edge_type,
                "direction": "directed",
            },
        }
