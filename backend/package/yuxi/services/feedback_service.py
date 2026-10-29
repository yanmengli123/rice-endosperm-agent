import asyncio

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.services.langfuse_service import submit_user_feedback_score
from yuxi.storage.postgres.models_business import Conversation, Message, MessageFeedback
from yuxi.utils.logging_config import logger


async def submit_message_feedback_view(
    *,
    message_id: int,
    rating: str,
    reason: str | None,
    db: AsyncSession,
    current_uid: str,
) -> dict:
    if rating not in ["like", "dislike"]:
        raise HTTPException(status_code=422, detail="Rating must be 'like' or 'dislike'")

    try:
        message_result = await db.execute(select(Message).filter_by(id=message_id))
        message = message_result.scalar_one_or_none()
        if not message:
            raise HTTPException(status_code=404, detail="Message not found")

        conversation_result = await db.execute(select(Conversation).filter_by(id=message.conversation_id))
        conversation = conversation_result.scalar_one_or_none()
        if not conversation or conversation.uid != str(current_uid):
            raise HTTPException(status_code=403, detail="Access denied")

        # 不做前置存在性查询（消除查后插竞态窗口）：并发/重复提交由
        # uq_message_feedbacks_message_uid 唯一索引兜底，IntegrityError 按幂等成功处理。
        new_feedback = MessageFeedback(
            message_id=message_id,
            uid=str(current_uid),
            rating=rating,
            reason=reason,
        )

        db.add(new_feedback)
        try:
            await db.commit()
        except IntegrityError:
            # 唯一索引冲突 = 该用户已对这条消息提交过反馈：回滚后返回既有记录，
            # 响应与首次提交一致；langfuse 评分首提时已推送，不重复推送。
            await db.rollback()
            duplicate_result = await db.execute(
                select(MessageFeedback).filter_by(message_id=message_id, uid=str(current_uid))
            )
            existing_feedback = duplicate_result.scalar_one_or_none()
            if existing_feedback is None:
                raise
            logger.info(
                f"User {current_uid} re-submitted feedback for message {message_id}; "
                "returning existing feedback as idempotent success"
            )
            return {
                "id": existing_feedback.id,
                "message_id": existing_feedback.message_id,
                "rating": existing_feedback.rating,
                "reason": existing_feedback.reason,
                "created_at": existing_feedback.created_at.isoformat(),
            }
        await db.refresh(new_feedback)

        trace_id = (message.extra_metadata or {}).get("langfuse_trace_id")
        if trace_id:
            # submit_user_feedback_score 内部会同步调用 client.flush() 发起阻塞网络请求，
            # 放到线程池执行避免阻塞事件循环；本地反馈已落库，上传失败不影响主流程。
            await asyncio.to_thread(
                submit_user_feedback_score,
                trace_id=trace_id,
                feedback_id=new_feedback.id,
                message_id=new_feedback.message_id,
                conversation_id=message.conversation_id,
                uid=str(current_uid),
                rating=rating,
                reason=reason,
            )

        logger.info(f"User {current_uid} submitted {rating} feedback for message {message_id}")

        return {
            "id": new_feedback.id,
            "message_id": new_feedback.message_id,
            "rating": new_feedback.rating,
            "reason": new_feedback.reason,
            "created_at": new_feedback.created_at.isoformat(),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error submitting message feedback: {e}")
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to submit feedback: {str(e)}")


async def get_message_feedback_view(
    *,
    message_id: int,
    db: AsyncSession,
    current_uid: str,
) -> dict:
    try:
        feedback_result = await db.execute(
            select(MessageFeedback).filter_by(message_id=message_id, uid=str(current_uid))
        )
        feedback = feedback_result.scalar_one_or_none()

        if not feedback:
            return {"has_feedback": False, "feedback": None}

        return {
            "has_feedback": True,
            "feedback": {
                "id": feedback.id,
                "rating": feedback.rating,
                "reason": feedback.reason,
                "created_at": feedback.created_at.isoformat(),
            },
        }

    except Exception as e:
        logger.exception(f"Error getting message feedback: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get feedback: {str(e)}")
