"""Figure Asset Projection（图卡 P0-1 暗发布）：冻结 Binding → 可发布资产投影。

契约红线（评审基线，违反即设计事故）：

- 投影是**派生视图**，不是权威状态：只读 ``locator_resolution["binding"]``
  的键 + join ``figure_entities``/``figure_assets``，不写任何表，不修改
  :class:`VerifiedLocatorBinding`（ADR-0004）。
- 页码唯一来源是 Binding（``asset_pdf_page_number``，缺省回落
  ``page_number``），永不读 ``figure_assets.page`` 上屏。
- ``asset_name`` = 对象完整 basename（含 ``{digest[:24]}-`` 段），接受域
  镜像 ``services/knowledge_asset_service`` 的校验（等价性由契约测试锁定，
  I4）；``media_type`` 由扩展名映射派生，**禁读** ``figure_assets.mime``
  （该列存 PIL format 标签且可为空）。
- 抑制只影响图卡：任何门禁不过返回抑制原因，文本回答/芯片/PDF 跳转照常；
  异常一律吞掉归 ``projection_error``，绝不向上抛（I6）。
- 发布授权 ``figure_image_publish_allowed`` 与 ``visual_explanation_allowed``
  分立：后者管模型能否谈视觉，前者管后端能否发原图；发布开关
  （figure_card_enabled）在 chat 层读取，与本投影完全解耦。
- 本模块不 import vision/observation（I7：图卡标题只能来自实体表的
  caption/container_label，禁用 VLM 文本）。
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.knowledge.contracts.locator_binding import (
    LOCATOR_KIND_FIGURE_CAPTION,
    LOCATOR_KIND_FIGURE_IMAGE,
    VerifiedLocatorBinding,
)
from yuxi.storage.postgres.models_knowledge import (
    FigureAssetRecord,
    FigureEntityRecord,
    KnowledgeFile,
)
from yuxi.utils import logger

FIGURE_PROJECTION_VERSION = "figure_projection_v1"

# 抑制原因闭合枚举（P0-4 SLA 字典：trace attribute 与落库 JSON 共用词表）
SUPPRESS_BINDING_NOT_VERIFIED = "binding_not_verified"
SUPPRESS_KIND_WITHOUT_FIGURE = "kind_without_figure"
SUPPRESS_NO_ASSET_ROW = "no_asset_row"
SUPPRESS_SCOPE_MISMATCH = "scope_mismatch"
SUPPRESS_REVISION_NOT_ACTIVE = "revision_not_active"
SUPPRESS_ASSET_NAME_UNRESOLVABLE = "asset_name_unresolvable"
SUPPRESS_ASSET_UNFINGERPRINTED = "asset_unfingerprinted"
SUPPRESS_PUBLISH_NOT_ALLOWED = "publish_not_allowed"
SUPPRESS_PROJECTION_ERROR = "projection_error"

_FIGURE_KINDS = {LOCATOR_KIND_FIGURE_IMAGE, LOCATOR_KIND_FIGURE_CAPTION}

# 多资产行的确定性选择规则（观测字段随投影输出，Phase 2 轮播预留）
_SELECTION_RULE = "anchor_desc_sha_desc_id_asc"

# 以下三组常量镜像 services/knowledge_asset_service（该模块拖 FastAPI/MinIO
# 依赖，contracts 层不做生产期 import）；与签发方的等价性由
# test_figure_asset_projection.py 的镜像契约测试锁定（I4）。
_ASSET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_ASSET_NAME_MAX_LENGTH = 255
_ALLOWED_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
_MEDIA_TYPE_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class PublishableFigure(BaseModel):
    """一条可发布的论文原图投影（Phase 1 长度 ∈ {0,1}）。

    永不携带 MinIO object_name、预签名 URL、tenant 信息或 datetime；
    前端凭 (kb_id, revision_id, asset_name) 走鉴权资产端点取图。
    """

    model_config = ConfigDict(extra="forbid")

    projection_version: str = FIGURE_PROJECTION_VERSION
    binding_id: str
    kb_id: str
    file_id: str
    revision_id: str
    asset_name: str
    asset_sha256: str
    media_type: str
    page: int
    panel_match: str | None = None
    evidence_id: str
    figure_label: str = ""
    caption: str = ""
    width: int = 0
    height: int = 0
    selection: dict[str, Any] = Field(default_factory=dict)


def derive_asset_name(object_name: str) -> str | None:
    """对象完整存储键 → kbasset asset_name（= basename，含 digest 段）。

    与 ``knowledge_asset_service.resolve_asset`` 的 ``{prefix}/{asset_name}``
    重建规则严格互补：对象名即 ``{prefix}/{digest24}-{原名}``，因此 asset_name
    必须是完整 basename，不做任何剥前缀手术（ingestor 剥前缀得到的 safe_name
    只是入库配对键，不是发布 URI 的一部分）。
    """
    name = posixpath.basename(str(object_name or "").strip())
    if not name or len(name) > _ASSET_NAME_MAX_LENGTH:
        return None
    if not _ASSET_NAME_PATTERN.match(name):
        return None
    if "/" in name or "\\" in name:
        return None
    suffix = posixpath.splitext(name)[1].lower()
    if suffix not in _ALLOWED_IMAGE_SUFFIXES:
        return None
    return name


def media_type_for_asset_name(asset_name: str) -> str | None:
    suffix = posixpath.splitext(str(asset_name or ""))[1].lower()
    return _MEDIA_TYPE_BY_SUFFIX.get(suffix)


def _anchor_condition(locator_kind: str, anchor_id: str) -> Any:
    """两种入口的锚点语义不同：图片入口 = 视觉锚点（figure_assets.anchor_id）；
    题注入口 = 题注锚点（figure_entities.caption_anchor_id，经实体中转）。"""
    if locator_kind == LOCATOR_KIND_FIGURE_IMAGE:
        return FigureAssetRecord.anchor_id == anchor_id
    return FigureEntityRecord.caption_anchor_id == anchor_id


async def _fetch_figure_rows(
    db: AsyncSession,
    *,
    locator_kind: str,
    revision_id: str,
    anchor_id: str,
    kb_id: str = "",
    file_id: str = "",
    require_active: bool = True,
) -> list[tuple[FigureAssetRecord, FigureEntityRecord]]:
    conditions: list[Any] = [
        FigureAssetRecord.parse_revision_id == revision_id,
        _anchor_condition(locator_kind, anchor_id),
    ]
    stmt = select(FigureAssetRecord, FigureEntityRecord).join(
        FigureEntityRecord, FigureEntityRecord.id == FigureAssetRecord.entity_id
    )
    if kb_id:
        conditions += [FigureAssetRecord.kb_id == kb_id, FigureEntityRecord.kb_id == kb_id]
    if file_id:
        conditions.append(FigureEntityRecord.file_id == file_id)
    if require_active:
        # figure 表无状态列，active revision 唯一过滤点是 knowledge_files
        stmt = stmt.join(KnowledgeFile, KnowledgeFile.file_id == FigureEntityRecord.file_id)
        conditions.append(KnowledgeFile.active_parse_revision_id == revision_id)
    stmt = stmt.where(*conditions).order_by(
        (FigureAssetRecord.anchor_id != "").desc(),
        (FigureAssetRecord.asset_sha256 != "").desc(),
        FigureAssetRecord.id.asc(),
    )
    return list((await db.execute(stmt)).all())


async def project_publishable_figures(
    db: AsyncSession,
    *,
    binding: dict[str, Any],
    publish_allowed: bool = False,
) -> tuple[list[PublishableFigure], str | None]:
    """七道发布门（有序）。返回 ``(figures, suppress_reason)``：
    attached → ``(figures, None)``；抑制 → ``([], reason)``。绝不 raise。
    """
    try:
        parsed = VerifiedLocatorBinding.model_validate(binding)
    except ValidationError:
        return [], SUPPRESS_BINDING_NOT_VERIFIED
    # 门1：VERIFIED 且页码/物理血统齐（freeze invariant 的防御性复查）
    if not parsed.verified or parsed.page_number is None or not parsed.physical_evidence_id:
        return [], SUPPRESS_BINDING_NOT_VERIFIED
    # 门2：只有图片/题注入口有资产可投影
    if parsed.locator_kind not in _FIGURE_KINDS:
        return [], SUPPRESS_KIND_WITHOUT_FIGURE
    kb_id = str(parsed.kb_id or "")
    file_id = str(parsed.file_id or "")
    revision_id = str(parsed.parse_revision_id or "")
    anchor_id = str(parsed.anchor_id or "")
    if not (kb_id and file_id and revision_id and anchor_id):
        # VERIFIED Binding 缺身份键 = 链路异常，无行可投影
        return [], SUPPRESS_NO_ASSET_ROW

    # 门3/4：资产行存在 + scope 一致 + active revision（抑制路径用诊断探针分级）
    rows = await _fetch_figure_rows(
        db, locator_kind=parsed.locator_kind, revision_id=revision_id, anchor_id=anchor_id, kb_id=kb_id, file_id=file_id
    )
    if not rows:
        any_scope_rows = await _fetch_figure_rows(
            db, locator_kind=parsed.locator_kind, revision_id=revision_id, anchor_id=anchor_id, require_active=False
        )
        if not any_scope_rows:
            return [], SUPPRESS_NO_ASSET_ROW
        scope_rows = await _fetch_figure_rows(
            db,
            locator_kind=parsed.locator_kind,
            revision_id=revision_id,
            anchor_id=anchor_id,
            kb_id=kb_id,
            file_id=file_id,
            require_active=False,
        )
        return [], (SUPPRESS_REVISION_NOT_ACTIVE if scope_rows else SUPPRESS_SCOPE_MISMATCH)

    asset, entity = rows[0]
    # 门5：object_name 非空且 asset_name 可推导可校验（与签发方同一接受域）
    asset_name = derive_asset_name(str(asset.object_name or ""))
    if asset_name is None:
        return [], SUPPRESS_ASSET_NAME_UNRESOLVABLE
    # 门6：指纹齐全（sha 为空 = 未指纹化资产，对象可能不可信，不发）
    asset_sha = str(asset.asset_sha256 or "")
    if not asset_sha:
        return [], SUPPRESS_ASSET_UNFINGERPRINTED
    # 门7：发布授权位（与 visual_explanation_allowed 分立）
    if not publish_allowed:
        return [], SUPPRESS_PUBLISH_NOT_ALLOWED
    figure = PublishableFigure(
        binding_id=parsed.binding_id,
        kb_id=kb_id,
        file_id=file_id,
        revision_id=revision_id,
        asset_name=asset_name,
        asset_sha256=asset_sha,
        media_type=_MEDIA_TYPE_BY_SUFFIX[posixpath.splitext(asset_name)[1].lower()],
        page=int(parsed.asset_pdf_page_number or parsed.page_number),
        panel_match=parsed.panel_match,
        evidence_id=str(parsed.physical_evidence_id),
        figure_label=str(entity.container_label or ""),
        caption=str(entity.caption or ""),
        width=int(asset.width or 0),
        height=int(asset.height or 0),
        selection={"asset_count": len(rows), "rule": _SELECTION_RULE},
    )
    return [figure], None


def figure_projection_envelope(figures: list[PublishableFigure], reason: str | None) -> dict[str, Any]:
    """投影信封：随 locator_resolution 落库（contract_hash 覆盖）并供 chat 层读取。"""
    attached = bool(figures)
    return {
        "version": FIGURE_PROJECTION_VERSION,
        "status": "attached" if attached else "suppressed",
        "reason": None if attached else (reason or SUPPRESS_PROJECTION_ERROR),
        "figures": [figure.model_dump(mode="json") for figure in figures],
    }


async def attach_figure_projection(
    db: AsyncSession,
    resolution: dict[str, Any],
    *,
    publish_allowed: bool,
) -> dict[str, Any]:
    """编排器挂接点（enforce_locator_authority 与 answer_policy 写回之后、
    contract_hash 之前）：把投影信封写进 ``locator_resolution["figure_projection"]``
    并返回。任何异常吞掉归 projection_error，绝不影响检索契约。
    """
    binding = resolution.get("binding") if isinstance(resolution, dict) else None
    if not isinstance(binding, dict):
        envelope = figure_projection_envelope([], SUPPRESS_BINDING_NOT_VERIFIED)
    else:
        try:
            figures, reason = await project_publishable_figures(db, binding=binding, publish_allowed=publish_allowed)
            envelope = figure_projection_envelope(figures, reason)
        except Exception as exc:  # noqa: BLE001 - 投影失败绝不影响检索契约
            logger.warning(f"figure asset projection failed: {exc}")
            envelope = figure_projection_envelope([], SUPPRESS_PROJECTION_ERROR)
    if isinstance(resolution, dict):
        resolution["figure_projection"] = envelope
    return envelope


__all__ = [
    "FIGURE_PROJECTION_VERSION",
    "PublishableFigure",
    "SUPPRESS_ASSET_NAME_UNRESOLVABLE",
    "SUPPRESS_ASSET_UNFINGERPRINTED",
    "SUPPRESS_BINDING_NOT_VERIFIED",
    "SUPPRESS_KIND_WITHOUT_FIGURE",
    "SUPPRESS_NO_ASSET_ROW",
    "SUPPRESS_PROJECTION_ERROR",
    "SUPPRESS_PUBLISH_NOT_ALLOWED",
    "SUPPRESS_REVISION_NOT_ACTIVE",
    "SUPPRESS_SCOPE_MISMATCH",
    "attach_figure_projection",
    "derive_asset_name",
    "figure_projection_envelope",
    "media_type_for_asset_name",
    "project_publishable_figures",
]
