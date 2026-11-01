from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from yuxi.services.readiness_service import check_readiness
from yuxi.services.run_queue_service import get_redis_client
from yuxi.storage.postgres.manager import pg_manager

readiness = APIRouter(prefix="/system", tags=["system"])


@readiness.get("/ready")
async def system_readiness(request: Request):
    try:
        redis = await get_redis_client()
    except Exception:
        redis = None
    result = await check_readiness(initialized=getattr(request.app.state, "initialized", False),
                                   db_context=pg_manager.get_async_session_context, redis=redis)
    return JSONResponse(result, status_code=200 if result["status"] == "ready" else 503)
