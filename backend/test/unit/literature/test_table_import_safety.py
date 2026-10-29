"""表格导入安全验收（BERTopic 接入断点任务；2026-10-12）。

钉死三条安全边界与原始行留存契约：
1. Excel 公式单元格只读缓存值（data_only），绝不求值——无缓存值时该行按
   no_value 失败关闭，不半猜数值；
2. xlsm 宏部件（vbaProject.bin）与外链部件（externalLinks/…file:///…）
   对导入是死数据：只解析工作表本地值，无宏执行面、无外链跟随面；
3. CSV 公式注入样式单元格（=、+、-、@ 开头）一律按字面文本处理，不解释、
   不裁剪、不部分执行；
4. 题录导入保留原始行（batch_id + record_no + 原始 payload + rid 关联），
   同文件重传幂等（不重复行、不新建文献），历史批次缺行可由重传补齐。

不调用真实供应商、不触碰 MinIO（_store_source 打桩）。
"""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import journals, reference_import
from yuxi.storage.postgres.models_knowledge import (
    LiteratureCitation,
    LiteraturePaper,
    LiteratureReferenceBatch,
    LiteratureReferenceMember,
    LiteratureReferenceRecord,
)

pytestmark = pytest.mark.unit

FIXTURES = reference_import.__file__  # 仅用于定位；实际题录 fixture 走统一目录
from pathlib import Path  # noqa: E402

FIXTURES = Path(reference_import.__file__).resolve().parents[0]
# test 文件位于 test/unit/literature/；题录 fixtures 在 backend/test/fixtures/reference_lists
REFERENCE_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "reference_lists"


def _xlsx(sheets: list[tuple[str, list[list]]]) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, grid in sheets:
        sheet = workbook.create_sheet(title=name)
        for row in grid:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ---------------------------------------------------------------- Excel 安全边界


def test_xlsx_formula_cells_never_evaluated():
    """公式单元格（=1+1）只读缓存值：无缓存 → None。

    真实 Excel 打开会显示 2；导入端绝不能出现求值产物 2，也不得把公式串当数值。
    行内 Q1 分区仍有效 → 行保留但 jif 为空，不半猜。
    """
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "JCR"
    sheet.append(["期刊名", "JIF", "分区", "年份"])
    sheet.append(["Journal A", "=1+1", "Q1", 2024])  # 公式（openpyxl 保存公式串，无缓存值）
    buffer = io.BytesIO()
    workbook.save(buffer)

    parsed = journals.parse_journals_table(buffer.getvalue(), "formula.xlsx")
    assert len(parsed["rows"]) == 1  # 分区 Q1 有效，行保留
    row = parsed["rows"][0]
    assert row["jif"] is None  # 无缓存值 → None；绝不等于 2（求值产物）或 "=1+1"
    assert row["quartile"] == "Q1"


def test_macro_and_external_link_parts_are_inert():
    """xlsm 宏部件与外链部件是死数据：只解析工作表本地值，不执行、不跟随。

    构造带恶意假 vbaProject.bin 与 file:/// 外链引用的容器（PK 头判型为 xlsx 家族），
    导入必须正常完成且指标值保持本地单元格值。
    """
    payload = _xlsx([("JCR", [["期刊名", "JIF", "分区", "年份"], ["Journal A", 1.5, "Q1", 2024]])])
    source = io.BytesIO(payload)
    sabotaged = io.BytesIO()
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(sabotaged, "w") as zout:
        for item in zin.infolist():
            zout.writestr(item, zin.read(item.filename))
        zout.writestr("xl/vbaProject.bin", b"\x00MZ\x00not-a-real-macro-and-never-executed")
        zout.writestr(
            "xl/externalLinks/externalLink1.xml",
            "<externalLink xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'>"
            "<externalBook><fileReference r:id='rId1'/></externalBook></externalLink>",
        )
        zout.writestr(
            "xl/externalLinks/_rels/externalLink1.xml.rels",
            "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
            "<Relationship Id='rId1' Target='file:///etc/passwd'/></Relationships>",
        )

    parsed = journals.parse_journals_table(sabotaged.getvalue(), "sabotaged.xlsm")
    assert len(parsed["rows"]) == 1
    assert parsed["rows"][0]["jif"] == 1.5  # 值保持本地缓存，未被宏/外链影响
    assert parsed["skipped"] == {"no_journal": 0, "no_year": 0, "no_value": 0}


def test_csv_formula_like_cells_stay_literal():
    """CSV 公式注入样式（=、+、-、@ 开头）一律字面文本：不解释、不裁剪、不执行。"""
    payload = (
        "期刊名,JIF,年份\n"
        "=cmd|'/C calc'!A0,2.0,2024\n"
        "@SUM(Journal),1.0,2024\n"
        "+Journal B,1.5,2024\n"
        "-Journal C,1.5,2024\n"
    ).encode()
    parsed = journals.parse_journals_table(payload, "injection.csv")
    names = {row["journal_name"] for row in parsed["rows"]}
    # 四个注入样式串全部按字面进入期刊名（数值列仍正常解析），无任何求值/剥离产物
    assert names == {"=cmd|'/C calc'!A0", "@SUM(Journal)", "+Journal B", "-Journal C"}
    assert {row["jif"] for row in parsed["rows"]} == {1.0, 1.5, 2.0}


# ---------------------------------------------------------------- 原始行留存契约


class _AsyncSessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, *_args):
        if exc_type is None:
            await self.db.commit()
        else:
            await self.db.rollback()
        return False


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        for model in (
            LiteraturePaper,
            LiteratureCitation,
            LiteratureReferenceBatch,
            LiteratureReferenceMember,
            LiteratureReferenceRecord,
        ):
            await conn.run_sync(model.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        monkeypatch.setattr(
            reference_import.pg_manager,
            "get_async_session_context",
            lambda: _AsyncSessionContext(session),
        )
        # 原始行分支只在有用户上下文时启用；rids 模拟"用户可访问来源集合"
        # （导入新建的文献并入自己的可访问范围，与真实 access 语义一致）
        scope_rids: set[str] = set()
        monkeypatch.setattr(
            reference_import,
            "current_access",
            lambda: SimpleNamespace(tenant_id=1, user=SimpleNamespace(uid="safety-user"), rids=scope_rids),
        )
        # 原件落 MinIO 不在安全验收范围内：记录调用即可
        stored: list[tuple[int, str]] = []

        async def fake_store_source(tenant_id: int, batch_id: str, data: bytes) -> str:
            stored.append((tenant_id, batch_id))
            return f"tenants/{tenant_id}/literature/reference-sources/{batch_id}/source"

        monkeypatch.setattr(reference_import, "_store_source", fake_store_source)
        yield SimpleNamespace(session=session, stored=stored, scope_rids=scope_rids)
    await engine.dispose()


async def test_raw_rows_retained_and_reupload_idempotent(env):
    wos = (REFERENCE_FIXTURES / "wos_sample.txt").read_bytes()

    first = await reference_import.import_references(1, wos, source_name="wos_sample.txt")
    assert first["batch_id"] and first["source_records"] >= 1
    papers_first = first["papers_written"] + first["papers_skipped"]

    session = env.session
    rows = (
        (await session.execute(select(LiteratureReferenceRecord).order_by(LiteratureReferenceRecord.record_no)))
        .scalars()
        .all()
    )
    assert len(rows) == first["source_records"]
    # 原始行保留来源字段与身份关联：payload 含原始标题，rid 指向去重文献
    assert all(row.payload_json.get("title") for row in rows)
    assert all(row.rid for row in rows)
    known_rids = {paper.rid for paper in (await session.execute(select(LiteraturePaper))).scalars()}
    assert {row.rid for row in rows} <= known_rids
    # 重传前把已建文献并入可访问范围（真实语义：自己的导入结果自己可见）
    env.scope_rids.update(known_rids)
    batch = await session.get(LiteratureReferenceBatch, first["batch_id"])
    assert batch is not None and batch.created_by == "safety-user"
    assert batch.source_object and env.stored  # 原件对象已登记（打桩路径）

    # 同文件重传 → 幂等：同一批次、不新建文献、原始行不重复、可补齐缺失行
    second = await reference_import.import_references(1, wos, source_name="wos_sample.txt")
    assert second["batch_id"] == first["batch_id"]
    assert second["papers_written"] == 0
    assert second["papers_skipped"] == papers_first
    assert (await session.execute(select(func.count()).select_from(LiteratureReferenceRecord))).scalar_one() == first[
        "source_records"
    ]

    # 历史批次缺行（模拟旧数据）→ 重传按 receipt 补齐 occurrences
    await session.execute(delete(LiteratureReferenceRecord))
    await session.commit()
    backfill = await reference_import.import_references(1, wos, source_name="wos_sample.txt")
    assert backfill["batch_id"] == first["batch_id"]
    assert (await session.execute(select(func.count()).select_from(LiteratureReferenceRecord))).scalar_one() == first[
        "source_records"
    ]
    # 补齐不得新建文献
    assert backfill["papers_written"] == 0
