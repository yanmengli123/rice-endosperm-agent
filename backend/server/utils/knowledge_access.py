"""知识库路径级授权依赖：统一覆盖文件、图谱、查询和托管导入端点。"""

from fastapi import Depends, HTTPException, Request

from server.utils.auth_middleware import get_admin_user
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
    current_user: User = Depends(get_admin_user),
) -> None:
    """对所有含 kb_id 的知识管理路由执行同一套读写授权。

    GET 与显式查询端点只需可读；其余 POST/PUT/DELETE 必须可管理。
    不返回 403，以免向跨租户调用者泄露资源是否存在。
    """
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
    if not kb_id:
        return
    is_read = request.method == "GET" or request.url.path.endswith(_READ_ONLY_POST_SUFFIXES)
    await authorize_knowledge_resource(current_user, kb_id, manage=not is_read)
