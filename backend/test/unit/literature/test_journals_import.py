"""期刊指标导入（PaperAgent §12 ④）契约测试。

钉死：预览只回摘要不 dump 全文；导入幂等且租户权威来自 PrincipalContext；
分区形态判定（Qn → JCR，n区/中文数字 → 中科院）；限额失败关闭；
关联写回 IF/分区后，分区与最低 IF 筛选可用。
"""

from __future__ import annotations

import io
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import server.routers.literature_router as router_module
from server.routers.literature_router import literature
from yuxi.literature import journals, library_pipeline, readops
from yuxi.literature.access import LiteratureAccess, _access
from yuxi.storage.postgres.models_knowledge import LiteratureJournalMetric, LiteraturePaper

pytestmark = pytest.mark.unit

TENANT = 19
CSV = "期刊名,JIF,分区,年份\nStarch & Starke,3.2,Q2,2024\nOther Journal,1.1,Q4,2024\n"


def _xlsx(sheets: list[tuple[str, list[list]]]) -> bytes:
    """构造 xlsx fixture（不依赖真实文件）。"""
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


# --- 解析与预览（纯函数） --------------------------------------------------------


def test_csv_rows_are_normalized_and_quartile_detected():
    parsed = journals.parse_journals_table(CSV.encode("utf-8"), "jcr.csv")
    assert [row["journal_key"] for row in parsed["rows"]] == ["starch and starke", "other journal"]
    assert parsed["rows"][0]["quartile"] == "Q2" and parsed["rows"][0]["jif"] == 3.2
    assert parsed["rows"][0]["cas_zone"] == "" and parsed["rows"][0]["source"] == "jcr"
    assert parsed["skipped"] == {"no_journal": 0, "no_year": 0, "no_value": 0}


def test_xlsx_cas_sheet_maps_zone_and_sheet_year_is_default():
    parsed = journals.parse_journals_table(
        _xlsx([("2024中科院分区", [["期刊名", "分区"], ["Starch & Starke", "1区"], ["Other Journal", "四区"]])]),
        "cas.xlsx",
    )
    assert [row["cas_zone"] for row in parsed["rows"]] == ["1区", "4区"]
    assert {row["year"] for row in parsed["rows"]} == {2024}  # sheet 名年份兜底
    assert {row["source"] for row in parsed["rows"]} == {"cas"}
    assert parsed["sheets"][0]["columns"] == ["journal", "quartile"]  # 列名只判「是分区列」


def test_unrecognized_columns_are_ignored_not_guessed():
    parsed = journals.parse_journals_table("期刊名,some column,JIF,年份\nA Journal,xyz,2.0,2023\n".encode(), "x.csv")
    assert parsed["sheets"][0]["columns"] == ["jif", "journal", "year"]
    assert parsed["rows"][0]["jif"] == 2.0


def test_rows_without_year_or_metrics_are_counted_not_silently_dropped():
    parsed = journals.parse_journals_table(
        "期刊名,JIF,年份\nA Journal,2.0,2024\nB Journal,,2024\n,3.0,2024\nC Journal,1.0,\n".encode(), "x.csv"
    )
    # 只有 A Journal 完整；其余按原因计数，不静默丢弃也不半猜
    assert [row["journal_name"] for row in parsed["rows"]] == ["A Journal"]
    assert parsed["skipped"] == {"no_journal": 1, "no_year": 1, "no_value": 1}


def test_preview_returns_summary_and_two_samples_only():
    payload = ("期刊名,JIF,分区,年份\n" + "".join(f"Journal {i},1.0,Q1,2024\n" for i in range(50))).encode()
    preview = journals.preview_journals_table(payload, "big.csv")
    assert preview["row_count"] == 50
    assert len(preview["sample"]) == 2
    assert preview["sheets"][0]["row_count"] == 50
    assert "rows" not in preview  # 预览绝不 dump 全文


def test_format_is_detected_by_content_not_extension():
    # 扩展名写错（xlsx 当 csv 上传）也必须能解析，而不是回 500 或按二进制乱读
    payload = _xlsx([("JCR", [["期刊名", "JIF", "分区", "年份"], ["Journal A", 1.5, "Q1", 2024]])])
    assert journals.parse_journals_table(payload, "jcr.csv")["rows"][0]["quartile"] == "Q1"
    with pytest.raises(ValueError, match="解析失败"):
        journals.parse_journals_table(b"PK\x03\x04not-a-real-zip", "fake.xlsx")


def test_limits_fail_closed(monkeypatch):
    with pytest.raises(ValueError, match="单批大小上限"):
        journals.preview_journals_table(b"x" * (journals.MAX_JOURNAL_FILE_BYTES + 1), "big.csv")
    with pytest.raises(ValueError, match="表为空"):
        journals.preview_journals_table(b"", "empty.csv")
    monkeypatch.setattr(journals, "MAX_JOURNAL_ROWS", 2)
    with pytest.raises(ValueError, match="行数超过单批上限"):
        journals.preview_journals_table(("期刊名,JIF,年份\n" + "J,1,2024\n" * 5).encode("utf-8"), "x.csv")


# --- 落库：幂等导入 + 关联 + 筛选 -------------------------------------------------


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LiteraturePaper.__table__.create)
        await connection.run_sync(LiteratureJournalMetric.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:

        @asynccontextmanager
        async def session_context():
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

        monkeypatch.setattr(journals.pg_manager, "get_async_session_context", session_context)
        db.add(LiteraturePaper(id=1, tenant_id=TENANT, rid="doi-x", kind="paper", journal="Starch & Starke"))
        db.add(LiteraturePaper(id=2, tenant_id=TENANT, rid="doi-y", kind="paper", journal="Other Journal"))
        await db.commit()
        scope = LiteratureAccess(
            TENANT, frozenset({"doi-x", "doi-y"}), frozenset({1, 2}), {}, {}, SimpleNamespace(uid="u1")
        )
        token = _access.set(scope)
        try:
            yield db
        finally:
            _access.reset(token)


async def test_import_is_idempotent_and_feeds_attach(env):
    db = env
    first = await journals.import_journals_table(TENANT, CSV.encode("utf-8"), "jcr.csv")
    assert (first["created"], first["updated"]) == (2, 0)
    again = await journals.import_journals_table(TENANT, CSV.encode("utf-8"), "jcr.csv")
    assert (again["created"], again["updated"]) == (0, 0)  # 同输入幂等
    changed = await journals.import_journals_table(
        TENANT, CSV.replace("Starch & Starke,3.2", "Starch & Starke,4.5").encode("utf-8"), "jcr.csv"
    )
    assert changed["updated"] == 1

    attached = await library_pipeline.attach_journal_metrics(TENANT)
    assert attached["matched"] == 2
    paper = await db.get(LiteraturePaper, 1)
    assert (paper.impact_factor, paper.quartile) == (4.5, "Q2")
    status = await journals.journal_metrics_status(TENANT)
    assert status["mapping_count"] == 2 and status["journal_count"] == 2
    assert status["year_min"] == status["year_max"] == 2024 and status["if_covered_count"] == 2


async def test_import_rejects_unbound_tenant_and_unrecognized_tables(env):
    with pytest.raises(ValueError, match="必须绑定租户"):
        await journals.import_journals_table(0, CSV.encode("utf-8"))
    with pytest.raises(ValueError, match="未识别到任何期刊指标行"):
        await journals.import_journals_table(TENANT, b"hello,world\n")


async def test_list_papers_filters_by_quartile_and_min_if(env):
    assert (await journals.import_journals_table(TENANT, CSV.encode("utf-8")))["created"] == 2
    await library_pipeline.attach_journal_metrics(TENANT)
    cases = (("Q2", None), ("Q4", None), ("none", None), ("", 2.0))
    pages = [await readops.list_papers(TENANT, kind="paper", quartile=q, min_if=v) for q, v in cases]
    assert [len(page["items"]) for page in pages] == [1, 1, 0, 1]
    assert pages[0]["items"][0]["rid"] == "doi-x" and pages[0]["items"][0]["quartile"] == "Q2"
    assert pages[3]["items"][0]["rid"] == "doi-x"
    assert (await readops.list_papers(TENANT, kind="paper", quartile="none,Q4"))["total"] == 1
    with pytest.raises(ValueError, match="Q1–Q4"):
        await readops.list_papers(TENANT, kind="paper", quartile="Q9")


# --- 路由契约 -------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(literature, prefix="/api")
    user = type("U", (), {"uid": "u1"})()
    app.dependency_overrides[router_module.get_required_user] = lambda: user
    app.dependency_overrides[router_module.get_admin_user] = lambda: user
    app.dependency_overrides[router_module.authorize_literature_request] = lambda: None

    async def _tenant(_user):
        return TENANT

    monkeypatch.setattr(router_module, "_current_tenant", _tenant)
    return TestClient(app, raise_server_exceptions=False)


def test_preview_endpoint_returns_summary_only(client, monkeypatch):
    seen = {}

    def fake_preview(data, filename):
        seen["filename"] = filename
        return {"row_count": 2, "sample": [], "sheets": [], "skipped": {}, "sources": ["jcr"]}

    monkeypatch.setattr(journals, "preview_journals_table", fake_preview)
    response = client.post("/api/literature/journals/preview", files={"file": ("jcr.csv", CSV.encode("utf-8"))})
    assert response.status_code == 200
    assert response.json()["row_count"] == 2 and seen["filename"] == "jcr.csv"


def test_preview_endpoint_rejects_oversized_upload_before_parsing(client):
    payload = b"x" * (journals.MAX_JOURNAL_FILE_BYTES + 1)
    response = client.post("/api/literature/journals/preview", files={"file": ("big.csv", payload)})
    assert response.status_code == 413


def test_import_endpoint_ignores_query_tenant(client, monkeypatch):
    seen = {}

    async def fake_import(tenant_id, data, filename=""):
        seen["tenant"] = tenant_id
        return {"created": 2, "updated": 0, "rows": 2, "skipped": {}, "sheets": []}

    monkeypatch.setattr(journals, "import_journals_table", fake_import)
    response = client.post(
        "/api/literature/journals/import?tenant_id=999", files={"file": ("jcr.csv", CSV.encode("utf-8"))}
    )
    assert response.status_code == 201
    assert seen["tenant"] == TENANT and response.json()["created"] == 2


def test_attach_metrics_endpoint_calls_pipeline(client, monkeypatch):
    seen = {}

    async def fake_attach(tenant_id):
        seen["tenant"] = tenant_id
        return {"matched": 1, "unmatched": 0, "protected": 0}

    monkeypatch.setattr(library_pipeline, "attach_journal_metrics", fake_attach)
    response = client.post("/api/literature/library/journal-metrics")
    assert response.status_code == 200 and seen["tenant"] == TENANT
