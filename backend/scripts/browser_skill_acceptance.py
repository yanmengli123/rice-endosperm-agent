"""Run a real Chrome acceptance pass through Yuxi's BrowserSkill bridge."""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from yuxi.services.browser_gateway_service import dispatch_browser_command
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import TenantMembership


async def main() -> None:
    async with pg_manager.get_async_session_context() as db:
        membership = (await db.execute(select(TenantMembership).limit(1))).scalar_one()

        async def call(op: str, payload: dict) -> dict:
            return await dispatch_browser_command(
                db,
                tenant_id=membership.tenant_id,
                uid=membership.uid,
                run_id=None,
                op=op,
                payload=payload,
            )

        navigation = await call(
            "navigate",
            {"url": "http://127.0.0.1:8765/browser_acceptance.html"},
        )
        before = await call("read_page", {})
        typed = await call(
            "type",
            {
                "selector": "#browser-acceptance-input",
                "text": "Yuxi Docker Bridge OK",
            },
        )
        clicked = await call("click", {"selector": "#browser-acceptance-button"})
        after = await call("read_page", {})
        screenshot = await call("screenshot", {})
        status = await call("get_status", {})
        ended = await call("end_task", {})

        print(
            json.dumps(
                {
                    "navigate_ok": bool(navigation),
                    "marker_read": "YUXI_BROWSER_ACCEPTANCE_MARKER" in before.get("text", ""),
                    "typed_ok": bool(typed),
                    "clicked_ok": bool(clicked),
                    "result_read": "Yuxi Docker Bridge OK" in after.get("text", ""),
                    "screenshot_png": screenshot.get("data_url", "").startswith("data:image/png;base64,"),
                    "agent_tabs": len(status.get("tabs", [])),
                    "ended": ended.get("ok"),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
