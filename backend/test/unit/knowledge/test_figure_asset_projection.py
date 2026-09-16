"""图卡资产投影（figure_asset_projection）单测：金标 G1–G9 + 契约锁。

覆盖：七道发布门分级、两种 locator_kind 的 join 分流（图片=视觉锚点直查 /
题注=实体 caption_anchor_id 中转）、确定性资产选择、I4 镜像校验（与
services/knowledge_asset_service 的 asset_name 接受域等价）、I6 异常吞噬、
contract_hash 对 figure_projection 信封的覆盖（挂接点必须在 _hash_contract
之前，本测试锁定「信封进哈希」这一半）。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.contracts.figure_asset_projection import (
    FIGURE_PROJECTION_VERSION,
    SUPPRESS_ASSET_NAME_UNRESOLVABLE,
    SUPPRESS_ASSET_UNFINGERPRINTED,
    SUPPRESS_BINDING_NOT_VERIFIED,
    SUPPRESS_KIND_WITHOUT_FIGURE,
    SUPPRESS_NO_ASSET_ROW,
    SUPPRESS_PROJECTION_ERROR,
    SUPPRESS_PUBLISH_NOT_ALLOWED,
    SUPPRESS_REVISION_NOT_ACTIVE,
    SUPPRESS_SCOPE_MISMATCH,
    PublishableFigure,
    attach_figure_projection,
    derive_asset_name,
    figure_projection_envelope,
    project_publishable_figures,
)
from yuxi.knowledge.contracts.locator_binding import (
    LOCATOR_KIND_FIGURE_CAPTION,
    LOCATOR_KIND_FIGURE_IMAGE,
    LOCATOR_KIND_QUOTE,
)
from yuxi.storage.postgres.models_knowledge import (
    FigureAssetRecord,
    FigureEntityRecord,
    KnowledgeFile,
    KnowledgeParseRevision,
)

pytestmark = [pytest.mark.unit]

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_ASSET_BASENAME = "0123456789abcdef012345678-fig1.png"
_OBJECT_NAME = f"tenants/1/documents/{_SHA_A}/mineru/pr_1/images/{_ASSET_BASENAME}"
_CAPTION = "Figure 1. Expression patterns of OsMYB73 in rice seeds during development."


@pytest_asyncio.fixture
async def figure_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(KnowledgeFile.__table__.create)
        await connection.run_sync(KnowledgeParseRevision.__table__.create)
        await connection.run_sync(FigureEntityRecord.__table__.create)
        await connection.run_sync(FigureAssetRecord.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _binding(**overrides) -> dict:
    payload = {
        "binding_id": "vlb_" + "1" * 20,
        "status": "VERIFIED",
        "locator_kind": LOCATOR_KIND_FIGURE_IMAGE,
        "physical_evidence_id": "ev_fig1",
        "anchor_id": "ea_fig1",
        "file_id": "file-a",
        "filename": "paper-a.pdf",
        "parse_revision_id": "pr_1",
        "kb_id": "kb-a",
        "page_number": 4,
        "asset_pdf_page_number": 4,
        "page_binding": "VERIFIED",
        "figure_identity_binding": "VERIFIED",
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not ...}


async def _seed_source(session, *, active_revision: str = "pr_1", row_id: int = 1) -> None:
    # SQLite 方言：BIGINT/自增主键需显式 id（见 AGENTS.md 测试注意事项）
    session.add(
        KnowledgeParseRevision(
            id=row_id,
            revision_id="pr_1",
            tenant_id=1,
            kb_id="kb-a",
            file_id="file-a",
            source_sha256=_SHA_A,
            parser_fingerprint="f" * 64,
            pipeline_version="scientific_pdf_v2.8",
            status="INDEXED_FULL",
        )
    )
    session.add(
        KnowledgeFile(
            id=row_id,
            file_id="file-a",
            kb_id="kb-a",
            filename="paper-a.pdf",
            active_parse_revision_id=active_revision,
        )
    )


async def _seed_figure(
    session,
    *,
    entity_id: int = 10,
    asset_id: int = 100,
    asset_key: str = "images/fig1.jpg",
    asset_anchor_id: str = "ea_fig1",
    object_name: str = _OBJECT_NAME,
    sha: str = _SHA_B,
    label: str | None = "Figure 1",
    caption: str | None = _CAPTION,
    caption_anchor_id: str | None = "ea_cap1",
) -> None:
    session.add(
        FigureEntityRecord(
            id=entity_id,
            tenant_id=1,
            parse_revision_id="pr_1",
            kb_id="kb-a",
            file_id="file-a",
            source_sha256=_SHA_A,
            entity_key="figure 1",
            container_label=label,
            caption=caption,
            caption_page=4,
            caption_anchor_id=caption_anchor_id,
            document_partition="MAIN_TEXT",
            association_method="span_linkage",
            asset_count=1,
        )
    )
    await session.flush()
    session.add(
        FigureAssetRecord(
            id=asset_id,
            tenant_id=1,
            entity_id=entity_id,
            parse_revision_id="pr_1",
            kb_id="kb-a",
            asset_key=asset_key,
            img_path=asset_key,
            anchor_id=asset_anchor_id,
            object_bucket="knowledgebases",
            object_name=object_name,
            asset_sha256=sha,
            asset_phash="ffff0000ffff0000",
            panel_phashes={},
            mime="png",
            width=800,
            height=600,
            bbox=[40.0, 400.0, 280.0, 480.0],
            page=4,
        )
    )


async def _seed_asset(
    session,
    *,
    entity_id: int,
    asset_id: int,
    asset_key: str,
    asset_anchor_id: str = "ea_fig1",
    object_name: str = _OBJECT_NAME,
    sha: str = _SHA_B,
    role: str = "panel",
    group_index: int = 0,
) -> None:
    """只追加资产行（同一实体的多资产场景，避免重复实体 PK/唯一键冲突）。"""
    session.add(
        FigureAssetRecord(
            id=asset_id,
            tenant_id=1,
            entity_id=entity_id,
            parse_revision_id="pr_1",
            kb_id="kb-a",
            asset_key=asset_key,
            img_path=asset_key,
            anchor_id=asset_anchor_id,
            object_bucket="knowledgebases",
            object_name=object_name,
            asset_sha256=sha,
            asset_phash="ffff0000ffff0000",
            panel_phashes={},
            mime="png",
            width=800,
            height=600,
            bbox=[40.0, 400.0, 280.0, 480.0],
            page=4,
            role=role,
            group_index=group_index,
        )
    )


# ---- G10：图组投影（A2）——primary 优先、阅读序、成员各自过门 ----


@pytest.mark.asyncio
async def test_g10_group_projection_primary_first_then_reading_order(figure_session):
    await _seed_source(figure_session)
    # 题注块 k（阅读序 2）；合成整图 primary（group_index -1）；panel a（0）；panel b 无对象（跳过不抑制）
    await _seed_figure(figure_session, asset_key="images/k.jpg")
    synthetic_name = f"tenants/1/documents/{_SHA_A}/mineru/pr_1/images/{'c' * 24}-synthetic_figure_1.png"
    await _seed_asset(
        figure_session,
        entity_id=10,
        asset_id=101,
        asset_key="synthetic:figure 1",
        object_name=synthetic_name,
        sha="c" * 64,
        role="primary",
        group_index=-1,
    )
    await _seed_asset(
        figure_session,
        entity_id=10,
        asset_id=102,
        asset_key="images/a.jpg",
        object_name=f"tenants/1/documents/{_SHA_A}/mineru/pr_1/images/{'d' * 24}-a.jpg",
        sha="d" * 64,
        group_index=0,
    )
    await _seed_asset(
        figure_session, entity_id=10, asset_id=103, asset_key="images/b.jpg", object_name="", sha="", group_index=1
    )
    await figure_session.flush()
    # 把题注块 k 标为阅读序 2
    k_row = (await figure_session.execute(select(FigureAssetRecord).where(FigureAssetRecord.id == 100))).scalars().one()
    k_row.group_index = 2
    await figure_session.flush()

    binding = _binding(locator_kind=LOCATOR_KIND_FIGURE_CAPTION, anchor_id="ea_cap1", asset_pdf_page_number=None)
    figures, reason = await project_publishable_figures(figure_session, binding=binding, publish_allowed=True)
    assert reason is None
    assert [figure.role for figure in figures] == ["primary", "panel", "panel"]
    assert [figure.group_index for figure in figures] == [-1, 0, 2]
    assert figures[0].asset_name.endswith("-synthetic_figure_1.png") and figures[0].media_type == "image/png"
    assert figures[1].media_type == "image/jpeg"
    assert all(figure.page == 4 and figure.caption == _CAPTION for figure in figures)  # 页码/题注仍来自 Binding/实体
    assert figures[0].selection["asset_count"] == 4  # 无对象的 b 计入组规模但不发布


# ---- G1：FIGURE_IMAGE 全字段（含 I2 页码唯一来源 / asset_name 完整 basename）----


@pytest.mark.asyncio
async def test_g1_figure_image_full_fields(figure_session):
    await _seed_source(figure_session)
    await _seed_figure(figure_session)
    figures, reason = await project_publishable_figures(figure_session, binding=_binding(), publish_allowed=True)
    assert reason is None
    assert len(figures) == 1
    figure = figures[0]
    # asset_name 是对象完整 basename（含 digest 段），不做剥前缀手术
    assert figure.asset_name == _ASSET_BASENAME
    assert figure.media_type == "image/png"
    assert figure.page == 4  # I2：页码来自 Binding.asset_pdf_page_number，非 figure_assets.page
    assert figure.evidence_id == "ev_fig1"
    assert figure.figure_label == "Figure 1" and figure.caption == _CAPTION
    assert figure.kb_id == "kb-a" and figure.file_id == "file-a" and figure.revision_id == "pr_1"
    assert figure.projection_version == FIGURE_PROJECTION_VERSION
    assert figure.selection == {"asset_count": 1, "rule": "primary_first_then_reading_order"}


# ---- G2：FIGURE_CAPTION 经实体 caption_anchor_id 中转；页码回落 Binding.page_number ----


@pytest.mark.asyncio
async def test_g2_figure_caption_via_entity(figure_session):
    await _seed_source(figure_session)
    # 题注入口：Binding.anchor_id 是题注锚点；资产自己的视觉锚点不同也不影响
    await _seed_figure(figure_session, asset_anchor_id="ea_visual")
    binding = _binding(
        locator_kind=LOCATOR_KIND_FIGURE_CAPTION,
        anchor_id="ea_cap1",
        asset_pdf_page_number=None,
        page_number=6,
    )
    figures, reason = await project_publishable_figures(figure_session, binding=binding, publish_allowed=True)
    assert reason is None
    assert len(figures) == 1
    assert figures[0].page == 6  # caption 入口无 asset 页，回落 Binding.page_number（题注验证页）
    assert figures[0].caption == _CAPTION


# ---- G3：QUOTE_LOCATOR 无资产可投影 ----


@pytest.mark.asyncio
async def test_g3_quote_kind_without_figure(figure_session):
    await _seed_source(figure_session)
    await _seed_figure(figure_session)
    figures, reason = await project_publishable_figures(
        figure_session, binding=_binding(locator_kind=LOCATOR_KIND_QUOTE), publish_allowed=True
    )
    assert figures == []
    assert reason == SUPPRESS_KIND_WITHOUT_FIGURE


# ---- 门1：非 VERIFIED / 畸形 Binding / 缺身份键 ----


@pytest.mark.asyncio
async def test_gate1_not_verified_or_malformed(figure_session):
    await _seed_source(figure_session)
    await _seed_figure(figure_session)
    figures, reason = await project_publishable_figures(
        figure_session, binding=_binding(status="MULTIPLE_MATCHES"), publish_allowed=True
    )
    assert (figures, reason) == ([], SUPPRESS_BINDING_NOT_VERIFIED)
    figures, reason = await project_publishable_figures(
        figure_session, binding={"status": "VERIFIED"}, publish_allowed=True
    )
    assert (figures, reason) == ([], SUPPRESS_BINDING_NOT_VERIFIED)
    figures, reason = await project_publishable_figures(
        figure_session, binding=_binding(anchor_id=None), publish_allowed=True
    )
    assert (figures, reason) == ([], SUPPRESS_NO_ASSET_ROW)


# ---- G4：revision 非 active（重解析窗口期 / 废弃 revision）----


@pytest.mark.asyncio
async def test_g4_revision_not_active(figure_session):
    await _seed_source(figure_session, active_revision="pr_old")
    await _seed_figure(figure_session)
    figures, reason = await project_publishable_figures(figure_session, binding=_binding(), publish_allowed=True)
    assert (figures, reason) == ([], SUPPRESS_REVISION_NOT_ACTIVE)


# ---- G5：object_name 空 / asset_name 不可校验；sha 空 ----


@pytest.mark.asyncio
async def test_g5_object_name_or_fingerprint_missing(figure_session):
    await _seed_source(figure_session)
    # 场景一：object_name 为空串（入库时 safe_name 撞名，映射被整体放弃）
    await _seed_figure(figure_session, object_name="")
    figures, reason = await project_publishable_figures(figure_session, binding=_binding(), publish_allowed=True)
    assert (figures, reason) == ([], SUPPRESS_ASSET_NAME_UNRESOLVABLE)


@pytest.mark.asyncio
async def test_g5b_invalid_asset_name(figure_session):
    await _seed_source(figure_session)
    # 场景二：对象存在但 basename 过不了发布校验（空格 → 端点同样会拒绝）
    await _seed_figure(figure_session, object_name="tenants/1/images/fig 1.png")
    figures, reason = await project_publishable_figures(figure_session, binding=_binding(), publish_allowed=True)
    assert (figures, reason) == ([], SUPPRESS_ASSET_NAME_UNRESOLVABLE)


@pytest.mark.asyncio
async def test_g5c_unfingerprinted(figure_session):
    await _seed_source(figure_session)
    await _seed_figure(figure_session, sha="")
    figures, reason = await project_publishable_figures(figure_session, binding=_binding(), publish_allowed=True)
    assert (figures, reason) == ([], SUPPRESS_ASSET_UNFINGERPRINTED)


# ---- G6：多资产行确定性选择（题注入口按实体 join，兄弟资产全部入候选；
# anchor 优先 > sha 优先 > id 升序）----


@pytest.mark.asyncio
async def test_g6_deterministic_multi_asset_selection(figure_session):
    await _seed_source(figure_session)
    await _seed_figure(figure_session, asset_id=100, asset_anchor_id="", sha="", asset_key="images/raw.jpg")
    # id=101 更大但有锚点+指纹：anchor 优先级必须压过 id 升序
    await _seed_asset(figure_session, entity_id=10, asset_id=101, asset_key="images/fig1b.jpg")
    binding = _binding(locator_kind=LOCATOR_KIND_FIGURE_CAPTION, anchor_id="ea_cap1", asset_pdf_page_number=None)
    figures, reason = await project_publishable_figures(figure_session, binding=binding, publish_allowed=True)
    assert reason is None
    assert len(figures) == 1
    assert figures[0].asset_name == _ASSET_BASENAME
    assert figures[0].selection["asset_count"] == 2
    assert figures[0].selection["rule"] == "primary_first_then_reading_order"


# ---- G7：scope 错配（Binding 的 kb 与行 kb 不一致）----


@pytest.mark.asyncio
async def test_g7_scope_mismatch(figure_session):
    await _seed_source(figure_session)
    await _seed_figure(figure_session)
    figures, reason = await project_publishable_figures(
        figure_session, binding=_binding(kb_id="kb-b"), publish_allowed=True
    )
    assert (figures, reason) == ([], SUPPRESS_SCOPE_MISMATCH)


# ---- G8：无题注无编号实体仍可发布（标题回退交前端中性文案）----


@pytest.mark.asyncio
async def test_g8_title_fallback_chain(figure_session):
    await _seed_source(figure_session)
    await _seed_figure(figure_session, label=None, caption=None, caption_anchor_id=None)
    binding = _binding()
    binding.pop("figure_identity_binding")  # 身份未确认也允许发布图卡（编号展示由前端按 identity 门控）
    figures, reason = await project_publishable_figures(figure_session, binding=binding, publish_allowed=True)
    assert reason is None
    assert figures[0].caption == "" and figures[0].figure_label == ""


# ---- 门7：发布授权位 ----


@pytest.mark.asyncio
async def test_gate7_publish_not_allowed(figure_session):
    await _seed_source(figure_session)
    await _seed_figure(figure_session)
    figures, reason = await project_publishable_figures(figure_session, binding=_binding(), publish_allowed=False)
    assert (figures, reason) == ([], SUPPRESS_PUBLISH_NOT_ALLOWED)


# ---- G9：attach_figure_projection 异常吞噬（I6）+ 信封写入 ----


@pytest.mark.asyncio
async def test_g9_attach_swallows_exceptions_and_writes_envelope(figure_session):
    await _seed_source(figure_session)
    await _seed_figure(figure_session)

    class _BoomSession:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("boom")

    envelope = await attach_figure_projection(_BoomSession(), {"binding": _binding()}, publish_allowed=True)
    assert envelope["status"] == "suppressed"
    assert envelope["reason"] == SUPPRESS_PROJECTION_ERROR

    resolution = {"binding": _binding()}
    envelope = await attach_figure_projection(figure_session, resolution, publish_allowed=True)
    assert envelope["status"] == "attached" and envelope["reason"] is None
    assert resolution["figure_projection"] is envelope

    empty = {}
    envelope = await attach_figure_projection(figure_session, empty, publish_allowed=True)
    assert envelope["status"] == "suppressed" and envelope["reason"] == SUPPRESS_BINDING_NOT_VERIFIED
    assert empty["figure_projection"] is envelope


# ---- I4：asset_name 接受域与资产服务端点镜像等价 ----


def test_i4_asset_name_acceptance_mirrors_asset_service():
    from yuxi.services.knowledge_asset_service import KnowledgeAssetError, _validate_asset_name

    valid = [
        _ASSET_BASENAME,
        "fig.s2.jpg",
        "0123456789abcdef01234567-a" * 8 + ".png",  # 长边距仍在 255 内
        "fig1.webp",
        "fig1.gif",
    ]
    invalid = [
        "",
        "fig 1.png",
        "fig1.svg",
        "fig1.txt",
        "fig1",  # 无扩展名
        "a" * 256 + ".png",  # 超长
        "fig\\1.png",
    ]
    for name in valid:
        assert derive_asset_name(f"any/prefix/{name}") == name
        assert _validate_asset_name(name) == name
    for name in invalid:
        assert derive_asset_name(name) is None
        with pytest.raises(KnowledgeAssetError):
            _validate_asset_name(name)


# ---- 契约：信封形状 + PublishableFigure 禁多余字段 + hash 覆盖 ----


def test_envelope_shape_and_extra_forbid():
    attached = figure_projection_envelope(
        [
            PublishableFigure(
                binding_id="vlb_x",
                kb_id="kb-a",
                file_id="file-a",
                revision_id="pr_1",
                asset_name=_ASSET_BASENAME,
                asset_sha256=_SHA_B,
                media_type="image/png",
                page=4,
                evidence_id="ev_1",
            )
        ],
        None,
    )
    assert attached["version"] == FIGURE_PROJECTION_VERSION
    assert attached["status"] == "attached" and attached["reason"] is None
    assert attached["figures"][0]["asset_name"] == _ASSET_BASENAME

    suppressed = figure_projection_envelope([], None)
    assert suppressed["status"] == "suppressed"
    assert suppressed["reason"] == SUPPRESS_PROJECTION_ERROR  # 空原因兜底

    with pytest.raises(ValidationError):
        PublishableFigure(
            binding_id="vlb_x",
            kb_id="kb-a",
            file_id="file-a",
            revision_id="pr_1",
            asset_name=_ASSET_BASENAME,
            asset_sha256=_SHA_B,
            media_type="image/png",
            page=4,
            evidence_id="ev_1",
            object_name="must-be-rejected",  # extra="forbid"：红线字段绝不进投影
        )


def test_figure_projection_is_hash_covered():
    """挂接点回归锁（一半）：figure_projection 属于 contract_hash 稳定域，
    信封变化必须改变哈希——防止后续有人把该键排除出 _hash_contract。"""
    from yuxi.knowledge.orchestration.retrieval_orchestrator import _hash_contract

    attached = figure_projection_envelope(
        [
            PublishableFigure(
                binding_id="vlb_x",
                kb_id="kb-a",
                file_id="file-a",
                revision_id="pr_1",
                asset_name=_ASSET_BASENAME,
                asset_sha256=_SHA_B,
                media_type="image/png",
                page=4,
                evidence_id="ev_1",
            )
        ],
        None,
    )
    suppressed = figure_projection_envelope([], SUPPRESS_NO_ASSET_ROW)
    contract = {"evidence": [], "locator_resolution": {"figure_projection": attached}}
    first = _hash_contract(contract)
    contract["locator_resolution"]["figure_projection"] = suppressed
    assert _hash_contract(contract) != first


def test_trace_event_types_registered_for_sla():
    """真实 run 暴露的缺口回归锁：投影 trace 事件必须在 trace 协议注册表内（未注册会被 recorder
    静默丢弃，单测里 recorder 是 no-op 测不到），且携带 runbook SQL 依赖的 reason 属性。"""
    from yuxi.trace.protocol import EVENT_ATTRIBUTE_SCHEMAS

    for event_type in ("knowledge.figure_projection.attached", "knowledge.figure_projection.suppressed"):
        assert {"reason", "figure_count", "locator_kind"} <= EVENT_ATTRIBUTE_SCHEMAS[event_type]
