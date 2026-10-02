"""识别复核与人工仲裁（G4，ADR-0010 决议 12）。

- 双通道渲染是**可选增强**：用户显式触发，子进程跑 vendor P14 完整链
  （paddle=True, ai_review=False——本地词典规则 + PDF 文本层第三信号，
  0 LLM 成本产出 review.json）；AI 综合仲裁（上游 dual_ai_review）后续以
  Yuxi 模型链替代接入。
- PaddleOCR 凭据经环境变量读取（YUXI_PADDLE_TOKEN），不落库。
- 四选一裁决（A=保留 MinerU / B=采用 Paddle / C=两者合并 / D=均不取）
  持久化 `literature_review_decisions`；修正文本的渲染回放属已知限制。
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from yuxi.literature.reading.service import RENDER_BUCKET, enqueue_reader_render
from yuxi.literature.review_contract import (
    CHOICE_EFFECT,
    ReviewItem,
    apply_choice_to_paragraph,
    normalize_choice,
)
from yuxi.storage.minio import get_minio_client
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeParseRevision,
    LiteratureReaderRender,
    LiteratureReviewDecision,
)

VALID_CHOICES = ("A", "B", "C", "D")


async def enqueue_dual_channel_render(revision_id: str) -> dict[str, Any]:
    """触发双通道渲染（可选增强；Paddle 凭据未配置时 fail-fast 明确报因）。

    P7.1 起为**真双通道**：gateway.run_dual_channel_render（paddle=True +
    third_decide=True，0 LLM 成本产出 review.json），模式经 render_version
    （reader_render_v1-dual）编码，与离线渲染行并存。
    """
    token = os.getenv("YUXI_PADDLE_TOKEN", "")
    if not token:
        raise ValueError("未配置 PaddleOCR 凭据（YUXI_PADDLE_TOKEN），双通道复核不可用")
    return await enqueue_reader_render(revision_id=revision_id, force=True, dual=True)


async def get_review_items(tenant_id: int, revision_id: str) -> dict[str, Any]:
    """读取 review.json 复核清单（MinIO work/ 前缀）+ 已裁决映射。"""
    async with pg_manager.get_async_session_context() as session:
        render = (
            await session.execute(
                select(LiteratureReaderRender)
                .where(
                    LiteratureReaderRender.revision_id == revision_id,
                    LiteratureReaderRender.tenant_id == tenant_id,
                )
                .order_by(LiteratureReaderRender.updated_at.desc(), LiteratureReaderRender.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if render is None:
            raise ValueError(f"解析版本无渲染记录: {revision_id}")
        revision_exists = (
            await session.execute(
                select(KnowledgeParseRevision.id).where(KnowledgeParseRevision.revision_id == revision_id)
            )
        ).scalar_one_or_none()
    if revision_exists is None:
        raise ValueError(f"解析版本不存在: {revision_id}")
    base = render.markdown_uri.removeprefix("kbasset://").removesuffix("render.md")
    review_object = f"{base}work/review.json"
    items: list[dict[str, Any]] = []
    try:
        data = await get_minio_client().adownload_file(RENDER_BUCKET, review_object)
        review = json.loads(data.decode("utf-8"))
        items = list(review.get("items") or [])
    except Exception:  # noqa: BLE001 - 无双通道产物（纯离线渲染）→ 空清单
        items = []
    decisions = await list_decisions(tenant_id, revision_id)
    render_id = render.render_version  # 与 render 行绑定的稳定身份
    normalized_items = []
    by_item_id: dict[str, dict] = {}
    for payload in items:
        item = ReviewItem.from_payload(payload, render_id=render_id)
        entry = item.to_payload()
        decision = next((d for d in decisions if d.get("report_idx") == item.report_idx), None)
        entry["decision"] = decision
        entry["choice_effect"] = CHOICE_EFFECT.get(normalize_choice(decision["choice"]), "") if decision else ""
        normalized_items.append(entry)
        by_item_id[entry["item_id"]] = entry
    blocking = sum(1 for e in normalized_items if e["blocking"] and not e["decision"])
    return {
        "revision_id": revision_id,
        "schema_version": "review_item_v2",
        "total": len(normalized_items),
        "blocking_pending": blocking,
        "items": normalized_items,
        "decisions": decisions,
    }


async def decide(
    tenant_id: int,
    revision_id: str,
    *,
    report_idx: int,
    choice: str,
    resolved_text: str | None = None,
    para_id: str = "",
    decided_by: str = "",
    legacy_ack: bool = False,
) -> dict[str, Any]:
    """四选一仲裁（幂等 upsert；上游兼容语义：A=mineru/B=paddleocr/C=both/D=neither）。

    兼容语义（review_contract）：B 的替换文本默认取复核项的 PaddleOCR 文本，
    resolved_text 仅作为 AI 建议/自定义修正的显式覆盖；C/D 不改写正文，
    携带旧语义 resolved_text（历史"合并/删除"）需 legacy_ack 显式确认。
    """
    normalized = normalize_choice(choice)
    if normalized in ("both", "neither") and (resolved_text or "").strip() and not legacy_ack:
        raise ValueError(
            "检测到旧语义裁决（C/D 携带 resolved_text：旧版 C=合并替换、D=删除）。"
            "当前兼容语义 C=两者皆可、D=标记存疑不改写正文；确要保留该文本请传 legacy_ack=true"
        )
    async with pg_manager.get_async_session_context() as session:
        stmt = (
            pg_insert(LiteratureReviewDecision)
            .values(
                tenant_id=tenant_id,
                revision_id=revision_id,
                report_idx=report_idx,
                para_id=para_id,
                choice=_letter_for(normalized),
                resolved_text=resolved_text,
                decided_by=decided_by,
            )
            .on_conflict_do_update(
                index_elements=("tenant_id", "revision_id", "report_idx"),
                set_={
                    "choice": choice,
                    "resolved_text": resolved_text,
                    "decided_by": decided_by,
                },
            )
        )
        await session.execute(stmt)
        await session.commit()
    return {"revision_id": revision_id, "report_idx": report_idx, "choice": _letter_for(normalized)}


def _letter_for(normalized: str) -> str:
    return {"mineru": "A", "paddleocr": "B", "both": "C", "neither": "D"}[normalized]


async def list_decisions(tenant_id: int, revision_id: str) -> list[dict[str, Any]]:
    async with pg_manager.get_async_session_context() as session:
        rows = (
            (
                await session.execute(
                    select(LiteratureReviewDecision).where(
                        LiteratureReviewDecision.tenant_id == tenant_id,
                        LiteratureReviewDecision.revision_id == revision_id,
                    )
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "report_idx": row.report_idx,
            "para_id": row.para_id,
            "choice": row.choice,
            "resolved_text": row.resolved_text,
            "decided_by": row.decided_by,
        }
        for row in rows
    ]


# ---------------------------------------------------------------- 修正回放（决议 12 后续迭代）

RESOLVED_DOC_NAME = "document_resolved.json"
RESOLVED_MD_NAME = "render_resolved.md"
RESOLVED_MANIFEST_NAME = "resolved.json"


def document_object_candidates(markdown_uri: str) -> list[str]:
    """编译/翻译取段落的对象候选（修正版优先，canonical 兜底）。

    人工裁决后的阅读视图是更可信的派生物——下游编译应优先消费
    document_resolved.json；无裁决时回落原始 document.json。
    """
    base = markdown_uri.removeprefix("kbasset://").removesuffix("render.md")
    return [f"{base}{RESOLVED_DOC_NAME}", f"{base}document.json"]


def para_text(para: dict) -> str:
    """段落文本：上游 document.json 的权威键是 ``text_en``（text_zh 为翻译回填）；
    修正回放写入同为 ``text_en``。``text`` 仅作兼容兜底。"""
    return str(para.get("text_en") or para.get("text") or "")


def _apply_decision_to_paragraphs(paragraphs: list[dict], decision: dict, item: ReviewItem | None = None) -> str:
    """把单条裁决应用到段落列表（上游兼容语义，委托 review_contract）。

    decision 为历史行（字母 A-D）；item 为对应复核项（提供文本兜底定位）。
    旧语义 C/D 行（带 resolved_text 且未 legacy_ack）在本层不再解释为
    替换/删除——按兼容语义执行（C 保持 / D 标记），历史文本仅留档。
    """
    choice = str(decision.get("choice") or "")
    if item is None:
        # 无复核项（裁决无对应清单，如导入迁移）：构造仅含定位信息的降级项
        item = ReviewItem(
            para_ids=(str(decision.get("para_id") or ""),) if decision.get("para_id") else (),
            mineru_text=str(decision.get("resolved_text") or ""),
            paddleocr_text="",
        )
    return apply_choice_to_paragraph(paragraphs, item, choice, resolved_text=str(decision.get("resolved_text") or ""))


async def apply_decisions(tenant_id: int, revision_id: str) -> dict:
    """修正回放：全部裁决 → document_resolved.json + render_resolved.md + 清单。

    决议 12 落地：裁决修正的是**阅读视图派生物**（新产物并立，原始 render 保留
    审计轨迹，不覆盖）；下游编译/翻译经 ``document_object_candidates`` 优先
    消费修正版。无裁决时 fail-closed（无可回放）。
    """
    decisions = await list_decisions(tenant_id, revision_id)
    if not decisions:
        raise ValueError("无已持久化的裁决，无可回放")
    async with pg_manager.get_async_session_context() as session:
        render = (
            await session.execute(
                select(LiteratureReaderRender)
                .where(
                    LiteratureReaderRender.revision_id == revision_id,
                    LiteratureReaderRender.tenant_id == tenant_id,
                )
                .order_by(LiteratureReaderRender.updated_at.desc(), LiteratureReaderRender.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if render is None or not render.markdown_uri:
        raise ValueError(f"解析版本无渲染记录: {revision_id}")
    base = render.markdown_uri.removeprefix("kbasset://").removesuffix("render.md")
    minio = get_minio_client()
    document = json.loads((await minio.adownload_file(RENDER_BUCKET, f"{base}document.json")).decode("utf-8"))
    paragraphs = [p for p in (document.get("paragraphs") or []) if isinstance(p, dict)]
    # 装载复核项（文本兜底定位；渲染缺 review.json 时逐条降级）
    review_items: dict[int, ReviewItem] = {}
    try:
        review_raw = json.loads((await minio.adownload_file(RENDER_BUCKET, f"{base}work/review.json")).decode("utf-8"))
        for payload in review_raw.get("items") or []:
            item = ReviewItem.from_payload(payload, render_id=render.render_version)
            review_items[item.report_idx] = item
    except Exception:  # noqa: BLE001 - 无复核清单（纯离线渲染）
        review_items = {}
    actions = {"kept": 0, "replaced": 0, "restored": 0, "marked": 0, "no_target": 0, "no_replacement_text": 0}
    for decision in decisions:
        item = review_items.get(int(decision.get("report_idx", -1)))
        action = _apply_decision_to_paragraphs(paragraphs, decision, item)
        actions[action] = actions.get(action, 0) + 1
    # 兼容语义下 D=标记不改写：不再按裁决删段（旧 removed 语义废弃）
    document["paragraphs"] = [p for p in paragraphs if para_text(p).strip()]
    resolved_doc = json.dumps(document, ensure_ascii=False)
    title = str((document.get("metadata") or {}).get("title") or "")
    body = "\n\n".join(para_text(p) for p in document["paragraphs"] if para_text(p).strip())
    resolved_md = (f"# {title}\n\n{body}\n") if title else f"{body}\n"
    digest = hashlib.sha256(resolved_doc.encode("utf-8")).hexdigest()
    previous = await get_resolved_manifest(tenant_id, revision_id)
    if previous and previous.get("digest") == digest:
        return {**previous, "reused": True}
    await minio.aupload_file(
        bucket_name=RENDER_BUCKET,
        object_name=f"{base}{RESOLVED_DOC_NAME}",
        data=resolved_doc.encode("utf-8"),
        content_type="application/json",
    )
    await minio.aupload_file(
        bucket_name=RENDER_BUCKET,
        object_name=f"{base}{RESOLVED_MD_NAME}",
        data=resolved_md.encode("utf-8"),
        content_type="text/markdown",
    )
    manifest = {
        "revision_id": revision_id,
        "applied_decisions": len(decisions),
        "actions": actions,
        "digest": digest,
        "resolved_at": _utc_now_iso(),
    }
    await minio.aupload_file(
        bucket_name=RENDER_BUCKET,
        object_name=f"{base}{RESOLVED_MANIFEST_NAME}",
        data=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
    )
    from sqlalchemy import update
    from yuxi.storage.postgres.models_knowledge import LiteraturePaper

    async with pg_manager.get_async_session_context() as session:
        from yuxi.literature.provenance import invalidate_note_publications

        await invalidate_note_publications(session, revision_id)
        await session.execute(
            update(LiteraturePaper)
            .where(LiteraturePaper.tenant_id == tenant_id, LiteraturePaper.source_revision_id == revision_id)
            .values(
                reading_version=LiteraturePaper.reading_version + 1,
                ai_value_score=None,
                topic_score=None,
                value_score=0.0,
            )
        )
        await session.commit()
    return manifest


def _utc_now_iso() -> str:
    from yuxi.utils.datetime_utils import utc_now

    return utc_now().isoformat()


async def get_resolved_manifest(tenant_id: int, revision_id: str) -> dict | None:
    """修正回放清单（未回放返回 None）。"""
    async with pg_manager.get_async_session_context() as session:
        render = (
            await session.execute(
                select(LiteratureReaderRender)
                .where(
                    LiteratureReaderRender.revision_id == revision_id,
                    LiteratureReaderRender.tenant_id == tenant_id,
                )
                .order_by(LiteratureReaderRender.updated_at.desc(), LiteratureReaderRender.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if render is None or not render.markdown_uri:
        return None
    base = render.markdown_uri.removeprefix("kbasset://").removesuffix("render.md")
    try:
        data = await get_minio_client().adownload_file(RENDER_BUCKET, f"{base}{RESOLVED_MANIFEST_NAME}")
        return json.loads(data.decode("utf-8"))
    except Exception:  # noqa: BLE001 - 未回放
        return None


# ---------------------------------------------------------------- AI 仲裁建议（人机回环）


async def ai_suggest(
    tenant_id: int,
    revision_id: str,
    report_idx: int,
    *,
    requested_by: str = "",
) -> dict:
    """AI 综合建议（上游 dual_ai_review 语义，LLM 换 Yuxi 模型链）。

    人机回环纪律：AI **只建议不裁决**——返回的 suggestion 供前端预填
    resolved_text，最终仍需人工确认四选一。select_model 延迟导入（防
    toolkits→literature→models 循环，同 notes.compiler 教训）。
    """
    items = (await get_review_items(tenant_id, revision_id)).get("items") or []
    item = next((it for it in items if int(it.get("report_idx", -1)) == report_idx), None)
    if item is None:
        raise ValueError(f"复核项不存在: report_idx={report_idx}")
    # 归一 DTO（review_item_v2）字段：mineru_text / paddleocr_text（旧扁平键兜底）
    a_text = str(item.get("mineru_text") or item.get("a_text") or "")
    b_text = str(item.get("paddleocr_text") or item.get("b_text") or item.get("paddle_text") or "")
    if not a_text or not b_text:
        raise ValueError("该复核项无双通道文本，无法 AI 建议")
    prompt = (
        "你是学术 PDF 解析的字符仲裁助手。同一片段有两个识别版本，请综合判断"
        "给出最终修正文本（选更可信者或合理合并；保留上下标/特殊符号的原义；"
        "只输出修正后的文本本身，不要解释）。\n\n"
        f"【版本 A（MinerU）】\n{a_text}\n\n【版本 B（PaddleOCR）】\n{b_text}"
    )
    from yuxi.literature.llm_gateway import complete_once

    response = await complete_once(
        tenant_id=tenant_id,
        uid=requested_by,
        operation="review_suggest",
        resource_ref=f"{revision_id}:{report_idx}",
        messages=[{"role": "user", "content": prompt}],
    )
    suggestion = str(response.content or "").strip()
    if not suggestion:
        raise ValueError("AI 建议为空")
    return {"report_idx": report_idx, "suggestion": suggestion}
