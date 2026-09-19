"""文档词典服务内核（P0-c Stage B / D2）：解析面之上幂等构建每文件的解释层。

入口形态是「服务内核 + 图谱 build Phase 0」混合：``prepare_files`` 由
``MilvusGraphService.build_pending_chunks`` 在抽取前调用（词典就绪才进 Pass 2），
后续可加 ACTIVATE 后预热入口——两者共用同一内容寻址幂等，天然安全。

失败语义（吸取科研 PDF 流水线「无重试预算/死信」的登记在案教训）：
attempts 由 ``claim_revision`` 在数据库单调递增，超过 ``DOCLEX_MAX_ATTEMPTS``
置终态 FAILED（死信），后续构建跳过该文件的词典注入但**不阻断图谱抽取**——
词典是增强，不是前置条件。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from yuxi.knowledge.graphs.doclex.bracket_triage import BRACKET_TRIAGE_VERSION, classify_brackets
from yuxi.knowledge.graphs.doclex.coref_dictionary import (
    COREF_DICTIONARY_VERSION,
    build_coreference_dictionary,
)
from yuxi.knowledge.graphs.doclex.figure_mentions import FIGURE_MENTION_VERSION
from yuxi.knowledge.graphs.doclex.table_observations import (
    TABLE_OBSERVATION_VERSION,
    observations_from_chunk,
)
from yuxi.knowledge.graphs.doclex.temporal_definitions import (
    TEMPORAL_DEFINITION_VERSION,
    parse_temporal_definitions,
)
from yuxi.knowledge.graphs.extraction_units import strip_provenance_prefix
from yuxi.knowledge.graphs.graph_utils import normalize_entity_name
from yuxi.knowledge.graphs.lexicon import LEXICON_VERSION
from yuxi.repositories.knowledge_doclex_repository import KnowledgeDoclexRepository
from yuxi.repositories.knowledge_graph_repository import KnowledgeGraphRepository
from yuxi.utils import hashstr
from yuxi.utils.logging_config import logger

DOCLEX_VERSION = "doclex_v2"  # v2: 增加 figure mention 索引（R6）
DOCLEX_MAX_ATTEMPTS = 3


def _default_figure_repo() -> Any:
    from yuxi.repositories.knowledge_doclex_repository import KnowledgeDoclexFigureRepository

    return KnowledgeDoclexFigureRepository()


def compute_doclex_fingerprint(parse_revision_id: str) -> str:
    """doclex 修订指纹：算法版本族 + 词典版本 + 解析修订（内容寻址幂等键）。"""
    return hashstr(
        "|".join(
            (
                DOCLEX_VERSION,
                LEXICON_VERSION,
                BRACKET_TRIAGE_VERSION,
                COREF_DICTIONARY_VERSION,
                TEMPORAL_DEFINITION_VERSION,
                FIGURE_MENTION_VERSION,
                TABLE_OBSERVATION_VERSION,
                parse_revision_id,
            )
        ),
        length=40,
    )


class DocLexService:
    def __init__(
        self,
        repo: KnowledgeDoclexRepository | None = None,
        graph_repo: KnowledgeGraphRepository | None = None,
        figure_repo: Any = None,
    ):
        self.repo = repo or KnowledgeDoclexRepository()
        self.graph_repo = graph_repo or KnowledgeGraphRepository()
        self.figure_repo = figure_repo or _default_figure_repo()

    async def prepare_files(self, kb_id: str, file_ids: list[str]) -> dict[str, dict[str, Any]]:
        """图谱构建 Phase 0：确保各文件词典 READY，返回注入载荷 {file_id: {fingerprint, entries}}。

        fingerprint 为 None 表示该文件无 doclex（无活跃解析修订或已死信），
        抽取照常进行、不注入词典。
        """
        prepared: dict[str, dict[str, Any]] = {}
        for file_id in dict.fromkeys(file_ids):
            prepared[file_id] = await self.prepare_file(kb_id, file_id)
        return prepared

    async def prepare_file(self, kb_id: str, file_id: str) -> dict[str, Any]:
        parse_revision_id = await self.repo.get_active_parse_revision_id(file_id)
        if not parse_revision_id:
            return {"fingerprint": None, "entries": []}
        fingerprint = compute_doclex_fingerprint(parse_revision_id)
        revision = await self.repo.get_revision(file_id, fingerprint)
        if revision is None or revision.status not in {"READY", "FAILED"}:
            await self._build(kb_id, file_id, parse_revision_id, fingerprint)
        entries_by_file = await self.repo.list_injection_entries(kb_id, [file_id])
        return {"fingerprint": fingerprint, "entries": entries_by_file.get(file_id) or []}

    async def _build(self, kb_id: str, file_id: str, parse_revision_id: str, fingerprint: str) -> None:
        doclex_id = hashstr(f"doclex:{file_id}:{fingerprint}", length=40)
        attempts = await self.repo.claim_revision(
            {
                "doclex_id": doclex_id,
                "kb_id": kb_id,
                "file_id": file_id,
                "parse_revision_id": parse_revision_id,
                "fingerprint": fingerprint,
                "doclex_version": DOCLEX_VERSION,
                "status": "BUILDING",
                "attempts": 1,
            }
        )
        if attempts > DOCLEX_MAX_ATTEMPTS:
            await self.repo.mark_status(doclex_id, "FAILED", error=f"attempts exhausted ({attempts})")
            return
        try:
            chunk_records = await self.figure_repo.list_chunk_records(file_id)
            text = "\n".join(strip_provenance_prefix(content) for _chunk_id, content in chunk_records)
            if not text.strip():
                await self.repo.replace_artifacts(doclex_id, [], [])
                await self.figure_repo.replace_figure_mentions(doclex_id, [])
                await self.repo.mark_status(doclex_id, "READY", stats={"empty": True})
                return
            annotations = classify_brackets(text)
            coref_entries = build_coreference_dictionary(text)
            definitions = parse_temporal_definitions(text)

            entry_rows = [
                {
                    "doclex_id": doclex_id,
                    "kb_id": kb_id,
                    "file_id": file_id,
                    "entry_kind": annotation.kind,
                    "entry_key": f"{annotation.kind}:{annotation.start}",
                    "surface": annotation.inner[:512],
                    "resolved_name": None,
                    "resolved_label": None,
                    "expansion": (annotation.expansion or None),
                    "quote": annotation.surface,
                    "quote_start": annotation.start,
                    "source": "RULE",
                }
                for annotation in annotations
            ]
            entry_rows.extend(
                {
                    "doclex_id": doclex_id,
                    "kb_id": kb_id,
                    "file_id": file_id,
                    "entry_kind": entry.entry_kind,
                    "entry_key": f"{entry.entry_kind}:{normalize_entity_name(entry.surface)}",
                    "surface": entry.surface[:512],
                    "resolved_name": entry.resolved_name[:512],
                    "resolved_label": entry.resolved_label,
                    "expansion": None,
                    "quote": (entry.quote or "")[:2000],
                    "quote_start": None,
                    "source": entry.source,
                }
                for entry in coref_entries
            )
            # R7c：表格行 → Observation 候选（确定性解析；图谱写入链路后续批次接入）
            table_observation_rows = []
            for chunk_id, content in chunk_records:
                for observation in observations_from_chunk(chunk_id, strip_provenance_prefix(content)):
                    table_observation_rows.append(
                        {
                            "doclex_id": doclex_id,
                            "kb_id": kb_id,
                            "file_id": file_id,
                            "entry_kind": "TABLE_OBSERVATION",
                            "entry_key": f"TABLE_OBSERVATION:{chunk_id}:{observation.row_index}",
                            "surface": f"{observation.caption or 'table'} row {observation.row_index}",
                            "resolved_name": None,
                            "resolved_label": "Observation",
                            "expansion": None,
                            "quote": "; ".join(f"{key}={value}" for key, value in observation.row.items())[:2000],
                            "quote_start": None,
                            "source": "RULE",
                        }
                    )
            entry_rows.extend(table_observation_rows[:2000])
            definition_rows = [
                {
                    "definition_id": hashstr(
                        f"def:{kb_id}:{normalize_entity_name(definition.entity_name)}:"
                        f"{definition.interval_start}:{definition.interval_end}:{definition.interval_unit}:{file_id}",
                        length=40,
                    ),
                    "doclex_id": doclex_id,
                    "kb_id": kb_id,
                    "file_id": file_id,
                    "entity_name": definition.entity_name[:512],
                    "entity_normalized": normalize_entity_name(definition.entity_name),
                    "entity_label": definition.entity_label,
                    "interval_start": definition.interval_start,
                    "interval_end": definition.interval_end,
                    "interval_unit": definition.interval_unit,
                    "ref_event": definition.ref_event,
                    "quote": definition.quote[:2000],
                    "chunk_id": None,
                }
                for definition in definitions
            ]
            await self.repo.replace_artifacts(doclex_id, entry_rows, definition_rows)
            figure_rows, figure_bound = await self._build_figure_mention_rows(
                kb_id, file_id, doclex_id, chunk_records, parse_revision_id
            )
            await self.figure_repo.replace_figure_mentions(doclex_id, figure_rows)
            await self.repo.mark_status(
                doclex_id,
                "READY",
                stats={
                    "brackets": len(annotations),
                    "brackets_by_kind": dict(Counter(annotation.kind for annotation in annotations)),
                    "dictionary_entries": len(coref_entries),
                    "definitions": len(definitions),
                    "figure_mentions": len(figure_rows),
                    "figure_mentions_bound": figure_bound,
                    "table_observations": len(table_observation_rows),
                },
            )
            await self._register_definition_conflicts(kb_id)
        except Exception as exc:  # noqa: BLE001
            # 不吞成功路径异常以外的信息：失败留在修订行上，达到上限即死信
            logger.error("doclex 构建失败 file_id={} attempt={}/{}: {}", file_id, attempts, DOCLEX_MAX_ATTEMPTS, exc)
            status = "FAILED" if attempts >= DOCLEX_MAX_ATTEMPTS else "BUILDING"
            await self.repo.mark_status(doclex_id, status, error=str(exc))
            raise

    async def _build_figure_mention_rows(
        self,
        kb_id: str,
        file_id: str,
        doclex_id: str,
        chunk_records: list[tuple[str, str]],
        parse_revision_id: str,
    ) -> tuple[list[dict[str, Any]], int]:
        """R6：逐 chunk 解析正文图表 mention，绑定同解析版本的 figure 实体。

        题注行被跳过（那是 caption 通道的地盘）；绑定失败的 mention 保留
        canonical_key 供后续人工/增量绑定，figure_entity_id 为 NULL。
        """
        from yuxi.knowledge.graphs.doclex.figure_mentions import mention_sentence, parse_body_figure_mentions

        mentions_by_chunk: list[tuple[str, Any, str]] = []
        keys: set[str] = set()
        for chunk_id, content in chunk_records:
            body = strip_provenance_prefix(content)
            for mention in parse_body_figure_mentions(body):
                mentions_by_chunk.append((chunk_id, mention, body))
                keys.add(mention.canonical_key)
        entity_ids = await self.figure_repo.figure_entity_ids_by_keys(kb_id, parse_revision_id, sorted(keys))
        rows = []
        bound = 0
        for chunk_id, mention, body in mentions_by_chunk:
            figure_entity_id = entity_ids.get(mention.canonical_key)
            if figure_entity_id is not None:
                bound += 1
            rows.append(
                {
                    "mention_id": hashstr(
                        f"figmention:{file_id}:{chunk_id}:{mention.canonical_key}:{mention.start}", length=40
                    ),
                    "doclex_id": doclex_id,
                    "kb_id": kb_id,
                    "file_id": file_id,
                    "chunk_id": chunk_id,
                    "surface": mention.surface,
                    "canonical_key": mention.canonical_key,
                    "figure_entity_id": figure_entity_id,
                    "start_char": mention.start,
                    "quote": mention_sentence(body, mention)[:1000],
                }
            )
        return rows, bound

    async def _register_definition_conflicts(self, kb_id: str) -> int:
        """B3/D6：同一发育期实体在同一单位下的区间口径不一致 → DEFINITION 冲突。

        冲突判定是「口径不一致」而非「区间不相交」——10-25 与 12-25 dDAH 部分
        重叠但仍是两个文献的两种定义，回答层应并陈而非静默取其一。
        """
        rows = await self.repo.list_definitions(kb_id)
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault((row["entity_normalized"], row["interval_unit"] or ""), []).append(
                {
                    "file_id": row["file_id"],
                    "filename": row.get("filename"),
                    "publish_year": row.get("publish_year"),
                    "entity_name": row["entity_name"],
                    "start": row["interval_start"],
                    "end": row["interval_end"],
                    "unit": row["interval_unit"],
                    "quote": (row.get("quote") or "")[:500],
                }
            )
        payloads: list[dict[str, Any]] = []
        for (entity_normalized, unit), definitions in grouped.items():
            intervals = sorted({(item["start"], item["end"]) for item in definitions})
            if len(intervals) < 2:
                continue
            payloads.append(
                {
                    "conflict_id": hashstr(f"DEFINITION:{kb_id}:{entity_normalized}:{unit}:{intervals}", length=40),
                    "kind": "DEFINITION",
                    "subject_ref": entity_normalized[:128],
                    "detail": {
                        "entity": entity_normalized,
                        "unit": unit,
                        "intervals": intervals,
                        "definitions": definitions,
                    },
                }
            )
        if payloads:
            return await self.graph_repo.register_conflicts(kb_id, payloads)
        return 0


async def prewarm_doclex_for_kb(ctx: dict[str, Any], kb_id: str) -> dict[str, Any]:
    """R6b ARQ 预热：ACTIVATE 后把 KB 下有活跃解析修订的文件词典提前构建好。

    失败语义：尽力而为——预热失败不告警不重试（幂等服务内核可重入，图谱
    构建 Phase 0 兜底正确性），预热只优化首次构建时延。信号量并发 2。
    """
    import asyncio

    from sqlalchemy import select as sa_select

    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_knowledge import KnowledgeFile

    del ctx
    async with pg_manager.get_async_session_context() as session:
        file_ids = list(

                (
                    await session.execute(
                        sa_select(KnowledgeFile.file_id).where(
                            KnowledgeFile.kb_id == kb_id,
                            KnowledgeFile.active_parse_revision_id.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()

        )
    service = DocLexService()
    semaphore = asyncio.Semaphore(2)
    prepared: dict[str, Any] = {}

    async def warm(file_id: str) -> None:
        async with semaphore:
            try:
                payload = await service.prepare_file(kb_id, file_id)
                prepared[file_id] = bool(payload.get("fingerprint"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("doclex 预热失败（Phase 0 会兜底） file_id={}: {}", file_id, exc)
                prepared[file_id] = False

    await asyncio.gather(*(warm(file_id) for file_id in file_ids))
    return {
        "kb_id": kb_id,
        "files": len(file_ids),
        "with_dictionary": sum(1 for value in prepared.values() if value),
    }


def doclex_prewarm_job_id(kb_id: str, parse_revision_id: str) -> str:
    """预热 job 幂等键：同 KB 同解析修订只入队一次。"""
    return f"doclex-prewarm:{kb_id}:{parse_revision_id}"
