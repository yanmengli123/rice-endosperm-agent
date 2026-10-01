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
                select(LiteratureReaderRender).where(
                    LiteratureReaderRender.revision_id == revision_id,
                    LiteratureReaderRender.tenant_id == tenant_id,
                )
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
    decided = {d["report_idx"]: d for d in decisions}
    for item in items:
        idx = int(item.get("report_idx", -1))
        item["decision"] = decided.get(idx)
    blocking = sum(1 for item in items if item.get("blocking") and not item.get("decision"))
    return {
        "revision_id": revision_id,
        "total": len(items),
        "blocking_pending": blocking,
        "items": items,
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
) -> dict[str, Any]:
    """四选一仲裁（幂等 upsert：同 (tenant, revision, report_idx) 覆盖）。"""
    if choice not in VALID_CHOICES:
        raise ValueError(f"非法裁决: {choice}（可选 {VALID_CHOICES}）")
    if choice in ("B", "C", "D") and not (resolved_text or "").strip():
        # B/C/D 需要结果文本（B=采用 Paddle、C=合并、D=均不取即删除标记文本）
        raise ValueError(f"裁决 {choice} 需提供 resolved_text")
    async with pg_manager.get_async_session_context() as session:
        stmt = (
            pg_insert(LiteratureReviewDecision)
            .values(
                tenant_id=tenant_id,
                revision_id=revision_id,
                report_idx=report_idx,
                para_id=para_id,
                choice=choice,
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
    return {"revision_id": revision_id, "report_idx": report_idx, "choice": choice}


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


def _apply_decision_to_paragraphs(paragraphs: list[dict], decision: dict) -> str:
    """把单条裁决应用到段落列表，返回动作（kept/replaced/removed/no_target）。"""
    para_id = str(decision.get("para_id") or "")
    choice = str(decision.get("choice") or "")
    for para in paragraphs:
        if not para_id or str(para.get("para_id") or "") != para_id:
            continue
        if choice == "A":
            return "kept"
        if choice in ("B", "C"):
            para["text"] = str(decision.get("resolved_text") or "")
            return "replaced"
        if choice == "D":
            para["text"] = ""
            return "removed"
        return "no_action"
    return "no_target"


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
                select(LiteratureReaderRender).where(
                    LiteratureReaderRender.revision_id == revision_id,
                    LiteratureReaderRender.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
    if render is None or not render.markdown_uri:
        raise ValueError(f"解析版本无渲染记录: {revision_id}")
    base = render.markdown_uri.removeprefix("kbasset://").removesuffix("render.md")
    minio = get_minio_client()
    document = json.loads((await minio.adownload_file(RENDER_BUCKET, f"{base}document.json")).decode("utf-8"))
    paragraphs = [p for p in (document.get("paragraphs") or []) if isinstance(p, dict)]
    actions = {"kept": 0, "replaced": 0, "removed": 0, "no_target": 0}
    for decision in decisions:
        action = _apply_decision_to_paragraphs(paragraphs, decision)
        actions[action] = actions.get(action, 0) + 1
    document["paragraphs"] = [p for p in paragraphs if str(p.get("text") or "").strip()]
    resolved_doc = json.dumps(document, ensure_ascii=False)
    title = str((document.get("metadata") or {}).get("title") or "")
    body = "\n\n".join(str(p.get("text") or "") for p in document["paragraphs"] if str(p.get("text") or "").strip())
    resolved_md = (f"# {title}\n\n{body}\n") if title else f"{body}\n"
    digest = hashlib.sha256(resolved_doc.encode("utf-8")).hexdigest()
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
    return manifest


def _utc_now_iso() -> str:
    from yuxi.utils.datetime_utils import utc_now

    return utc_now().isoformat()


async def get_resolved_manifest(tenant_id: int, revision_id: str) -> dict | None:
    """修正回放清单（未回放返回 None）。"""
    async with pg_manager.get_async_session_context() as session:
        render = (
            await session.execute(
                select(LiteratureReaderRender).where(
                    LiteratureReaderRender.revision_id == revision_id,
                    LiteratureReaderRender.tenant_id == tenant_id,
                )
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


async def ai_suggest(tenant_id: int, revision_id: str, report_idx: int) -> dict:
    """AI 综合建议（上游 dual_ai_review 语义，LLM 换 Yuxi 模型链）。

    人机回环纪律：AI **只建议不裁决**——返回的 suggestion 供前端预填
    resolved_text，最终仍需人工确认四选一。select_model 延迟导入（防
    toolkits→literature→models 循环，同 notes.compiler 教训）。
    """
    items = (await get_review_items(tenant_id, revision_id)).get("items") or []
    item = next((it for it in items if int(it.get("report_idx", -1)) == report_idx), None)
    if item is None:
        raise ValueError(f"复核项不存在: report_idx={report_idx}")
    a_text = str(item.get("a_text") or item.get("mineru_text") or "")
    b_text = str(item.get("b_text") or item.get("paddle_text") or "")
    if not a_text or not b_text:
        raise ValueError("该复核项无双通道文本，无法 AI 建议")
    prompt = (
        "你是学术 PDF 解析的字符仲裁助手。同一片段有两个识别版本，请综合判断"
        "给出最终修正文本（选更可信者或合理合并；保留上下标/特殊符号的原义；"
        "只输出修正后的文本本身，不要解释）。\n\n"
        f"【版本 A（MinerU）】\n{a_text}\n\n【版本 B（PaddleOCR）】\n{b_text}"
    )
    from yuxi import config
    from yuxi.models.chat import select_model  # noqa: PLC0415 - 延迟导入防循环

    model = select_model(model_spec=config.default_model)
    response = await model.call([{"role": "user", "content": prompt}], stream=False)
    suggestion = str(response.content or "").strip()
    if not suggestion:
        raise ValueError("AI 建议为空")
    return {"report_idx": report_idx, "suggestion": suggestion, "model": config.default_model}
