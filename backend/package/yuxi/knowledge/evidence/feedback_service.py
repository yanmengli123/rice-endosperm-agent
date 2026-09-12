"""证据反馈飞轮服务（P4）：提交 → 脱敏 → 人工裁决 → benchmark candidate。

- ``anonymize_comment``：确定性脱敏（邮箱/手机号/长 token/URL），去除
  对话内容引用，只保留反馈语义；
- ``submit_evidence_feedback``：append-only 写入（同 run+evidence+uid 幂等）；
- ``adjudicate_feedback``：管理员裁决；approve → 自动生成 benchmark candidate
  （evidence_pr_gate.jsonl 同构，用 question_types/identifiers 检测器补全）。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.planning.task_classifier import detect_question_types
from yuxi.knowledge.research_evidence import extract_gene_identifiers
from yuxi.storage.postgres.models_knowledge import (
    EvidenceBenchmarkCandidateRecord,
    EvidenceFeedbackRecord,
)
from yuxi.utils.datetime_utils import utc_now

FEEDBACK_ACTIONS = ("helpful", "misleading", "wrong_location")
ADJUDICATIONS = ("approved", "rejected")
MAX_COMMENT_LENGTH = 500

_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_PATTERN = re.compile(r"(?<!\d)(?:1[3-9]\d{9}|\+?\d[\d -]{7,}\d)(?!\d)")
_TOKEN_PATTERN = re.compile(r"(?:sk|ey|Bearer)[A-Za-z0-9_\-]{12,}")
_URL_PATTERN = re.compile(r"https?://\S+")
_KEY_VALUE_PAIR = re.compile(r"(?:问题|question)[:：]\s*.+")


def anonymize_comment(comment: str | None) -> str | None:
    """确定性脱敏：邮箱/手机号/token/URL 替换为占位符，去除引用的问句原文。"""
    if not comment:
        return None
    cleaned = str(comment).strip()[:MAX_COMMENT_LENGTH]
    cleaned = _KEY_VALUE_PAIR.sub("[QUESTION_REMOVED]", cleaned)
    cleaned = _EMAIL_PATTERN.sub("[EMAIL]", cleaned)
    cleaned = _TOKEN_PATTERN.sub("[TOKEN]", cleaned)
    cleaned = _URL_PATTERN.sub("[URL]", cleaned)
    cleaned = _PHONE_PATTERN.sub("[PHONE]", cleaned)
    return cleaned or None


def _feedback_id(run_id: str, evidence_id: str, uid: str) -> str:
    digest = hashlib.sha256(f"{run_id}|{evidence_id}|{uid}".encode()).hexdigest()[:32]
    return f"efb_{digest}"


async def submit_evidence_feedback(
    db: AsyncSession,
    *,
    run_id: str,
    evidence_id: str,
    uid: str,
    tenant_id: int,
    action: str,
    comment: str | None = None,
) -> dict[str, Any]:
    """提交证据反馈；同 (run, evidence, user) 幂等 upsert（重提回 PENDING）。"""
    if action not in FEEDBACK_ACTIONS:
        raise ValueError(f"不支持的反馈类型: {action}")
    feedback_id = _feedback_id(run_id, evidence_id, uid)
    anonymized = anonymize_comment(comment)
    existing = (
        await db.execute(select(EvidenceFeedbackRecord).where(EvidenceFeedbackRecord.feedback_id == feedback_id))
    ).scalar_one_or_none()
    if existing is not None:
        existing.action = action
        existing.comment = anonymized
        existing.status = "PENDING"
        existing.adjudication = None
        existing.adjudicated_by = None
        existing.adjudicated_at = None
        await db.flush()
        return {"feedback_id": feedback_id, "status": existing.status, "updated": True}

    record = EvidenceFeedbackRecord(
        feedback_id=feedback_id,
        tenant_id=tenant_id,
        run_id=run_id,
        evidence_id=evidence_id,
        uid=uid,
        action=action,
        comment=anonymized,
        anonymized=True,
        status="PENDING",
    )
    db.add(record)
    await db.flush()
    return {"feedback_id": feedback_id, "status": "PENDING", "updated": False}


async def adjudicate_feedback(
    db: AsyncSession,
    *,
    feedback_id: str,
    adjudication: str,
    adjudicated_by: str,
) -> dict[str, Any]:
    """管理员裁决；approved 自动生成 benchmark candidate（飞轮）。"""
    if adjudication not in ADJUDICATIONS:
        raise ValueError(f"不支持的裁决结果: {adjudication}")
    record = (
        await db.execute(select(EvidenceFeedbackRecord).where(EvidenceFeedbackRecord.feedback_id == feedback_id))
    ).scalar_one_or_none()
    if record is None:
        raise LookupError("反馈不存在")
    if record.status == "ADJUDICATED":
        return {"feedback_id": feedback_id, "status": record.status, "adjudication": record.adjudication}

    record.status = "ADJUDICATED"
    record.adjudication = adjudication
    record.adjudicated_by = adjudicated_by
    record.adjudicated_at = utc_now()

    candidate_created = None
    if adjudication == "approved":
        if record.action in {"misleading", "wrong_location"}:
            # 负例候选：该证据曾误导/定位错误，进入基准时要求证据链不包含它。
            candidate = await _create_negative_candidate(db, record)
            candidate_created = candidate.candidate_id if candidate else None
        elif record.action == "helpful":
            candidate = await _create_positive_candidate(db, record)
            candidate_created = candidate.candidate_id if candidate else None

    return {
        "feedback_id": feedback_id,
        "status": record.status,
        "adjudication": adjudication,
        "benchmark_candidate_id": candidate_created,
    }


async def _existing_candidate(db: AsyncSession, feedback_id: str):
    return (
        await db.execute(
            select(EvidenceBenchmarkCandidateRecord).where(EvidenceBenchmarkCandidateRecord.feedback_id == feedback_id)
        )
    ).scalar_one_or_none()


async def _create_negative_candidate(db: AsyncSession, record: EvidenceFeedbackRecord):
    if await _existing_candidate(db, record.feedback_id) is not None:
        return None
    question = f"本次检索证据 {record.evidence_id} 是否应作为答案依据？"
    candidate = EvidenceBenchmarkCandidateRecord(
        candidate_id=f"ebc_{hashlib.sha256(record.feedback_id.encode()).hexdigest()[:32]}",
        tenant_id=record.tenant_id,
        feedback_id=record.feedback_id,
        case_id=f"flywheel-{record.feedback_id[:16]}",
        question=question,
        question_types=["NEGATIVE"],
        required_identifiers=[],
        answerable=False,
        source_evidence_id=record.evidence_id,
        created_by=record.adjudicated_by,
    )
    db.add(candidate)
    await db.flush()
    return candidate


async def _create_positive_candidate(db: AsyncSession, record: EvidenceFeedbackRecord):
    if await _existing_candidate(db, record.feedback_id) is not None:
        return None
    # question 无法从 append-only 反馈恢复（脱敏原则），退化为 evidence-id 锚定
    # 占位；导出时由管理员补全为真实问题后再进基准集。
    question = f"[待补全] 与证据 {record.evidence_id} 相关的原始问题"
    identifiers = extract_gene_identifiers(record.comment or "") or []
    candidate = EvidenceBenchmarkCandidateRecord(
        candidate_id=f"ebc_{hashlib.sha256(record.feedback_id.encode()).hexdigest()[:32]}",
        tenant_id=record.tenant_id,
        feedback_id=record.feedback_id,
        case_id=f"flywheel-{record.feedback_id[:16]}",
        question=question,
        question_types=detect_question_types(question),
        required_identifiers=list(dict.fromkeys(identifiers)),
        answerable=True,
        source_evidence_id=record.evidence_id,
        created_by=record.adjudicated_by,
    )
    db.add(candidate)
    await db.flush()
    return candidate


async def list_feedback_for_admin(db: AsyncSession, *, limit: int = 50) -> list[dict[str, Any]]:
    """管理员裁决列表（PENDING 优先，时间倒序）。"""
    records = (
        (
            await db.execute(
                select(EvidenceFeedbackRecord)
                .order_by(EvidenceFeedbackRecord.status.asc(), EvidenceFeedbackRecord.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "feedback_id": record.feedback_id,
            "run_id": record.run_id,
            "evidence_id": record.evidence_id,
            "action": record.action,
            "comment": record.comment,
            "status": record.status,
            "adjudication": record.adjudication,
            "created_at": record.created_at.isoformat() if record.created_at else None,
        }
        for record in records
    ]
