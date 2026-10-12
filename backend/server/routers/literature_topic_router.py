"""Thin HTTP boundary for bibliometric BERTopic runs, inherited literature ACL."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, model_validator

from server.utils.auth_middleware import get_required_user
from yuxi.literature import topic_analysis, topic_legacy
from yuxi.storage.postgres.models_business import User

router = APIRouter(prefix="/metrics/topic-runs", tags=["literature"])


class AnalysisScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_ids: list[str] = Field(default_factory=list, max_length=50)
    only_imports: bool = False
    database: Literal["", "pubmed", "wos", "cnki", "wanfang", "other"] = ""
    query: str = Field(default="", max_length=200)
    year_from: int | None = Field(default=None, ge=1500, le=2100)
    year_to: int | None = Field(default=None, ge=1500, le=2100)


class TopicOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="文献主题分析", min_length=1, max_length=160)
    mode: Literal["papers", "records"] = "papers"
    expected_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    embedding_spec: str = Field(default="", max_length=512)
    chinese_embedding_spec: str = Field(default="", max_length=512)
    allow_remote: bool = False
    chinese_comparison: bool = False
    seed: int = Field(default=42, ge=0, le=2147483647)
    neighbors: int = Field(default=30, ge=3, le=100)
    cluster_sizes: list[int] | None = Field(default=None, min_length=1, max_length=5)
    stability: bool = True

    @model_validator(mode="after")
    def validate_sizes(self):
        if self.cluster_sizes and any(
            type(value) is not int or not 2 <= value <= 10000 for value in self.cluster_sizes
        ):
            raise ValueError("最小簇规模需为 2–10000 的整数")
        return self


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    options: TopicOptions = Field(default_factory=TopicOptions)
    filters: AnalysisScope = Field(default_factory=AnalysisScope)


async def _tenant(user):
    from server.routers.literature_router import _current_tenant

    return await _current_tenant(user)


async def _call(operation):
    try:
        return await operation
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/capabilities")
async def capabilities(current_user: User = Depends(get_required_user)):
    from yuxi.models.providers.cache import model_cache
    from yuxi.storage.redis import get_async_redis_client

    queue = await get_async_redis_client()
    return {
        "worker_available": bool(await queue.get("literature:topic-worker:heartbeat")),
        "schema_version": topic_analysis.VERSION,
        "models": [
            {"spec": info.spec, "name": info.display_name, "model": info.model_id}
            for info in model_cache.get_all_specs("embedding")
            if info.model_id in {"BAAI/bge-m3", "BAAI/bge-large-zh-v1.5"}
        ],
    }


@router.post("/preview")
async def preview(body: StartRequest, current_user: User = Depends(get_required_user)):
    return await _call(
        topic_analysis.preview(await _tenant(current_user), mode=body.options.mode, **body.filters.model_dump())
    )


@router.post("", status_code=202)
async def start(body: StartRequest, current_user: User = Depends(get_required_user)):
    options = body.options.model_dump(exclude_none=True)
    return await _call(topic_analysis.start_run(await _tenant(current_user), options, body.filters.model_dump()))


@router.get("")
async def list_runs(
    offset: int = Query(default=0, ge=0, le=1000000),
    limit: int = Query(default=25, ge=1, le=100),
    current_user: User = Depends(get_required_user),
):
    return await _call(topic_analysis.list_runs(await _tenant(current_user), offset=offset, limit=limit))


@router.post("/import", status_code=202)
async def import_result(
    file: UploadFile,
    name: str = Query(default="四库 BERTopic 历史分析", max_length=160),
    current_user: User = Depends(get_required_user),
):
    # 边界早拒：魔数判型（PK 头，与 journals 按内容判格式的先例一致），
    # 非 ZIP 在进入领域层前直接 422，不靠下游兜底。
    if not topic_legacy.looks_like_zip(file.file):
        raise HTTPException(status_code=422, detail="请上传 ZIP 格式的 BERTopic 离线结果包")
    size = await asyncio.to_thread(file.file.seek, 0, 2)
    if size > 1024 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="离线结果包超过 1 GiB 安全预算")
    await file.seek(0)
    return await _call(topic_legacy.import_archive(await _tenant(current_user), file.file, name=name))


@router.get("/{run_id}")
async def detail(run_id: str, current_user: User = Depends(get_required_user)):
    return await _call(topic_analysis.get_run(await _tenant(current_user), run_id))


@router.post("/{run_id}/cancel")
async def cancel(run_id: str, current_user: User = Depends(get_required_user)):
    return await _call(topic_analysis.cancel_run(await _tenant(current_user), run_id))


@router.post("/{run_id}/rerun", status_code=202)
async def rerun(run_id: str, body: TopicOptions, current_user: User = Depends(get_required_user)):
    return await _call(
        topic_analysis.start_run(
            await _tenant(current_user), body.model_dump(exclude_unset=True, exclude_none=True), {}, reuse_run_id=run_id
        )
    )


@router.get("/{run_id}/data")
async def data(
    run_id: str,
    model: Literal["joint_bge_m3", "chinese_bge_zh", "chinese_bge_m3_comparison"] = "joint_bge_m3",
    topic: int | None = Query(default=None, ge=-1),
    offset: int = Query(default=0, ge=0, le=1000000),
    limit: int = Query(default=25, ge=1, le=100),
    view: Literal["documents", "charts"] = "documents",
    current_user: User = Depends(get_required_user),
):
    return await _call(
        topic_analysis.result_view(
            await _tenant(current_user), run_id, model=model, topic=topic, offset=offset, limit=limit, view=view
        )
    )


@router.get("/{run_id}/export")
async def export(
    run_id: str,
    model: Literal["joint_bge_m3", "chinese_bge_zh", "chinese_bge_m3_comparison"] = "joint_bge_m3",
    current_user: User = Depends(get_required_user),
):
    data = await _call(topic_analysis.export_run(await _tenant(current_user), run_id, model=model))
    return Response(
        data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="document_topics.csv"',
            "X-Content-Type-Options": "nosniff",
        },
    )
