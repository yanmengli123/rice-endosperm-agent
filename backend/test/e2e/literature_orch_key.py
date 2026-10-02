"""编排器独立 API key 签发（容器内跑）：name=lit-e2e-orch-key，与 pytest 版
e2e 的 lit-e2e-test-key 互不干扰（两个 e2e 会话并行时单行 key 会互踩重签）。
输出 JSON：{"api_key": "...", ...seed 字段透传}。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sys

KEY_NAME = "lit-e2e-orch-key"


async def main() -> int:
    from sqlalchemy import select

    from yuxi.storage.postgres.manager import pg_manager
    from yuxi.storage.postgres.models_business import APIKey, User

    plaintext = "yxkey_lit_orch_" + secrets.token_hex(16)
    async with pg_manager.get_async_session_context() as session:
        admin = (
            await session.execute(select(User).where(User.role == "superadmin").order_by(User.id).limit(1))
        ).scalar_one()
        row = (await session.execute(select(APIKey).where(APIKey.name == KEY_NAME))).scalar_one_or_none()
        if row is None:
            row = APIKey(
                key_hash="",
                key_prefix="",
                name=KEY_NAME,
                user_id=admin.id,
                department_id=admin.department_id,
                purpose="external_agent",
                scopes=["*"],
                created_by="lit-e2e-orch",
                tenant_id=1,
            )
            session.add(row)
        row.key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        row.key_prefix = plaintext[:16]
        row.user_id = admin.id
        row.department_id = admin.department_id
        row.is_enabled = True
        row.expires_at = None
        await session.commit()
    # 透传种子字段（rid/revision_id/paper_id 由调用方传入 argv[1] 的 seed JSON）
    seed = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    seed["api_key"] = plaintext
    print(json.dumps(seed, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
