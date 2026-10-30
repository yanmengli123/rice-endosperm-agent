"""Public Session credentials never become product management credentials."""

from fastapi import Depends, HTTPException, Request

from server.utils.auth_middleware import get_current_user, get_db, _verify_api_key
from yuxi.services.principal import PrincipalResolutionError, resolve_principal
from yuxi.services.public_identity_service import PublicActor, resolve_app_actor


async def authenticate_public_request(request: Request, db=Depends(get_db)) -> PublicActor:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer ") or not authorization.removeprefix("Bearer "):
        raise HTTPException(401, "需要 Bearer 凭据", headers={"WWW-Authenticate": "Bearer"})
    token = authorization.removeprefix("Bearer ")
    end_user = request.headers.get("x-end-user-id")
    try:
        if token.startswith("yxkey_"):
            actor = await resolve_app_actor(db, token=token, external_id=end_user)
            if actor is not None:
                return actor
            user, key = await _verify_api_key(token, db)
            if user is None:
                raise HTTPException(401, "API Key 不可用")
            if key.purpose not in {"full", "desktop_legacy"}:
                raise HTTPException(403, "Session 接口需要 APP Key 或个人完整权限 Key")
        else:
            user = await get_current_user(request=request, authorization=authorization, db=db)
            key = None
        if end_user is not None:
            raise HTTPException(422, "X-End-User-Id 仅用于绑定应用的 Key")
        if user is None:
            raise HTTPException(401, "凭据不可用")
        if user.department_id is None:
            raise HTTPException(403, "当前用户未绑定部门")
        principal = await resolve_principal(db, user)
        if key is not None:
            if key.tenant_id != principal.tenant_id:
                raise HTTPException(403, "API Key 的租户归属已变化")
            from yuxi.utils.datetime_utils import utc_now_naive

            key.last_used_at = utc_now_naive()
            await db.commit()
        return PublicActor(uid=principal.uid, tenant_id=principal.tenant_id)
    except PrincipalResolutionError as exc:
        raise HTTPException(403, str(exc)) from None
