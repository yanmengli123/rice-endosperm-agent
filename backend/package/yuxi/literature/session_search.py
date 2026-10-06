"""Owner-private, visible conversation text search; never a source of evidence."""

from sqlalchemy import Integer, cast, func, literal_column, or_, select

from yuxi.literature.access import build_access, current_access
from yuxi.literature.retrieval import escape_like, tokenize, _excerpt
from yuxi.literature.session import SESSION_KINDS
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import Conversation, Message
from yuxi.utils.reasoning_visibility import sanitize_visible_text


async def search_history(tenant_id: int, uid: str, query: str, *, archived=False, offset=0, limit=20) -> dict:
    offset, limit = max(0, offset), max(1, min(limit, 100))
    scope = current_access()
    if scope is None or (scope.tenant_id, scope.user.uid) != (tenant_id, uid):
        raise PermissionError("历史检索需要当前用户")
    terms = tokenize(query)[:32]
    if not terms:
        return {"items": [], "has_more": False, "next_offset": offset}
    fresh = await build_access(scope.user, tenant_id)
    async with pg_manager.get_async_session_context() as db:
        binding = Conversation.extra_metadata["literature_session"]
        visible = or_(binding["kind"].as_string() != "paper", binding["rid"].as_string().in_(fresh.rids))
        matches = []
        for term in terms:
            columns = (Conversation.title, Message.content)
            if db.get_bind().dialect.name == "postgresql" and term.isascii():
                config = literal_column("'simple'::regconfig")
                matches.append(
                    or_(
                        *(
                            func.to_tsvector(config, func.coalesce(column, "")).op("@@")(
                                func.plainto_tsquery(config, term)
                            )
                            for column in columns
                        )
                    )
                )
            else:
                matches.append(or_(*(column.ilike(f"%{escape_like(term)}%", escape="\\") for column in columns)))
        rank = sum(cast(match, Integer) for match in matches)
        rows = (
            await db.execute(
                select(Conversation.thread_id, Conversation.title, binding, Message.id, Message.content)
                .join(Message, Message.conversation_id == Conversation.id)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.uid == uid,
                    Conversation.status == ("archived" if archived else "active"),
                    binding["kind"].as_string().in_(SESSION_KINDS),
                    visible,
                    Message.role.in_(("user", "assistant")),
                    Message.message_type == "text",
                    Message.delivery_status == "complete",
                    func.length(Message.content) <= 200000,
                    or_(*matches),
                )
                .order_by(rank.desc(), Conversation.updated_at.desc(), Message.id.desc())
                .offset(offset)
                .limit(limit + 1)
            )
        ).all()
    now = await build_access(scope.user, tenant_id)
    if fresh.rids != now.rids or fresh.sources != now.sources:
        raise ValueError("历史检索期间来源权限已变更")
    items = []
    for thread_id, title, session, message_id, content in rows[:limit]:
        # Sanitize BEFORE taking a window; a window inside <think> must not leak.
        text = sanitize_visible_text(content)
        if not any(term in (str(title or "") + " " + text).lower() for term in terms):
            continue
        items.append(
            dict(
                id=thread_id,
                title=title,
                session=session,
                message_id=message_id,
                excerpt=_excerpt(text, terms, budget=400),
                authority="HISTORICAL_NAVIGATION_ONLY",
            )
        )
    return {"items": items, "has_more": len(rows) > limit, "next_offset": offset + min(len(rows), limit)}
