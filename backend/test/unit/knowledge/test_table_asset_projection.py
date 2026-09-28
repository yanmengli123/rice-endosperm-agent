"""表格资产投影（table_asset_projection，ADR-0008 P2）单测。

覆盖：受控解析器（白名单/跨度/实体/危险内容/规模护栏）、表块提取、七道门
抑制分级（合成 SQLite 会话）、确定性 table_id、锚点文本覆盖率择优（跨页表）。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.contracts.table_asset_projection import (
    SUPPRESS_CHUNK_REVISION_UNVERIFIED,
    SUPPRESS_PUBLISH_NOT_ALLOWED,
    SUPPRESS_REVISION_NOT_ACTIVE,
    SUPPRESS_SCOPE_MISMATCH,
    SUPPRESS_TABLE_ANCHOR_MISSING,
    SUPPRESS_TABLE_HTML_MISSING,
    SUPPRESS_TABLE_TOO_LARGE,
    PublishableTable,
    extract_table_blocks,
    parse_table_html,
    project_publishable_table,
)
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    KnowledgeChunk,
    KnowledgeFile,
    KnowledgeParseRevision,
)

pytestmark = [pytest.mark.unit]

_TABLE_HTML = (
    "<table><thead><tr><th>Material</th><th>Starch</th><th>Protein</th></tr></thead>"
    "<tbody><tr><td>WT</td><td>75.2</td><td>8.9</td></tr>"
    "<tr><td>osmyb73</td><td>68.4</td><td>10.2</td></tr></tbody></table>"
)
_ANCHOR_QUOTE = "Table 1. Physicochemical properties of grains Material Starch Protein WT 75.2 8.9 osmyb73 68.4 10.2"


# ---- 受控解析器 ----


def test_parse_thead_headers_and_spans():
    rows, header_rows, limited, error = parse_table_html(
        '<table><thead><tr><th rowspan="2">材料</th><th colspan="2">含量</th></tr>'
        "<tr><th>淀粉</th><th>蛋白</th></tr></thead>"
        "<tbody><tr><td>WT</td><td>75.2 &plusmn; 1.1</td><td>8.9</td></tr></tbody></table>"
    )
    assert error is None and limited is False
    assert header_rows == 2  # thead 连续两行含 th
    first = rows[0]
    assert first[0].rowspan == 2 and first[1].colspan == 2 and first[0].header
    assert rows[2][1].text == "75.2 ± 1.1"  # HTML 实体解码
    assert not rows[2][1].header


def test_parse_infers_textual_td_header_over_numeric_data():
    rows, header_rows, limited, error = parse_table_html(
        "<table><tr><td>Genotype</td><td>Chalkiness (%)</td></tr>"
        "<tr><td>WT</td><td>12.1</td></tr><tr><td>mutant</td><td>26.8</td></tr></table>"
    )
    assert error is None and limited is False
    assert header_rows == 1
    assert rows is not None and all(cell.header for cell in rows[0])
    assert all(not cell.header for cell in rows[1])


def test_parse_does_not_infer_arbitrary_text_body_as_header():
    rows, header_rows, _, error = parse_table_html(
        "<table><tr><td>WT</td><td>tall</td></tr><tr><td>mutant</td><td>short</td></tr></table>"
    )
    assert error is None and rows is not None
    assert header_rows == 0


def test_parse_drops_script_style_content_entirely():
    rows, _, limited, error = parse_table_html(
        "<table><tr><td>a<script>alert(1)</script>b</td>"
        "<td><style>.x{}</style>c<img src=x onerror=alert(1)>d</td></tr></table>"
    )
    assert error is None and limited is False
    assert [cell.text for cell in rows[0]] == ["ab", "cd"]  # 危险标签连内容带属性全丢弃


def test_parse_row_limit_truncates_instead_of_rejecting():
    big = "<table>" + "".join(f"<tr><td>r{i}</td></tr>" for i in range(260)) + "</table>"
    rows, _, limited, error = parse_table_html(big)
    assert error is None
    assert len(rows) == 200  # _MAX_ROWS 截断：部分展示优于全无
    assert limited is True  # 截断必须可见（UI 文案据此区分跨页/规模）


def test_parse_col_and_cell_limits_mark_limited():
    wide = "<table><tr>" + "".join(f"<td>c{i}</td>" for i in range(70)) + "</tr></table>"
    rows, _, limited, error = parse_table_html(wide)
    assert error is None and limited is True
    assert all(len(row) <= 60 for row in rows)  # _MAX_COLS 截断

    dense = "<table>" + "".join("<tr>" + "".join(f"<td>{i}</td>" for i in range(30)) + "</tr>" for _ in range(200))
    dense += "</table>"  # 6000 格 > _MAX_CELLS=4000
    rows, _, limited, error = parse_table_html(dense)
    assert error is None and limited is True
    assert sum(len(row) for row in rows) <= 4000


def test_parse_html_byte_limit_rejected_as_too_large():
    """HTML 预截断会劈掉闭合标签 → 必然不完整，拒绝比误导诚实（ADR-0008 P2）。"""
    huge = "<table><tr><td>" + ("x" * 200_050) + "</td></tr></table>"
    rows, header_rows, limited, error = parse_table_html(huge)
    assert rows is None and header_rows == 0 and limited is False
    assert error == SUPPRESS_TABLE_TOO_LARGE


def test_parse_empty_or_broken_table_rejected():
    assert parse_table_html("")[0] is None
    assert parse_table_html("<table><tr></tr></table>")[3] is not None
    assert parse_table_html("no table at all")[0] is None


def test_extract_table_blocks_case_and_multiline():
    blocks = extract_table_blocks(
        "前文\n<TABLE class='x'>\n<tr><td>a</td></tr>\n</TABLE>\n后文 <table><tr><td>b</td></tr></table>"
    )
    assert len(blocks) == 2
    assert extract_table_blocks("没有任何表格") == []


# ---- 投影门（合成会话） ----


@pytest_asyncio.fixture
async def table_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(EvidenceAnchorRecord.__table__.create)
        await connection.run_sync(KnowledgeChunk.__table__.create)
        await connection.run_sync(KnowledgeFile.__table__.create)
        await connection.run_sync(KnowledgeParseRevision.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        # SQLite 方言：自增主键显式 id（AGENTS.md 测试注意事项）
        session.add(
            KnowledgeFile(
                id=1,
                file_id="file-a",
                kb_id="kb-a",
                filename="paper-a.pdf",
                active_parse_revision_id="pr_1",
                active_index_revision_id="ir_1",
            )
        )
        session.add(
            EvidenceAnchorRecord(
                id=10,
                anchor_id="ea_tab1",
                parse_revision_id="pr_1",
                page=11,
                bbox=[40.0, 300.0, 560.0, 480.0],
                word_start=0,
                word_end=0,
                quote_hash="c" * 64,
                prefix_hash="0" * 64,
                suffix_hash="0" * 64,
                quote=_ANCHOR_QUOTE,
                fragments=[],
                anchor_type="table",
                locator_quality="HIGH",
                confidence=1.0,
                locatable=True,
                source="mineru",
            )
        )
        session.add(
            KnowledgeChunk(
                id=20,
                chunk_id="file-a_rev_1_chunk_0",
                file_id="file-a",
                kb_id="kb-a",
                chunk_index=0,
                content=(
                    f"Table 1. Physicochemical properties of grains\n{_TABLE_HTML}\n【页码】11\n【证据锚点】ea_tab1"
                ),
                source_provenance={"parse_revision_id": "pr_1", "index_revision_id": "ir_1"},
            )
        )
        await session.flush()
        yield session
    await engine.dispose()


async def _project(session, **overrides):
    kwargs = dict(
        kb_id="kb-a",
        file_id="file-a",
        revision_id="pr_1",
        anchor_id="ea_tab1",
        evidence_id="ev_t1",
        page=11,
        label="Table 1",
        caption="Table 1. Physicochemical properties of grains",
        publish_allowed=True,
    )
    kwargs.update(overrides)
    return await project_publishable_table(session, **kwargs)


@pytest.mark.asyncio
async def test_gates_attached_full_fields(table_session):
    table, reason = await _project(table_session)
    assert reason is None and isinstance(table, PublishableTable)
    assert table.anchor_id == "ea_tab1" and table.page == 11
    assert table.label == "Table 1" and table.row_count == 3 and table.header_rows == 1
    assert table.col_count == 3
    assert table.rows[0][0].header and table.rows[0][0].text == "Material"
    assert table.table_id.startswith("tbl_") and len(table.table_id) == 24  # 确定性派生
    assert table.selection["chunk_id"] == "file-a_rev_1_chunk_0"
    assert table.truncated is False and table.limited is False  # 完整小表不得标截断


@pytest.mark.asyncio
async def test_gate_anchor_missing(table_session):
    table, reason = await _project(table_session, anchor_id="ea_nope")
    assert table is None and reason == SUPPRESS_TABLE_ANCHOR_MISSING


@pytest.mark.asyncio
async def test_gate_scope_mismatch(table_session):
    table, reason = await _project(table_session, kb_id="kb-other")
    assert table is None and reason == SUPPRESS_SCOPE_MISMATCH


@pytest.mark.asyncio
async def test_gate_revision_not_active(table_session):
    file_row = (await table_session.execute(_select_file())).scalars().one()
    file_row.active_parse_revision_id = "pr_2"
    await table_session.flush()
    table, reason = await _project(table_session)
    assert table is None and reason == SUPPRESS_REVISION_NOT_ACTIVE


@pytest.mark.asyncio
async def test_gate_html_missing_when_no_table_chunk(table_session):
    chunk = (await table_session.execute(_select_chunk())).scalars().one()
    chunk.content = chunk.content.replace("<table", "<div").replace("</table>", "</div>")
    await table_session.flush()
    table, reason = await _project(table_session)
    assert table is None and reason == SUPPRESS_TABLE_HTML_MISSING


@pytest.mark.asyncio
async def test_gate_publish_not_allowed(table_session):
    table, reason = await _project(table_session, publish_allowed=False)
    assert table is None and reason == SUPPRESS_PUBLISH_NOT_ALLOWED


@pytest.mark.asyncio
async def test_multichunk_cross_page_picks_best_match(table_session):
    # 跨页续表：第二个 chunk 只含部分行（重复表头），锚点相同——择优取覆盖最高块
    table_session.add(
        KnowledgeChunk(
            id=21,
            chunk_id="file-a_rev_1_chunk_1",
            file_id="file-a",
            kb_id="kb-a",
            chunk_index=1,
            content=(
                "Table 1. Physicochemical properties of grains (continued)\n"
                "<table><thead><tr><th>Material</th><th>Starch</th></tr></thead>"
                "<tbody><tr><td>osNF-YB1</td><td>70.1</td></tr></tbody></table>\n"
                "【页码】12\n【证据锚点】ea_tab1"
            ),
            source_provenance={"parse_revision_id": "pr_1", "index_revision_id": "ir_1"},
        )
    )
    await table_session.flush()
    table, reason = await _project(table_session)
    assert reason is None
    assert table.row_count == 3  # 覆盖最高的完整块（首个 chunk）
    assert table.truncated is True  # 存在其他达标延续块 → 标记截断


def _select_file():
    from sqlalchemy import select

    return select(KnowledgeFile).where(KnowledgeFile.file_id == "file-a")


def _select_chunk():
    from sqlalchemy import select

    return select(KnowledgeChunk).where(KnowledgeChunk.chunk_id == "file-a_rev_1_chunk_0")


@pytest.mark.asyncio
async def test_gate_limited_table_marks_truncated(table_session):
    """护栏截断必须上屏：limited=True 并入 truncated（绝不可静默截断，ADR-0008 P2）。"""
    chunk = (await table_session.execute(_select_chunk())).scalars().one()
    header = "<thead><tr><th>Material</th><th>Starch</th><th>Protein</th></tr></thead>"
    seed_rows = "<tr><td>WT</td><td>75.2</td><td>8.9</td></tr><tr><td>osmyb73</td><td>68.4</td><td>10.2</td></tr>"
    filler = "".join(f"<tr><td>WT r{i}</td><td>75.2</td><td>8.9</td></tr>" for i in range(260))
    chunk.content = (
        "Table 1. Physicochemical properties of grains\n"
        f"<table>{header}<tbody>{seed_rows}{filler}</tbody></table>\n【页码】11\n【证据锚点】ea_tab1"
    )
    await table_session.flush()
    table, reason = await _project(table_session)
    assert reason is None
    assert table.row_count == 200  # 护栏按行截断
    assert table.truncated is True and table.limited is True


@pytest.mark.asyncio
async def test_selection_records_chunk_revision_evidence(table_session):
    """chunk 表无 revision 列 → source_provenance 必须双修订匹配并可审计。"""
    chunk = (await table_session.execute(_select_chunk())).scalars().one()
    chunk.source_provenance = {"parse_revision_id": "pr_1", "index_revision_id": "ir_1"}
    await table_session.flush()
    table, reason = await _project(table_session)
    assert reason is None
    assert table.selection["chunk_parse_revision"] == "pr_1"
    assert table.selection["chunk_index_revision"] == "ir_1"
    assert table.selection["active_revision_match"] is True
    assert table.selection["active_index_revision_match"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provenance",
    [
        {},
        {"parse_revision_id": "pr_old", "index_revision_id": "ir_1"},
        {"parse_revision_id": "pr_1", "index_revision_id": "ir_old"},
    ],
)
async def test_stale_or_unversioned_chunk_is_fail_closed(table_session, provenance):
    """同 file/anchor 的旧 chunk 也不能跨修订发布为当前表卡。"""
    chunk = (await table_session.execute(_select_chunk())).scalars().one()
    chunk.source_provenance = provenance
    await table_session.flush()
    table, reason = await _project(table_session)
    assert table is None
    assert reason == SUPPRESS_CHUNK_REVISION_UNVERIFIED


@pytest.mark.asyncio
async def test_missing_active_index_revision_is_fail_closed(table_session):
    file_row = (await table_session.execute(_select_file())).scalars().one()
    file_row.active_index_revision_id = None
    await table_session.flush()
    table, reason = await _project(table_session)
    assert table is None
    assert reason == SUPPRESS_CHUNK_REVISION_UNVERIFIED
