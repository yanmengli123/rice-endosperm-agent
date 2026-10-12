"""Read the user's offline BERTopic result ZIP as data, never executable models."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import uuid
from collections import Counter, defaultdict
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

import numpy as np
from sqlalchemy import select

from yuxi.literature import topic_analysis as service
from yuxi.literature.access import paper_visibility
from yuxi.literature.topic_contract import (
    MODELS,
    VERSION,
    corpus_digest,
    corpus_summary,
    prepare_document,
    validate_result,
)
from yuxi.storage.postgres.models_knowledge import (
    LiteraturePaper,
    LiteratureReferenceBatch,
    LiteratureReferenceRecord,
    LiteratureTopicRun,
)
from yuxi.utils.datetime_utils import utc_now

MAX_SELECTED_BYTES = 512 * 1024 * 1024


def looks_like_zip(stream) -> bool:
    """入口魔数快查（PK 头）：非 ZIP 在读入任何条目前早拒，不信任扩展名。

    从流头判型（读前归零，ZipFile 与既有测试均按"流自身寻址"语义使用流）；
    不可 seek 的流不做拦截，交给 ZipFile 语义裁决，避免误杀合法输入。
    """
    try:
        origin = stream.tell()
        stream.seek(0)
        head = stream.read(4)
        stream.seek(origin)
        return head[:2] == b"PK"
    except (OSError, ValueError):
        return True


def read_archive(stream) -> dict:
    if not looks_like_zip(stream):
        raise ValueError("请上传 ZIP 格式的 BERTopic 离线结果包")
    try:
        with ZipFile(stream) as archive:
            entries = archive.infolist()
            if len(entries) > 10000:
                raise ValueError("离线结果 ZIP 文件数超过安全预算")
            selected = {}
            total = 0
            for item in entries:
                path = PurePosixPath(item.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in item.filename
                    or ":" in item.filename
                    or item.flag_bits & 1
                ):
                    raise ValueError("ZIP 含不安全路径或加密内容")
                parts = path.parts
                if path.name == "topic_names.json":
                    if "topic_names.json" in selected or item.file_size > 128 * 1024:
                        raise ValueError("历史主题命名文件重复或超限")
                    selected["topic_names.json"] = archive.read(item)
                    continue
                if "outputs" not in parts:
                    continue
                relative = "/".join(parts[parts.index("outputs") + 1 :])
                if relative in {
                    "data_manifest.json",
                    "runtime_versions.json",
                    "joint_bge_m3_embedding_metadata.json",
                    "chinese_bge_zh_embedding_metadata.json",
                    "joint_bge_m3_embeddings.npy",
                    "chinese_bge_zh_embeddings.npy",
                } or (
                    len(relative.split("/")) == 2
                    and relative.split("/")[0] in MODELS
                    and relative.split("/")[1]
                    in {
                        "document_topics.json",
                        "topic_summary.json",
                        "topic_keywords.json",
                        "representative_documents.json",
                        "selected_model_metrics.json",
                        "stability.json",
                        "parameter_comparison.json",
                        "topic_similarity.json",
                        "topic_keywords_over_time.json",
                        "topic_lexical_frequencies.json",
                        "topic_labels.json",
                    }
                ):
                    if relative in selected:
                        raise ValueError("ZIP 含重复结果文件")
                    total += item.file_size
                    if total > MAX_SELECTED_BYTES or item.file_size > 220 * 1024 * 1024:
                        raise ValueError("离线结果展开大小超过安全预算")
                    selected[relative] = archive.read(item)
            if "joint_bge_m3/document_topics.json" not in selected:
                raise ValueError("请上传包含 outputs/joint_bge_m3/document_topics.json 的 BERTopic 结果包")
            return selected
    except (BadZipFile, RuntimeError) as exc:
        raise ValueError("离线结果 ZIP 损坏或不受支持") from exc


async def import_archive(tenant_id: int, stream, *, name: str = "四库 BERTopic 历史分析") -> dict:
    import asyncio

    uid = service._owner(tenant_id)
    files = await asyncio.to_thread(read_archive, stream)
    await asyncio.to_thread(_validate_files, files)
    rows = _json(files, "joint_bge_m3/document_topics.json")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 50000:
        raise ValueError("历史语料数量无效")
    source_hashes = _json(files, "data_manifest.json", {}).get("source_sha256", {})
    async with service.pg_manager.get_async_session_context() as db:
        papers = (
            (
                await db.execute(
                    select(LiteraturePaper).where(paper_visibility(tenant_id), LiteraturePaper.is_reference.is_(False))
                )
            )
            .scalars()
            .all()
        )
        source_rows = (
            await db.execute(
                select(LiteratureReferenceRecord, LiteratureReferenceBatch.source_sha256)
                .join(LiteratureReferenceBatch, LiteratureReferenceBatch.batch_id == LiteratureReferenceRecord.batch_id)
                .where(
                    LiteratureReferenceRecord.tenant_id == tenant_id,
                    LiteratureReferenceBatch.created_by == uid,
                    LiteratureReferenceBatch.source_sha256.in_(list(source_hashes.values())),
                )
            )
        ).all()
    source_aliases = {}
    allowed_rids = {paper.rid for paper in papers}
    for record, sha256 in source_rows:
        if record.rid in allowed_rids:
            source_aliases[sha256, record.record_no] = record
    aliases = defaultdict(set)
    for p in papers:
        for kind, value in (
            ("doi", _doi(p.doi)),
            ("pmid", p.pmid or ""),
            ("title", _title(p.title) + ":" + str(p.pub_year or "")),
        ):
            if value:
                aliases[kind, value].add(p.rid)
    documents = []
    unmatched = 0
    ids = []
    for row in rows:
        key = row.get("record_id")
        if not isinstance(key, str) or not key or len(key) > 160:
            raise ValueError("历史记录 ID 无效")
        ids.append(key)
        matches = set()
        logical_row = re.search(r"_(\d+)$", key)
        occurrence = (
            source_aliases.get((source_hashes.get(row.get("source_file")), int(logical_row.group(1))))
            if logical_row
            else None
        )
        if occurrence:
            original = prepare_document(key, occurrence.rid, occurrence.payload_json)
            if original["text_sha256"] != row.get("text_sha256"):
                raise ValueError("历史源文件行的文本与分析结果不一致")
            for field in ("doi", "pmid"):
                old = occurrence.payload_json.get(field)
                new = row.get(field)
                if (
                    old
                    and new
                    and (str(old).strip() if field == "pmid" else _doi(old))
                    != (str(new).strip() if field == "pmid" else _doi(new))
                ):
                    raise ValueError("历史源文件行的文献身份与分析结果不一致")
            matches.add(occurrence.rid)
        for kind, value in (("doi", _doi(row.get("doi"))), ("pmid", str(row.get("pmid") or ""))):
            if value and not occurrence:
                matches.update(aliases.get((kind, value), set()))
        if not matches:
            matches = aliases.get(
                (
                    "title",
                    _title(row.get("title")) + ":" + str(_integer(row["year"]) if row.get("year") is not None else ""),
                ),
                set(),
            )
        if len(matches) != 1:
            unmatched += 1
            continue
        d = prepare_document(
            key,
            next(iter(matches)),
            {
                **row,
                "year": _integer(row["year"]) if row.get("year") is not None else None,
                "source_fields": occurrence.payload_json.get("source_fields", {}) if occurrence else {},
                "affiliations": occurrence.payload_json.get("affiliations", []) if occurrence else [],
            },
        )
        if d["text_sha256"] != row.get("text_sha256"):
            raise ValueError("历史文本与清理契约不一致，拒绝关联旧向量")
        documents.append(d)
    if len(ids) != len(set(ids)):
        raise ValueError("历史记录 ID 重复")
    if unmatched:
        raise ValueError(f"有 {unmatched} 条历史记录未唯一匹配当前可见文献；请先导入四库原始清单，禁止猜测关联")
    result = {
        "schema_version": VERSION,
        "models": {},
        "method": {
            "origin": "legacy_offline",
            "deduplication": "NONE",
            "model_files_executed": False,
            "versions": _json(files, "runtime_versions.json", {}),
            "source_manifest": {
                k: v for k, v in _json(files, "data_manifest.json", {}).items() if k != "input_directory"
            },
            "topic_labels": "historical explanatory labels; not scientific validation",
        },
    }
    for model in MODELS:
        raw = _json(files, f"{model}/document_topics.json", None)
        if raw is None:
            continue
        expected = [d for d in documents if model == "joint_bge_m3" or d["language_of_text"] == "zh"]
        if [r["record_id"] for r in raw] != [d["document_key"] for d in expected]:
            raise ValueError("历史子模型记录顺序与语料不一致")
        keywords = defaultdict(list)
        for row in _json(files, f"{model}/topic_keywords.json", []):
            keywords[_integer(row["topic"])].append({"term": row["keyword"], "weight": row["ctfidf_weight"]})
        representatives = defaultdict(list)
        for row in _json(files, f"{model}/representative_documents.json", []):
            representatives[_integer(row["topic"])].append(row["record_id"])
        labels = {
            **_json(files, f"{model}/topic_labels.json", {}),
            **_json(files, "topic_names.json", {}).get(model, {}),
        }
        topics = [
            {
                "topic": _integer(r["topic"]),
                "count": _integer(r["count"]),
                "label": labels.get(str(r["topic"]), r["label"]),
                "keywords": keywords[_integer(r["topic"])],
                "representatives": representatives[_integer(r["topic"])],
                "npmi": r.get("npmi_top10"),
            }
            for r in _json(files, f"{model}/topic_summary.json", [])
        ]
        assignments = [
            {
                "document_key": r["record_id"],
                "topic": _integer(r["topic"]),
                "membership": r["cluster_membership_strength"],
                "nearest_topic": _integer(r["nearest_semantic_topic"]),
                "centroid_cosine": r["nearest_topic_cosine"],
                "x": r["umap_1"],
                "y": r["umap_2"],
            }
            for r in raw
        ]
        by_year = defaultdict(Counter)
        for d, a in zip(expected, assignments, strict=True):
            if d.get("year") is not None:
                by_year[d["year"]][a["topic"]] += 1
        result["models"][model] = {
            "topics": topics,
            "assignments": assignments,
            "yearly": [
                {"year": year, "topic": t, "count": count, "denominator": sum(counts.values())}
                for year, counts in sorted(by_year.items())
                for t, count in counts.items()
            ],
            "similarity": [
                {"a": _integer(r["topic_a"]), "b": _integer(r["topic_b"]), "cosine": r["cosine_similarity"]}
                for r in _json(files, f"{model}/topic_similarity.json", [])
            ],
            "selected": _json(files, f"{model}/selected_model_metrics.json", {}),
            "candidates": _json(files, f"{model}/parameter_comparison.json", []),
            "stability": _json(files, f"{model}/stability.json", {}),
            "dynamic": [
                {
                    "topic": _integer(r["Topic"]),
                    "year": _integer(r["Timestamp"]),
                    "count": _integer(r["Frequency"]),
                    "words": r["Words"],
                }
                for r in _json(files, f"{model}/topic_keywords_over_time.json", [])
            ],
            "warnings": ["历史原始记录口径，含重复记录；主题编号不能跨模型比较"],
            "analysis_year": _integer(
                str(_json(files, "data_manifest.json", {}).get("analysis_date", "2026"))[:4], allow_string=True
            ),
        }
        lexical = {
            _integer(r["topic"]): r.get("frequencies", {})
            for r in _json(files, f"{model}/topic_lexical_frequencies.json", [])
        }
        for topic in topics:
            topic["word_frequencies"] = sorted(
                lexical.get(topic["topic"], {}).items(), key=lambda pair: (-pair[1], pair[0])
            )[:100]
        result["models"][model]["hierarchy"] = _hierarchy(topics, result["models"][model]["similarity"])
    validate_result(result, documents)
    run_id = uuid.uuid4().hex
    config = {
        "schema_version": VERSION,
        "origin": "legacy_offline",
        "allow_remote": False,
        "chinese_comparison": "chinese_bge_zh" in result["models"],
        "embedding_objects": {},
        "analysis_year": result["models"]["joint_bge_m3"]["analysis_year"],
    }
    for model in ("joint_bge_m3", "chinese_bge_zh"):
        key = f"{model}_embeddings.npy"
        if key not in files:
            continue
        meta = _json(files, f"{model}_embedding_metadata.json", {})
        if meta.get("model") != ("BAAI/bge-m3" if model == "joint_bge_m3" else "BAAI/bge-large-zh-v1.5"):
            raise ValueError("历史嵌入模型与向量空间契约不一致")
        expected = [d for d in documents if model == "joint_bge_m3" or d["language_of_text"] == "zh"]
        fingerprint = hashlib.sha256(
            (meta.get("model", "") + "\0" + "\n".join(d["text_sha256"] for d in expected)).encode()
        ).hexdigest()
        if meta.get("record_ids") != [d["document_key"] for d in expected] or meta.get("fingerprint") != fingerprint:
            raise ValueError("历史嵌入指纹或记录顺序不匹配")
        await asyncio.to_thread(load_matrix, files[key], len(expected))
        obj = f"tenants/{tenant_id}/literature/topic-runs/{run_id}/vectors/{model}.npy"
        await service.get_minio_client().aupload_file(service.BUCKET, obj, files[key], "application/octet-stream")
        config["embedding_objects"][model] = {"object": obj, "sha256": hashlib.sha256(files[key]).hexdigest()}
    run = LiteratureTopicRun(
        run_id=run_id,
        tenant_id=tenant_id,
        created_by=uid,
        name=name,
        status="PENDING",
        mode="records",
        input_digest=corpus_digest(documents),
        config_json=config,
        progress_json=corpus_summary(documents),
        attempts=0,
        cancel_requested=False,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    async with service.pg_manager.get_async_session_context() as db:
        db.add(run)
        await db.flush()
        await service._save_documents(db, run, documents)
        await db.commit()
    await service.publish_result(run, documents, result)
    return await service.get_run(tenant_id, run_id)


def load_matrix(data: bytes, rows: int):
    stream = io.BytesIO(data)
    version = np.lib.format.read_magic(stream)
    if version == (1, 0):
        shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
    elif version == (2, 0):
        shape, _, dtype = np.lib.format.read_array_header_2_0(stream)
    else:
        raise ValueError("不支持的嵌入 NPY 版本")
    if (
        shape != (rows, 1024)
        or dtype.kind != "f"
        or dtype.itemsize not in (4, 8)
        or len(data) != stream.tell() + rows * 1024 * dtype.itemsize
    ):
        raise ValueError("历史嵌入 NPY 尺寸、类型或字节数无效")
    matrix = np.load(io.BytesIO(data), allow_pickle=False)
    if not np.isfinite(matrix).all() or not np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-4):
        raise ValueError("历史嵌入包含无效或未归一化向量")
    return matrix


def _hierarchy(topics, pairs):
    from scipy.cluster.hierarchy import linkage
    from scipy.spatial.distance import squareform

    ids = sorted(t["topic"] for t in topics)
    if len(ids) < 2:
        return []
    if len(pairs) != len(ids) * (len(ids) - 1) // 2:
        raise ValueError("历史主题相似度矩阵不完整")
    index = {topic: i for i, topic in enumerate(ids)}
    distance = np.zeros((len(ids), len(ids)))
    seen = set()
    for pair in pairs:
        key = tuple(sorted((pair["a"], pair["b"])))
        if (
            key in seen
            or key[0] == key[1]
            or any(topic not in index for topic in key)
            or not np.isfinite(pair["cosine"])
        ):
            raise ValueError("历史主题相似度索引或数值无效")
        seen.add(key)
        distance[index[key[0]], index[key[1]]] = distance[index[key[1]], index[key[0]]] = np.clip(
            1 - pair["cosine"], 0, 2
        )
    return linkage(squareform(distance), method="average").tolist()


def _json(files, name, default=None):
    if name not in files:
        return default
    return json.loads(files[name], parse_constant=lambda value: (_ for _ in ()).throw(ValueError("JSON 含非有限值")))


def _integer(value, *, allow_string=False):
    if allow_string and isinstance(value, str) and re.fullmatch(r"\d{4}", value):
        return int(value)
    if type(value) not in (int, float) or not math.isfinite(value) or value != int(value):
        raise ValueError("历史主题编号、年份或计数必须是整数")
    return int(value)


def _validate_files(files):
    required = {
        "document_topics.json": {
            "record_id",
            "topic",
            "cluster_membership_strength",
            "nearest_semantic_topic",
            "nearest_topic_cosine",
            "umap_1",
            "umap_2",
        },
        "topic_summary.json": {"topic", "count", "label"},
        "topic_keywords.json": {"topic", "keyword", "ctfidf_weight"},
        "representative_documents.json": {"topic", "record_id"},
        "parameter_comparison.json": set(),
        "topic_similarity.json": {"topic_a", "topic_b", "cosine_similarity"},
        "topic_keywords_over_time.json": {"Topic", "Timestamp", "Frequency", "Words"},
        "topic_lexical_frequencies.json": {"topic", "frequencies"},
    }
    for key in files:
        if not key.endswith(".json"):
            continue
        value = _json(files, key)
        name = key.rsplit("/", 1)[-1]
        if name in required:
            if not isinstance(value, list) or any(
                not isinstance(row, dict) or not required[name] <= row.keys() for row in value
            ):
                raise ValueError(f"历史结果 {name} 的结构或必需字段无效")
        elif not isinstance(value, dict):
            raise ValueError(f"历史结果 {name} 必须是 JSON 对象")
        if name == "data_manifest.json":
            hashes = value.get("source_sha256", {})
            if not isinstance(hashes, dict) or any(
                not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in hashes.values()
            ):
                raise ValueError("历史源文件哈希清单无效")
        if name == "topic_lexical_frequencies.json":
            for row in value:
                if not isinstance(row["frequencies"], dict) or any(
                    not isinstance(term, str) or type(count) is not int or count < 0
                    for term, count in row["frequencies"].items()
                ):
                    raise ValueError("历史词频结构或计数无效")


def _doi(value):
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", str(value or "").strip().casefold()).rstrip(".")


def _title(value):
    import unicodedata

    return re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", str(value or "")).casefold())
