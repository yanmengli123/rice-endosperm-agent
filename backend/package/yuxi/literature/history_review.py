"""Map anchored historical OCR reviews, never auto-replay or trust AI choices."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict

from sqlalchemy import select

from yuxi.literature.history_import import find_paper
from yuxi.literature.reading.service import RENDER_BUCKET
from yuxi.literature.review import _letter_for, _render_identity
from yuxi.literature.review_contract import normalize_choice, normalize_review_items
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.models_knowledge import (
    LiteratureArtifactRevision,
    LiteratureReaderRender,
    LiteratureReviewDecision,
)


def _norm(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


async def promote_review(db, item, *, tenant_id, uid, papers, uri, digest):
    paper = find_paper(item.key, papers)
    if not paper or not (paper.source_revision_id or "").startswith("pair-"):
        return "PRESERVED", None, "历史复核仅绑定同批导入来源，不套用新的原生来源"
    render = (
        await db.execute(
            select(LiteratureReaderRender)
            .where(
                LiteratureReaderRender.tenant_id == tenant_id,
                LiteratureReaderRender.revision_id == paper.source_revision_id,
                LiteratureReaderRender.render_version == "paperagent_v4",
                LiteratureReaderRender.status == "SUCCEEDED",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if render is None:
        return "PRESERVED", None, "缺少同源阅读产物"
    if render.review_uri:
        return (
            ("UNCHANGED" if render.review_digest == digest else "CONFLICT"),
            f"review:{render.id}",
            "已有复核清单不覆盖",
        )
    pointer = (paper.artifact_pointers or {}).get("document")
    document_revision = (
        await db.execute(
            select(LiteratureArtifactRevision).where(
                LiteratureArtifactRevision.revision_id == pointer,
                LiteratureArtifactRevision.tenant_id == tenant_id,
                LiteratureArtifactRevision.paper_id == paper.id,
                LiteratureArtifactRevision.source == "imported",
                LiteratureArtifactRevision.source_revision_id == paper.source_revision_id,
            )
        )
    ).scalar_one_or_none()
    if document_revision is None:
        return "PRESERVED", None, "当前正文已人工修改或无法证明同源，需显式重新绑定复核"
    content = await get_minio_client().adownload_file(
        RENDER_BUCKET, document_revision.object_uri.removeprefix("kbasset://")
    )
    if hashlib.sha256(content).hexdigest() != document_revision.content_digest:
        raise ValueError("历史复核正文完整性校验失败")
    paragraphs = json.loads(content).get("paragraphs", [])
    by_block, by_para = defaultdict(set), defaultdict(set)
    for index, paragraph in enumerate(paragraphs):
        by_para[str(paragraph.get("para_id") or "")].add(index)
        for block in paragraph.get("source_block_ids") or []:
            by_block[str(block)].add(index)
    review = json.loads(item.content)
    if not isinstance(review, dict) or not isinstance(review.get("items"), list):
        raise ValueError("历史复核缺少 items 数组")
    identity, decisions, seen = _render_identity(render), [], set()
    normalized = normalize_review_items(review["items"], render_id=identity, render_digest=digest)
    for index, (raw, dto) in enumerate(zip(review["items"], normalized, strict=True)):
        if raw.get("item_id") or int(raw.get("report_idx", raw.get("idx", index))) != index:
            return "PRESERVED", None, "旧清单携带不一致版本身份，需显式重绑定"
        positions = set().union(
            *(by_block.get(block, set()) for block in dto.source_block_ids),
            *(by_para.get(para, set()) for para in dto.para_ids),
        )
        targets = [paragraphs[position] for position in positions]
        candidates = [_norm(dto.mineru_text), _norm(dto.paddleocr_text)]
        if len(targets) != 1 or not any(
            t and t in _norm(targets[0].get("text_en") or targets[0].get("text")) for t in candidates
        ):
            return "PRESERVED", None, "历史复核定位或正文不唯一，未按序号猜测裁决"
        if dto.item_id in seen:
            return "PRESERVED", None, "历史复核块身份重复，需人工拆分绑定"
        seen.add(dto.item_id)
        # Never promote AI verdict/resolved flags as a human decision.
        choice = raw.get("user_choice")
        if choice:
            existing = (
                await db.execute(
                    select(LiteratureReviewDecision.id).where(
                        LiteratureReviewDecision.tenant_id == tenant_id,
                        LiteratureReviewDecision.revision_id == paper.source_revision_id,
                        LiteratureReviewDecision.report_idx == index,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return "CONFLICT", None, "当前已有裁决，历史裁决不覆盖"
            decisions.append(
                LiteratureReviewDecision(
                    tenant_id=tenant_id,
                    revision_id=paper.source_revision_id,
                    report_idx=index,
                    para_id=str(targets[0].get("para_id") or ""),
                    choice=_letter_for(normalize_choice(choice)),
                    decided_by=uid,
                    render_id=identity,
                    render_digest=digest,
                    item_id=dto.item_id,
                    contract_version="review_choice_v2",
                    legacy_ack=False,
                )
            )
    # Original immutable receipt bytes are the served artifact; DB digest binds it.
    render.review_uri, render.review_digest = uri, digest
    db.add_all(decisions)
    await db.flush()
    return "APPLIED", f"review:{render.id}", "同源复核及明确人工选择已恢复；未重放正文、未继承 AI 裁决"
