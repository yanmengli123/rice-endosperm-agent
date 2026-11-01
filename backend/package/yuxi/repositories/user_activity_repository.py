"""Aggregate the authenticated user's messages by Shanghai calendar day."""

from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from yuxi.storage.postgres.models_business import Conversation, Message
from yuxi.utils.datetime_utils import utc_now


async def message_activity(db, *, uid: str, tenant_id: int, days: int = 365, now=None):
    moment = now or utc_now()
    today = moment.astimezone(ZoneInfo('Asia/Shanghai')).date()
    first = today - timedelta(days=days - 1)
    start = moment.astimezone(ZoneInfo('Asia/Shanghai')).replace(hour=0, minute=0, second=0, microsecond=0)
    start = (start - timedelta(days=days - 1)).astimezone(ZoneInfo('UTC')).replace(tzinfo=None)
    end = start + timedelta(days=days)
    if db.get_bind().dialect.name == 'postgresql':
        bucket = func.date(func.timezone('Asia/Shanghai', func.timezone('UTC', Message.created_at)))
    else:
        bucket = func.date(func.datetime(Message.created_at, '+8 hours'))
    rows = await db.execute(
        select(bucket.label('day'), func.count(Message.id))
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Conversation.uid == uid,
            Conversation.tenant_id == tenant_id,
            Conversation.status != 'deleted',
            Message.role.in_(('user', 'assistant', 'tool')),
            Message.created_at >= start,
            Message.created_at < end,
        )
        .group_by(bucket)
    )
    counts = {str(day): count for day, count in rows}
    daily = []
    for index in range(days):
        day = str(first + timedelta(days=index))
        daily.append({'date': day, 'message_count': counts.get(day, 0)})
    return {
        'window_days': days,
        'timezone': 'Asia/Shanghai',
        'daily_message_counts': daily,
    }
