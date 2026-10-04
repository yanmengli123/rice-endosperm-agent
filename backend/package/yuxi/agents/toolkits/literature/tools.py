"""文献域 Agent 工具（P6b；ADR-0010 决议 11 + ADR-0005 计划先行）。

P1–P5 能力的 Agent 暴露层，**不含业务逻辑**。三条硬边界：

  B1 域隔离：工具**不接受 kb_id / tenant_id 参数**——文献域隔离边界是租户
     （uid → resolve_tenant_id，与 HTTP 层同源）；uid 缺失一律 fail-closed。
  B2 派生物不作证据：工具只返回导航元数据形态（无 content/quote 证据字段）；
     literature_notes 已由 P0 登记为 derived_product，scope_gateway/authority_gate
     自动拒绝其进证据通道——工具层不再重复判定，只保证不构造证据形态。
  B3 计划先行：写操作（request_paper_translation）在体内校验 Run 冻结计划
     （context._turn_execution_plan，knowledge_context 中间件同源）——无计划或
     源策略不允许时 fail-closed 返回明确原因，不静默降级。只读导航工具由
     filter_tools_by_turn_plan 统一过滤，不做重复判定。
"""

from __future__ import annotations

from typing import Any
from functools import wraps
import inspect

from yuxi.literature.access import access_scope, paper_visibility
from yuxi.repositories.user_repository import UserRepository

from langgraph.prebuilt.tool_node import ToolRuntime
from pydantic import BaseModel, ConfigDict, Field

from yuxi.agents.toolkits.registry import tool
from yuxi.literature.notes.service import VALID_LEVELS
from yuxi.literature.translate.service import VALID_VARIANT_KINDS
from yuxi.services.principal import resolve_tenant_id
from yuxi.storage.postgres.manager import pg_manager


def _source_authorized(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        runtime = inspect.signature(func).bind_partial(*args, **kwargs).arguments.get("runtime")
        context = getattr(runtime, "context", None)
        if func.__name__ == "request_paper_translation" and _plan_allows_write(context):
            return await func(*args, **kwargs)
        tenant_id = await _principal_tenant(runtime)
        uid = getattr(context, "uid", None)
        user = await UserRepository().get_by_uid(uid)
        if user is None or getattr(user, "is_disabled", False):
            raise PermissionError("无法验证用户身份（fail-closed）")
        async with access_scope(user, tenant_id) as scope:
            binding = getattr(context, "_literature_session_context", None) or {}
            if binding.get("session_kind") == "paper":
                from dataclasses import replace
                from yuxi.literature.access import _access

                rid = str(binding.get("bound_rid") or "")
                if rid not in scope.rids:
                    raise PermissionError("绑定文献的来源权限已撤销")
                token = _access.set(replace(scope, rids=frozenset({rid})))
                try:
                    return await func(*args, **kwargs)
                finally:
                    _access.reset(token)
            return await func(*args, **kwargs)

    return wrapped


class SearchLiteraturePapersInput(BaseModel):
    # extra=allow: langgraph 注入的 runtime 参数需穿透 pydantic 校验（见类内注释）
    model_config = ConfigDict(extra="allow")

    """文献库检索输入（无 kb_id/tenant_id——B1）。"""

    query: str = Field(default="", description="检索词（匹配标题/DOI，空=全库）")
    kind: str = Field(default="", description="类型筛选：paper/thesis/book/chapter/patent/standard（空=全部）")
    top_k: int = Field(default=10, ge=1, le=50, description="返回条数上限")
    channel: str = Field(
        default="catalog",
        pattern="^(catalog|compiled)$",
        description="catalog 元数据/语义目录；compiled 当前笔记 RRF 导航（不是证据）",
    )


class ReadCompiledNoteInput(BaseModel):
    # extra=allow: langgraph 注入的 runtime 参数需穿透 pydantic 校验（见类内注释）
    model_config = ConfigDict(extra="allow")

    """读取已编译笔记的输入。"""

    rid: str = Field(description="文献资源键（search_literature_papers 返回的 rid）")
    level: str = Field(default="L1", description="笔记级别：L1/L2/L3")


class ListPaperAttachmentsInput(BaseModel):
    # extra=allow: langgraph 注入的 runtime 参数需穿透 pydantic 校验（见类内注释）
    model_config = ConfigDict(extra="allow")

    """依附资料清单输入。"""

    rid: str = Field(description="文献资源键")


class RequestPaperTranslationInput(BaseModel):
    # extra=allow: langgraph 注入的 runtime 参数需穿透 pydantic 校验（见类内注释）
    model_config = ConfigDict(extra="allow")

    """翻译请求输入（写操作，需计划许可）。"""

    rid: str = Field(description="文献资源键")
    variant_kind: str = Field(default="zh", description="变体类型：zh / en_zh / summary")


class GetCitationNeighborsInput(BaseModel):
    # extra=allow: langgraph 注入的 runtime 参数需穿透 pydantic 校验（见类内注释）
    model_config = ConfigDict(extra="allow")

    """引用邻居输入。"""

    rid: str = Field(description="文献资源键")


async def _principal_tenant(runtime: ToolRuntime) -> int:
    """从 Run 上下文解析租户（B1：唯一隔离边界）；uid 缺失 fail-closed。"""
    context = getattr(runtime, "context", None) if runtime is not None else None
    uid = getattr(context, "uid", None) if context is not None else None
    if not uid:
        raise PermissionError("无法从运行上下文解析用户身份（fail-closed）")
    async with pg_manager.get_async_session_context() as session:
        return await resolve_tenant_id(session, uid)


def _plan_allows_write(context: Any) -> str | None:
    """B3：写操作的计划校验。返回 None 表示放行，否则返回拒绝原因。"""
    binding = getattr(context, "_literature_session_context", None) or {}
    if binding.get("session_kind") in {"global-qa", "lit"}:
        return "当前文献会话为只读模式；请转到文献管理或文献专属会话提出写操作"
    raw_plan = getattr(context, "_turn_execution_plan", None) if context is not None else None
    if not isinstance(raw_plan, dict):
        return "本轮无已冻结的执行计划：翻译属写操作，需先制定计划（ADR-0005 计划先行）"
    source = raw_plan.get("source") or {}
    allowed = {str(item) for item in (source.get("allowed_sources") or [])}
    if allowed and not (allowed & {"BIBLIOGRAPHY", "DISCOVERY"}):
        return f"本轮计划源策略不允许文献操作（allowed_sources={sorted(allowed)}）"
    return None


def _nav_item(row: Any) -> dict[str, Any]:
    """导航元数据形态（B2）：只有身份/计数字段，无 content/quote 证据字段。"""
    return {
        "rid": row.rid,
        "kind": row.kind,
        "doi": row.doi,
        "title": row.title,
        "quartile": row.quartile,
    }


@tool(
    category="literature",
    tags=["文献"],
    display_name="检索文献库",
    config_guide="导航工具：按标题/DOI 检索本租户文献库，返回元数据不含正文",
    args_schema=SearchLiteraturePapersInput,
)
@_source_authorized
async def search_literature_papers(
    query: str, kind: str = "", top_k: int = 10, runtime: ToolRuntime = None, channel: str = "catalog"
) -> str:
    """检索当前用户的文献库（元数据导航；不含正文，不可作证据）。

    Args:
        query: 检索词（标题/DOI 子串）
        kind: 类型筛选（paper/thesis/book/...，空=全部）
        top_k: 返回上限（1-50）
        runtime: 运行时注入（含用户身份）
    """
    from sqlalchemy import select

    from yuxi.storage.postgres.models_knowledge import LiteraturePaper

    tenant_id = await _principal_tenant(runtime)
    if channel not in {"catalog", "compiled"}:
        raise ValueError("不支持的文献检索通道")
    if channel == "compiled" or query.strip():
        import json
        from yuxi.literature import retrieval

        if channel == "compiled":
            result = await retrieval.search_compiled(tenant_id, query, kind=kind, limit=min(50, top_k))
        else:
            hits = await retrieval.search_catalog(tenant_id, query, kind=kind, limit=min(50, top_k))
            result = {"count": len(hits), "papers": hits}
        return json.dumps(result, ensure_ascii=False)
    from yuxi.literature.retrieval import escape_like

    like = f"%{escape_like((query or '').strip())}%"
    async with pg_manager.get_async_session_context() as session:
        conditions = [paper_visibility(tenant_id)]
        if like != "%%":
            conditions.append(
                LiteraturePaper.title.ilike(like, escape="\\") | LiteraturePaper.doi.ilike(like, escape="\\")
            )
        if (kind or "").strip():
            conditions.append(LiteraturePaper.kind == kind.strip())
        rows = (
            (await session.execute(select(LiteraturePaper).where(*conditions).limit(max(1, min(50, int(top_k or 10))))))
            .scalars()
            .all()
        )
    if not rows:
        return "文献库中未找到匹配文献。"
    import json

    return json.dumps({"count": len(rows), "papers": [_nav_item(row) for row in rows]}, ensure_ascii=False)


@tool(
    category="literature",
    tags=["文献"],
    display_name="读取编译笔记",
    config_guide="导航工具：读取 L1/L2/L3 笔记的逻辑 URI 与状态（正文经前端资产端点获取）",
    args_schema=ReadCompiledNoteInput,
)
@_source_authorized
async def read_compiled_note(rid: str, level: str = "L1", runtime: ToolRuntime = None) -> str:
    """读取文献的已编译笔记（L1/L2/L3）导航信息：状态与 kbasset URI，不含笔记正文。

    Args:
        rid: 文献资源键
        level: 笔记级别 L1/L2/L3
        runtime: 运行时注入
    """
    from yuxi.literature import readops

    if (level or "").strip().upper() not in VALID_LEVELS:
        return f"非法笔记级别 {level!r}（允许 {VALID_LEVELS}）"
    tenant_id = await _principal_tenant(runtime)
    note_uri = await readops.get_note_uri(tenant_id, rid.strip(), level.strip().upper())
    if not note_uri:
        return f"文献 {rid} 尚无 {level} 笔记（可能未编译或编译未完成）。"
    import json

    data = await readops.read_current_note(tenant_id, rid, level.upper(), max_chars=12000)
    return json.dumps(
        {
            "rid": rid,
            "level": level.upper(),
            "note_uri": note_uri,
            "navigation_excerpt": data,
            "product_kind": "literature_notes",
            "notice": "派生笔记，仅用于导航与理解；不是 EvidenceEnvelope，事实需核验原知识源",
        },
        ensure_ascii=False,
    )


@tool(
    category="literature",
    tags=["文献"],
    display_name="列出依附资料",
    config_guide="Authority 工具：SI/审稿意见等用户原始资料清单（0 API 抽取文本可检索）",
    args_schema=ListPaperAttachmentsInput,
)
@_source_authorized
async def list_paper_attachments(rid: str, runtime: ToolRuntime = None) -> str:
    """列出文献的依附资料（支撑信息/审稿意见/数据；用户原始资料）。

    Args:
        rid: 文献资源键
        runtime: 运行时注入
    """
    from yuxi.literature import readops

    tenant_id = await _principal_tenant(runtime)
    detail = await readops.get_paper_detail(tenant_id, rid.strip())
    if detail is None:
        return f"文献 {rid} 不存在或无权访问。"
    import json

    return json.dumps({"rid": rid, "attachments": detail.get("attachments") or []}, ensure_ascii=False)


@tool(
    category="literature",
    tags=["文献"],
    display_name="请求翻译",
    config_guide="写操作：触发翻译入队（202 异步），需本轮计划允许文献操作",
    args_schema=RequestPaperTranslationInput,
)
@_source_authorized
async def request_paper_translation(rid: str, variant_kind: str = "zh", runtime: ToolRuntime = None) -> str:
    """为文献请求翻译（异步任务；需本轮执行计划允许，写操作 fail-closed）。

    Args:
        rid: 文献资源键
        variant_kind: 变体类型 zh / en_zh / summary
        runtime: 运行时注入
    """
    from yuxi.literature import readops
    from yuxi.literature.translate.service import enqueue_literature_translation

    context = getattr(runtime, "context", None) if runtime is not None else None
    rejection = _plan_allows_write(context)
    if rejection:
        return f"翻译请求被拒绝：{rejection}"
    if (variant_kind or "").strip() not in VALID_VARIANT_KINDS:
        return f"非法变体类型 {variant_kind!r}（允许 {VALID_VARIANT_KINDS}）"
    tenant_id = await _principal_tenant(runtime)
    detail = await readops.get_paper_detail(tenant_id, rid.strip())
    if detail is None:
        return f"文献 {rid} 不存在或无权访问。"
    paper_id = await _paper_id_by_rid(tenant_id, rid.strip())
    result = await enqueue_literature_translation(
        paper_id=paper_id,
        variant_kind=variant_kind.strip(),
        requested_by=str(getattr(context, "uid", "") or ""),
    )
    import json

    return json.dumps({"rid": rid, **result}, ensure_ascii=False)


@tool(
    category="literature",
    tags=["文献"],
    display_name="引用邻居",
    config_guide="导航工具：返回文献的引用邻居与共被引分簇（分数不作证据）",
    args_schema=GetCitationNeighborsInput,
)
@_source_authorized
async def get_citation_neighbors(rid: str, runtime: ToolRuntime = None) -> str:
    """返回文献在引用网络中的邻居与共被引分簇（导航元数据）。

    Args:
        rid: 文献资源键
        runtime: 运行时注入
    """
    from yuxi.literature import graph_service, readops
    import json

    tenant_id = await _principal_tenant(runtime)
    paper = await readops.get_paper_detail(tenant_id, rid.strip())
    if paper is None:
        return f"文献 {rid} 不存在或无权访问。"
    neighbors = await graph_service.graph_neighbors(tenant_id, rid.strip())
    return json.dumps(
        {
            "rid": rid,
            "cited_references": neighbors["references"][:20],
            "cited_by": neighbors["cited_by"][:20],
            "cocitation_cluster": paper.get("cocitation_cluster", 0),
            "paper_rank": paper.get("paper_rank", 0.0),
        },
        ensure_ascii=False,
    )


async def _paper_id_by_rid(tenant_id: int, rid: str) -> int:
    from sqlalchemy import select

    from yuxi.storage.postgres.models_knowledge import LiteraturePaper

    async with pg_manager.get_async_session_context() as session:
        paper = (
            await session.execute(
                select(LiteraturePaper).where(paper_visibility(tenant_id), LiteraturePaper.rid == rid)
            )
        ).scalar_one_or_none()
    if paper is None:
        raise ValueError(f"文献不存在: {rid}")
    return paper.id
