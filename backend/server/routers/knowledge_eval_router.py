from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from server.utils.auth_middleware import get_admin_user
from server.utils.knowledge_access import authorize_knowledge_path, authorize_knowledge_resource
from yuxi.knowledge.eval.benchmark_generation import (
    DEFAULT_BENCHMARK_GENERATION_CONCURRENCY,
    MAX_BENCHMARK_GENERATION_CONCURRENCY,
)
from yuxi.knowledge.eval.service import (
    DatasetStateError,
    EvaluationService,
    FinalizeValidationError,
    ItemValidationError,
)
from yuxi.storage.postgres.models_business import User
from yuxi.utils import logger


evaluation = APIRouter(
    prefix="/evaluation",
    tags=["evaluation"],
    dependencies=[Depends(authorize_knowledge_path)],
)


async def _authorize_dataset(
    service: EvaluationService,
    dataset_id: str,
    current_user: User,
    *,
    manage: bool,
) -> None:
    kb_id = await service.get_dataset_kb_id(dataset_id)
    await authorize_knowledge_resource(current_user, kb_id, manage=manage)


def _eval_http_error(exc: Exception) -> HTTPException:
    """评估服务的领域异常 → HTTP 状态码映射；调用方直接 raise 该返回值。"""
    if isinstance(exc, DatasetStateError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ItemValidationError):
        return HTTPException(status_code=400, detail={"message": str(exc), "fields": exc.fields})
    if isinstance(exc, FinalizeValidationError):
        return HTTPException(status_code=400, detail={"message": "基准校验未通过", "report": exc.report})
    if isinstance(exc, ValueError):
        if "not found" in str(exc).lower():
            return HTTPException(status_code=404, detail=str(exc))
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


class GenerateDatasetRequest(BaseModel):
    name: str = Field(default="自动生成评估数据集", min_length=1, max_length=100)
    description: str = ""
    count: int = Field(default=10, ge=1, le=100)
    neighbors_count: int = Field(default=1, ge=0, le=10)
    concurrency_count: int = Field(
        default=DEFAULT_BENCHMARK_GENERATION_CONCURRENCY,
        ge=1,
        le=MAX_BENCHMARK_GENERATION_CONCURRENCY,
    )
    llm_model_spec: str = Field(..., min_length=1)
    generation_mode: Literal["vector", "graph_enhanced"] = "vector"
    graph_expand_top_k: int = Field(default=1, ge=1, le=3)


class RunEvaluationRequest(BaseModel):
    dataset_id: str = Field(..., min_length=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    retrieval_config: dict[str, Any] = Field(default_factory=dict, alias="model_config")


class CreateDatasetRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    review_required: bool = True


class UpdateDatasetRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    review_required: bool | None = None


class EvidenceEntry(BaseModel):
    file: str = Field(..., min_length=1, max_length=255)
    quote: str | None = Field(default=None, max_length=500)
    page: int | None = Field(default=None, ge=1)


class DatasetItemPayload(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    gold_answer: str | None = Field(default=None, max_length=4000)
    gold_chunk_ids: list[str] = Field(default_factory=list, max_length=20)
    external_id: str | None = Field(default=None, max_length=128)
    answer_type: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=10)
    difficulty: str | None = None
    must_include: list[str] = Field(default_factory=list, max_length=10)
    evidence: list[EvidenceEntry] = Field(default_factory=list, max_length=5)
    source_version: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=1000)


class BatchImportRequest(BaseModel):
    content: str = Field(..., min_length=1)


class ReviewItemsRequest(BaseModel):
    item_ids: list[str] | None = None
    action: Literal["approve", "reject", "reset"]
    reason: str = Field(default="", max_length=500)


class NewDatasetVersionRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)


class ProbeRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    gold_chunk_ids: list[str] = Field(default_factory=list, max_length=20)
    top_k: int = Field(default=5, ge=1, le=10)


class DuplicatesCheckRequest(BaseModel):
    threshold: float = Field(default=0.92, ge=0.5, le=1.0)


@evaluation.post("/databases/{kb_id}/datasets/upload")
async def upload_evaluation_dataset(
    kb_id: str,
    file: UploadFile = File(...),
    name: str = Form(...),
    description: str = Form(""),
    current_user: User = Depends(get_admin_user),
):
    """上传评估数据集"""
    try:
        if not file.filename.endswith(".jsonl"):
            raise HTTPException(status_code=400, detail="仅支持JSONL格式文件")

        service = EvaluationService()
        result = await service.upload_dataset(
            kb_id=kb_id,
            file_content=await file.read(),
            filename=file.filename,
            name=name,
            description=description,
            created_by=current_user.uid,
        )
        return {"message": "success", "data": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"上传评估数据集失败: {e}")
        raise HTTPException(status_code=500, detail=f"上传评估数据集失败: {str(e)}")


@evaluation.get("/databases/{kb_id}/datasets")
async def list_evaluation_datasets(kb_id: str, current_user: User = Depends(get_admin_user)):
    """获取知识库的评估数据集列表"""
    try:
        service = EvaluationService()
        datasets = await service.list_datasets(kb_id)
        return {"message": "success", "data": datasets}
    except Exception as e:
        logger.exception(f"获取评估数据集列表失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取评估数据集列表失败: {str(e)}")


@evaluation.get("/databases/{kb_id}/datasets/{dataset_id}")
async def get_evaluation_dataset(
    kb_id: str,
    dataset_id: str,
    page: int = 1,
    page_size: int = 10,
    status: str | None = Query(None, description="题目状态筛选（draft/approved/rejected）"),
    keyword: str | None = Query(None, max_length=100, description="问题/答案/业务编号关键词"),
    current_user: User = Depends(get_admin_user),
):
    """获取知识库的评估数据集详情"""
    try:
        if page < 1:
            raise HTTPException(status_code=400, detail="页码必须大于0")
        if page_size < 1 or page_size > 100:
            raise HTTPException(status_code=400, detail="每页大小必须在1-100之间")

        service = EvaluationService()
        dataset = await service.get_dataset_detail(kb_id, dataset_id, page, page_size, status=status, keyword=keyword)
        return {"message": "success", "data": dataset}
    except HTTPException:
        raise
    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"获取评估数据集详情失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取评估数据集详情失败: {str(e)}")


@evaluation.get("/datasets/{dataset_id}/download")
async def download_evaluation_dataset(dataset_id: str, current_user: User = Depends(get_admin_user)):
    """导出评估数据集 JSONL"""
    try:
        service = EvaluationService()
        await _authorize_dataset(service, dataset_id, current_user, manage=False)
        export_info = await service.export_dataset_jsonl(dataset_id)
        filename = export_info["filename"]
        return Response(
            content=export_info["content"].encode("utf-8"),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        )
    except HTTPException:
        raise
    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"导出评估数据集失败: {e}")
        raise HTTPException(status_code=500, detail=f"导出评估数据集失败: {str(e)}")


@evaluation.delete("/datasets/{dataset_id}")
async def delete_evaluation_dataset(dataset_id: str, current_user: User = Depends(get_admin_user)):
    """删除评估数据集"""
    try:
        service = EvaluationService()
        await _authorize_dataset(service, dataset_id, current_user, manage=True)
        await service.delete_dataset(dataset_id)
        return {"message": "success", "data": None}
    except HTTPException:
        raise
    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"删除评估数据集失败: {e}")
        raise HTTPException(status_code=500, detail=f"删除评估数据集失败: {str(e)}")


@evaluation.post("/databases/{kb_id}/datasets/generate")
async def generate_evaluation_dataset(
    kb_id: str, request: GenerateDatasetRequest, current_user: User = Depends(get_admin_user)
):
    """自动生成评估数据集"""
    try:
        service = EvaluationService()
        result = await service.generate_dataset(
            kb_id=kb_id,
            name=request.name,
            description=request.description,
            count=request.count,
            neighbors_count=request.neighbors_count,
            concurrency_count=request.concurrency_count,
            llm_model_spec=request.llm_model_spec,
            generation_mode=request.generation_mode,
            graph_expand_top_k=request.graph_expand_top_k,
            created_by=current_user.uid,
        )
        return {"message": "success", "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"生成评估数据集失败: {e}")
        raise HTTPException(status_code=500, detail=f"生成评估数据集失败: {str(e)}")


# ============================================================
# 基准逐条构建（authoring）：draft 态工作台
# ============================================================


@evaluation.post("/databases/{kb_id}/datasets")
async def create_manual_dataset(
    kb_id: str, request: CreateDatasetRequest, current_user: User = Depends(get_admin_user)
):
    """新建空白基准（draft 态，逐条填空构建）"""
    try:
        service = EvaluationService()
        result = await service.create_manual_dataset(
            kb_id=kb_id,
            name=request.name,
            description=request.description,
            review_required=request.review_required,
            created_by=current_user.uid,
        )
        return {"message": "success", "data": result}
    except Exception as e:
        logger.exception(f"新建评估基准失败: {e}")
        raise _eval_http_error(e)


@evaluation.put("/datasets/{dataset_id}")
async def update_evaluation_dataset(
    dataset_id: str, request: UpdateDatasetRequest, current_user: User = Depends(get_admin_user)
):
    """修改基准设置（仅 draft 态）"""
    service = EvaluationService()
    await _authorize_dataset(service, dataset_id, current_user, manage=True)
    try:
        result = await service.update_dataset_settings(
            dataset_id,
            name=request.name,
            description=request.description,
            review_required=request.review_required,
        )
        return {"message": "success", "data": result}
    except Exception as e:
        logger.exception(f"更新评估基准失败: {e}")
        raise _eval_http_error(e)


@evaluation.get("/datasets/{dataset_id}/stats")
async def get_evaluation_dataset_stats(dataset_id: str, current_user: User = Depends(get_admin_user)):
    """基准统计：状态/类型/难度/标签分布与答案覆盖率"""
    service = EvaluationService()
    await _authorize_dataset(service, dataset_id, current_user, manage=False)
    try:
        return {"message": "success", "data": await service.get_dataset_stats(dataset_id)}
    except Exception as e:
        logger.exception(f"获取基准统计失败: {e}")
        raise _eval_http_error(e)


@evaluation.post("/datasets/{dataset_id}/items")
async def add_evaluation_dataset_item(
    dataset_id: str, request: DatasetItemPayload, current_user: User = Depends(get_admin_user)
):
    """新增一条题目（仅 draft 态）"""
    service = EvaluationService()
    await _authorize_dataset(service, dataset_id, current_user, manage=True)
    try:
        result = await service.add_dataset_item(
            dataset_id, request.model_dump(exclude_none=True), operator=current_user.uid
        )
        return {"message": "success", "data": result}
    except Exception as e:
        logger.exception(f"新增评估题目失败: {e}")
        raise _eval_http_error(e)


@evaluation.put("/datasets/{dataset_id}/items/{item_id}")
async def update_evaluation_dataset_item(
    dataset_id: str, item_id: str, request: DatasetItemPayload, current_user: User = Depends(get_admin_user)
):
    """修改一条题目（仅 draft 态；内容变更会使审核状态回到草稿）"""
    service = EvaluationService()
    await _authorize_dataset(service, dataset_id, current_user, manage=True)
    try:
        result = await service.update_dataset_item(
            dataset_id, item_id, request.model_dump(exclude_none=True), operator=current_user.uid
        )
        return {"message": "success", "data": result}
    except Exception as e:
        logger.exception(f"更新评估题目失败: {e}")
        raise _eval_http_error(e)


@evaluation.delete("/datasets/{dataset_id}/items/{item_id}")
async def delete_evaluation_dataset_item(dataset_id: str, item_id: str, current_user: User = Depends(get_admin_user)):
    """删除一条题目（仅 draft 态；删除后行序留空位不复用）"""
    service = EvaluationService()
    await _authorize_dataset(service, dataset_id, current_user, manage=True)
    try:
        result = await service.delete_dataset_item(dataset_id, item_id)
        return {"message": "success", "data": result}
    except Exception as e:
        logger.exception(f"删除评估题目失败: {e}")
        raise _eval_http_error(e)


@evaluation.post("/datasets/{dataset_id}/items/batch")
async def batch_import_evaluation_dataset_items(
    dataset_id: str, request: BatchImportRequest, current_user: User = Depends(get_admin_user)
):
    """向 draft 基准追加导入 JSONL：合法行入库（草稿态），非法行逐行报错"""
    service = EvaluationService()
    await _authorize_dataset(service, dataset_id, current_user, manage=True)
    try:
        result = await service.import_dataset_items(dataset_id, request.content, operator=current_user.uid)
        return {"message": "success", "data": result}
    except Exception as e:
        logger.exception(f"批量导入评估题目失败: {e}")
        raise _eval_http_error(e)


@evaluation.post("/datasets/{dataset_id}/items/review")
async def review_evaluation_dataset_items(
    dataset_id: str, request: ReviewItemsRequest, current_user: User = Depends(get_admin_user)
):
    """批量审核题目：approve / reject（须填原因）/ reset（重新变为草稿）"""
    service = EvaluationService()
    await _authorize_dataset(service, dataset_id, current_user, manage=True)
    try:
        result = await service.review_dataset_items(
            dataset_id,
            item_ids=request.item_ids,
            action=request.action,
            reason=request.reason,
            operator=current_user.uid,
        )
        return {"message": "success", "data": result}
    except Exception as e:
        logger.exception(f"审核评估题目失败: {e}")
        raise _eval_http_error(e)


@evaluation.post("/datasets/{dataset_id}/finalize")
async def finalize_evaluation_dataset(dataset_id: str, current_user: User = Depends(get_admin_user)):
    """完成基准：跑数据集级校验门禁，全部通过后锁定为 completed 才可发起评估"""
    service = EvaluationService()
    await _authorize_dataset(service, dataset_id, current_user, manage=True)
    try:
        result = await service.finalize_dataset(dataset_id, operator=current_user.uid)
        return {"message": "success", "data": result}
    except Exception as e:
        logger.exception(f"完成评估基准失败: {e}")
        raise _eval_http_error(e)


@evaluation.post("/datasets/{dataset_id}/new-version")
async def create_evaluation_dataset_version(
    dataset_id: str, request: NewDatasetVersionRequest, current_user: User = Depends(get_admin_user)
):
    """派生新版本：复制全部题目为新的 draft 基准，父版本保持不可变"""
    service = EvaluationService()
    await _authorize_dataset(service, dataset_id, current_user, manage=True)
    try:
        result = await service.create_dataset_version(dataset_id, name=request.name, operator=current_user.uid)
        return {"message": "success", "data": result}
    except Exception as e:
        logger.exception(f"派生基准新版本失败: {e}")
        raise _eval_http_error(e)


@evaluation.get("/databases/{kb_id}/chunks")
async def list_kb_chunks_for_picker(
    kb_id: str,
    file_id: str = Query(..., min_length=1, description="文件 ID"),
    keyword: str | None = Query(None, max_length=100, description="内容关键词"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_admin_user),
):
    """选块器：列出指定文件的文档块（预览文本 + 关键词过滤 + 分页）"""
    try:
        service = EvaluationService()
        data = await service.list_kb_chunks(kb_id, file_id=file_id, keyword=keyword, page=page, page_size=page_size)
        return {"message": "success", "data": data}
    except Exception as e:
        logger.exception(f"获取文档块列表失败: {e}")
        raise _eval_http_error(e)


@evaluation.post("/datasets/{dataset_id}/gold-chunks/check")
async def check_evaluation_dataset_gold_chunks(dataset_id: str, current_user: User = Depends(get_admin_user)):
    """失效参考块检测：gold_chunk_ids 引用的块已不存在时逐题报告"""
    service = EvaluationService()
    await _authorize_dataset(service, dataset_id, current_user, manage=False)
    try:
        return {"message": "success", "data": await service.check_dataset_gold_chunks(dataset_id)}
    except Exception as e:
        logger.exception(f"检测失效参考块失败: {e}")
        raise _eval_http_error(e)


@evaluation.post("/databases/{kb_id}/probe")
async def probe_kb_question(kb_id: str, request: ProbeRequest, current_user: User = Depends(get_admin_user)):
    """试答探测：按知识库当前检索配置取 top_k 并标注是否命中参考块"""
    try:
        service = EvaluationService()
        data = await service.probe_question(
            kb_id, query=request.query, gold_chunk_ids=request.gold_chunk_ids, top_k=request.top_k
        )
        return {"message": "success", "data": data}
    except Exception as e:
        logger.exception(f"试答探测失败: {e}")
        raise _eval_http_error(e)


@evaluation.post("/datasets/{dataset_id}/duplicates/check")
async def check_evaluation_dataset_duplicates(
    dataset_id: str, request: DuplicatesCheckRequest, current_user: User = Depends(get_admin_user)
):
    """语义近重复检测：用知识库向量模型对全部问题做余弦配对"""
    service = EvaluationService()
    await _authorize_dataset(service, dataset_id, current_user, manage=False)
    try:
        data = await service.check_dataset_duplicates(dataset_id, threshold=request.threshold)
        return {"message": "success", "data": data}
    except Exception as e:
        logger.exception(f"语义查重失败: {e}")
        raise _eval_http_error(e)


@evaluation.post("/databases/{kb_id}/runs")
async def run_evaluation(kb_id: str, request: RunEvaluationRequest, current_user: User = Depends(get_admin_user)):
    """运行RAG评估"""
    # 数据集必须归属路径上的知识库，防止跨库 dataset_id 注入
    service = EvaluationService()
    await _authorize_dataset(service, request.dataset_id, current_user, manage=True)
    if await service.get_dataset_kb_id(request.dataset_id) != kb_id:
        raise HTTPException(status_code=404, detail="数据集不存在")
    try:
        run_id = await service.run_evaluation(
            kb_id=kb_id,
            dataset_id=request.dataset_id,
            name=request.name,
            model_config=request.retrieval_config,
            created_by=current_user.uid,
        )
        return {"message": "success", "data": {"run_id": run_id}}
    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"启动评估失败: {e}")
        raise HTTPException(status_code=500, detail=f"启动评估失败: {str(e)}")


@evaluation.get("/databases/{kb_id}/runs")
async def list_evaluation_runs(kb_id: str, current_user: User = Depends(get_admin_user)):
    """获取知识库评估运行历史"""
    try:
        service = EvaluationService()
        runs = await service.list_runs(kb_id)
        return {"message": "success", "data": runs}
    except Exception as e:
        logger.exception(f"获取评估运行历史失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取评估运行历史失败: {str(e)}")


@evaluation.get("/databases/{kb_id}/runs/{run_id}")
async def get_evaluation_run_results(
    kb_id: str,
    run_id: str,
    page: int = 1,
    page_size: int = 20,
    error_only: bool = False,
    current_user: User = Depends(get_admin_user),
):
    """获取评估运行结果"""
    try:
        if page < 1:
            raise HTTPException(status_code=400, detail="页码必须大于0")
        if page_size < 1 or page_size > 100:
            raise HTTPException(status_code=400, detail="每页大小必须在1-100之间")

        service = EvaluationService()
        results = await service.get_run_results(kb_id, run_id, page=page, page_size=page_size, error_only=error_only)
        return {"message": "success", "data": results}
    except HTTPException:
        raise
    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"获取评估运行结果失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取评估运行结果失败: {str(e)}")


@evaluation.get("/databases/{kb_id}/runs/{run_id}/export")
async def export_evaluation_run_results(
    kb_id: str,
    run_id: str,
    current_user: User = Depends(get_admin_user),
):
    """导出评估结果 xlsx（运行汇总 + 逐题明细：问题/标准答案/检索上下文/生成答案/指标/评判）。"""
    try:
        service = EvaluationService()
        package = await service.export_run_results(kb_id, run_id)
        return Response(
            content=package["content"],
            media_type=package["media_type"],
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(package['filename'])}"},
        )
    except ValueError as e:
        message = str(e)
        raise HTTPException(status_code=404 if "not found" in message.lower() else 400, detail=message)
    except Exception as e:
        logger.exception(f"导出评估结果失败: {e}")
        raise HTTPException(status_code=500, detail=f"导出评估结果失败: {str(e)}")


@evaluation.delete("/databases/{kb_id}/runs/{run_id}")
async def delete_evaluation_run(kb_id: str, run_id: str, current_user: User = Depends(get_admin_user)):
    """删除评估运行"""
    try:
        service = EvaluationService()
        await service.delete_run(kb_id, run_id)
        return {"message": "success", "data": None}
    except ValueError as e:
        if "not found" in str(e).lower():
            raise HTTPException(status_code=404, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"删除评估运行失败: {e}")
        raise HTTPException(status_code=500, detail=f"删除评估运行失败: {str(e)}")
