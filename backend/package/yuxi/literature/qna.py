"""Explicit native-chat answer writeback; reasoning and tool payloads never become cards."""

import hashlib

from sqlalchemy import func, or_, select

from yuxi.literature import cards
from yuxi.literature.access import build_access, current_access
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import AgentRun, Conversation, Message
from yuxi.utils.reasoning_visibility import sanitize_visible_text


async def answer_preview(tenant_id: int, uid: str, thread_id: str, *, message_id: int | None = None) -> dict:
    scope = current_access()
    if scope is None or scope.user.uid != uid or scope.tenant_id != tenant_id:
        raise PermissionError("问答卡片必须绑定当前请求者")
    fresh = await build_access(scope.user, tenant_id)
    async with pg_manager.get_async_session_context() as db:
        conversation = (
            await db.execute(
                select(Conversation).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.uid == uid,
                    Conversation.thread_id == thread_id,
                    Conversation.status == "active",
                )
            )
        ).scalar_one_or_none()
        binding = (conversation.extra_metadata or {}).get("literature_session") if conversation else None
        if not isinstance(binding, dict) or binding.get("kind") not in {"paper", "global-qa", "manage", "lit"}:
            raise ValueError("文献会话不存在")
        rid = str(binding.get("rid") or "") if binding["kind"] == "paper" else ""
        if rid and rid not in fresh.rids:
            raise PermissionError("绑定文献来源访问权已撤销")
        query = (
            select(Message)
            .outerjoin(AgentRun, AgentRun.id == Message.run_id)
            .where(
                Message.conversation_id == conversation.id,
                Message.role == "assistant",
                Message.message_type == "text",
                Message.delivery_status == "complete",
                Message.content != "",
                func.coalesce(Message.extra_metadata["historical_unverified"].as_boolean(), False).is_(False),
                or_(Message.run_id.is_(None), AgentRun.status == "completed"),
            )
            .order_by(Message.id.desc())
            .limit(1)
        )
        if message_id is not None:
            query = query.where(Message.id == message_id)
        answer = (await db.execute(query)).scalar_one_or_none()
        if answer is None:
            raise ValueError("尚无完整、成功交付的回答可写回")
        question = (
            await db.execute(
                select(Message.content)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.role == "user",
                    Message.id < answer.id,
                )
                .order_by(Message.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none() or ""
        visible = sanitize_visible_text(answer.content).strip()
        if not visible:
            raise ValueError("回答只有内部推理，没有可保存正文")
        title = str(conversation.title or "问答")[:512]
        content = f"## 问题\n\n{question}\n\n## 回答\n\n{visible}"
        if len(content) > 200_000:
            raise ValueError("问答内容超过卡片大小限制")
        digest = hashlib.sha256(content.encode()).hexdigest()
        return {"rid": rid, "message_id": answer.id, "title": title, "content": content, "digest": digest}


async def writeback(tenant_id: int, uid: str, thread_id: str, *, message_id: int, expected_digest: str) -> dict:
    preview = await answer_preview(tenant_id, uid, thread_id, message_id=message_id)
    if preview["digest"] != expected_digest:
        raise cards.CardConflictError("问答内容已变更，请重新预览")
    slug = f"qa-{hashlib.sha256(thread_id.encode()).hexdigest()[:16]}-{message_id}"
    # Deterministic identity gives safe retry. Never overwrite a human edit.
    for card in await cards.list_cards(tenant_id, preview["rid"], card_type="card-qa"):
        if card["slug"] == slug:
            if card["content"] != preview["content"]:
                raise cards.CardConflictError("写回卡片已有人工修改；未覆盖任何内容")
            return {"rid": preview["rid"], "slug": slug, "reused": True}
    result = await cards.write_card(
        tenant_id,
        preview["rid"],
        card_type="card-qa",
        title=preview["title"],
        content=preview["content"],
        slug=slug,
    )
    return {**result, "reused": False}
