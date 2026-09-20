"""知识库路径级授权依赖：统一覆盖文件、图谱、查询和托管导入端点。"""

from fastapi import Depends, HTTPException, Request

from server.utils.auth_middleware import get_authenticated_user
from yuxi.knowledge.runtime import knowledge_base
from yuxi.storage.postgres.models_business import User

_READ_ONLY_POST_SUFFIXES = ("/query", "/query-test")


def _user_info(current_user: User) -> dict:
    return (
        current_user.to_dict()
        if hasattr(current_user, "to_dict")
        else {
            "uid": getattr(current_user, "uid", None),
            "role": getattr(current_user, "role", None),
            "department_id": getattr(current_user, "department_id", None),
        }
    )


async def authorize_knowledge_resource(
    current_user: User,
    kb_id: str,
    *,
    manage: bool,
) -> None:
    """Authorize a knowledge-base id obtained from a path, query, or related record."""
    allowed = (
        await knowledge_base.check_manageable(_user_info(current_user), kb_id)
        if manage
        else await knowledge_base.check_accessible(_user_info(current_user), kb_id)
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="知识库不存在或无权访问")


async def authorize_knowledge_path(
    request: Request,
    current_user: User = Depends(get_authenticated_user),
) -> None:
    """对所有含 kb_id 的知识管理路由执行同一套读写授权。

    GET 与显式查询端点只需可读；其余 POST/PUT/DELETE 必须可管理。
    不返回 403，以免向跨租户调用者泄露资源是否存在。
    """
    kb_id = await _kb_id_from_request(request)
    if not kb_id:
        return
    is_read = request.method == "GET" or request.url.path.endswith(_READ_ONLY_POST_SUFFIXES)
    await authorize_knowledge_resource(current_user, kb_id, manage=not is_read)


# ── 知识图谱路由：读写授权 ∪ KB 级协作能力（viewer/reviewer/publisher）──

_GRAPH_CAPABILITY_RANK = {"viewer": 0, "reviewer": 1, "publisher": 2, "admin": 3}


async def _kb_id_from_request(request: Request) -> str | None:
    kb_id = request.path_params.get("kb_id") or request.query_params.get("kb_id")
    if not kb_id and request.method != "GET":
        # 部分写端点（如 POST /files/fetch-url）把 kb_id 放在 JSON body 中
        try:
            body = await request.body()
            import json as _json

            parsed = _json.loads(body) if body else None
            if isinstance(parsed, dict):
                candidate = parsed.get("kb_id")
                if isinstance(candidate, str) and candidate.strip():
                    kb_id = candidate.strip()
        except Exception:
            pass  # 非 JSON body 的端点本就不携带 kb_id，交由后续处理
    return kb_id


async def authorize_graph_path(
    request: Request,
    current_user: User = Depends(get_authenticated_user),
) -> None:
    """图谱路由授权：既有 accessible/manageable 语义不变（admin 通道），
    叠加 knowledge_base_members 能力通道——读需 viewer，写需 reviewer。

    share_config 决定"能否看见"，成员表决定"能做什么"；两者任一满足即放行。
    失败仍统一 404，不向跨租户调用者泄露资源是否存在。
    """
    kb_id = await _kb_id_from_request(request)
    if not kb_id:
        return
    is_read = request.method == "GET" or request.url.path.endswith(_READ_ONLY_POST_SUFFIXES)
    allowed = (
        await knowledge_base.check_accessible(_user_info(current_user), kb_id)
        if is_read
        else await knowledge_base.check_manageable(_user_info(current_user), kb_id)
    )
    if allowed:
        return
    uid = str(getattr(current_user, "uid", "") or "")
    capability = None
    if uid:
        from yuxi.repositories.knowledge_graph_review_repository import KnowledgeGraphReviewRepository

        capability = await KnowledgeGraphReviewRepository().get_member_capability(kb_id, uid)
    required = "viewer" if is_read else "reviewer"
    if capability is not None and _GRAPH_CAPABILITY_RANK.get(capability, -1) >= _GRAPH_CAPABILITY_RANK[required]:
        return
    raise HTTPException(status_code=404, detail="知识库不存在或无权访问")
