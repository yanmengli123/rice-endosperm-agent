"""文献清单导入测试（G3）：编码链、嗅探矩阵、四格式解析、三查链幂等、
引用边、CNKI 键身份、预算失败关闭与 ACL 冲突。

fixtures（backend/test/fixtures/reference_lists/）按四家官网真实导出语法构造，
同时是字段映射表的回归锁：万方/知网 EndNote 变体标签以官方样本校准为准。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.literature import reference_import
from yuxi.storage.postgres.models_knowledge import LiteratureCitation, LiteraturePaper

# asyncio_mode=auto（pyproject.toml）：async 测试自动收集，无需显式标记；
# 显式 asyncio 标记会把同步纯函数一并打上并触发 PytestWarning。
pytestmark = [pytest.mark.unit]

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "reference_lists"


# ---------------------------------------------------------------- 纯函数：编码与嗅探


def test_decode_chain():
    text, codec = reference_import.decode_reference_file("水稻".encode())
    assert (text, codec) == ("水稻", "utf-8")
    text, codec = reference_import.decode_reference_file(b"\xef\xbb\xbf" + "标题".encode())
    assert (text, codec) == ("标题", "utf-8-sig")
    text, codec = reference_import.decode_reference_file("中文文献清单".encode("gb18030"))
    assert (text, codec) == ("中文文献清单", "gb18030")
    text, codec = reference_import.decode_reference_file("知网导出".encode("utf-16"))
    assert (text, codec) == ("知网导出", "utf-16")
    with pytest.raises(ValueError, match="编码"):
        reference_import.decode_reference_file(b"\xff")
    with pytest.raises(ValueError, match="BOM"):
        reference_import.decode_reference_file(b"\xff\xfe\x81")


def test_detect_format_matrix():
    assert reference_import.detect_format((FIXTURES / "pubmed_sample.nbib").read_text("utf-8")) == "pubmed-nbib"
    assert reference_import.detect_format((FIXTURES / "wos_sample.txt").read_text("utf-8")) == "wos-txt"
    # Thomson 时代的历史文件头写法同样识别
    assert reference_import.detect_format("FN Thomson Reuters Web of Science\nVR 1.0\nPT J\nTI x\nER") == "wos-txt"
    assert reference_import.detect_format((FIXTURES / "ris_sample.ris").read_text("utf-8")) == "ris"
    assert reference_import.detect_format((FIXTURES / "endnote_sample.txt").read_text("utf-8")) == "endnote"
    assert reference_import.detect_format("@article{a,\n  title = {x}}\n") == "bibtex"
    with pytest.raises(ValueError, match="无法识别"):
        reference_import.detect_format("普通文本一段\n没有任何标签行\n")


# ---------------------------------------------------------------- 纯函数：四格式解析


def test_parse_pubmed_nbib():
    records = reference_import.parse_references((FIXTURES / "pubmed_sample.nbib").read_text("utf-8"), "pubmed-nbib")
    assert len(records) == 2
    first, second = records
    assert first["title"].startswith("Grain number determination")
    assert first["authors"] == ["Zhang, Wei"]  # FAU 优先于 AU 短格式
    assert first["journal"] == "Rice Science"  # JT 全称优先于 TA 缩写
    assert first["doi"] == "10.1016/j.rsci.2024.07.001"  # AID 剥 [doi] 后缀
    assert first["pmid"] == "38991116"
    assert first["year"] == 2024
    assert first["language"] == "en"
    assert "Oryza" in first["keywords"]  # MeSH 主词（剥 /*genetics 限定符）
    assert "grain number" in first["keywords"]  # OT 作者关键词
    assert first["issn"] == "1672-6308"
    assert second["doi"] == ""  # 仅有 [pii] AID 行 → 无 DOI
    assert second["title"] == "水稻幼苗叶绿体发育研究"
    assert second["language"] == "zh"
    assert second["pmid"] == "12345678"
    assert second["affiliations"] == ["College of Life Sciences, Wuhan University, Wuhan, China."]


def test_parse_wos_txt():
    records = reference_import.parse_references((FIXTURES / "wos_sample.txt").read_text("utf-8"), "wos-txt")
    assert len(records) == 2  # FN/VR 文件头不产生记录
    first, second = records
    assert first["authors"] == ["Zhang, Wei", "Li, Ming"]  # AF 续行 = 多作者拆分
    assert first["journal"] == "RICE SCIENCE"
    assert first["year"] == 2024
    assert first["doi"] == "10.1016/j.rsci.2024.07.001"
    assert first["language"] == "en"  # "English" 归一
    assert first["kind"] == "paper"
    assert first["wos_categories"] == ["Plant Sciences", "Agronomy"]
    assert first["research_areas"] == ["Plant Sciences", "Agriculture"]
    assert first["affiliations"] == [
        "Natl Rice Res Inst, Hangzhou, Peoples R China",
        "Natl Rice Res Inst, State Key Lab Rice Biol, Hangzhou, Peoples R China",
    ]  # C1 方括号作者头剥除
    assert first["cited_dois"] == [
        "10.1126/science.aaw1234",
        "10.1038/s41586-020-2452-z",
        "10.1073/pnas.1718920115",
    ]
    assert second["year"] == 2019
    assert second["cited_dois"] == ["10.1038/s41586-020-2452-z"]


def test_parse_ris():
    records = reference_import.parse_references((FIXTURES / "ris_sample.ris").read_text("utf-8"), "ris")
    assert len(records) == 2
    first, second = records
    assert first["doi"] == "10.1016/j.rsci.2024.07.001"
    assert first["keywords"] == ["grain number", "panicle architecture"]  # 重复 KW 行累积
    assert "molecular regulators of panicle architecture." in first["abstract"]  # 续行折叠为空格
    assert first["language"] == "en"  # "ENG" 归一
    assert first["kind"] == "paper"  # JOUR
    assert second["title"] == "水稻幼苗叶绿体发育研究"
    assert second["authors"] == ["李明"]
    assert second["language"] == "zh"  # "CHI" 归一


def test_parse_endnote():
    records = reference_import.parse_references((FIXTURES / "endnote_sample.txt").read_text("utf-8"), "endnote")
    assert len(records) == 2
    first, second = records
    assert first["title"] == "稻穗粒数决定的分子调控研究"
    assert first["authors"] == ["张伟", "李明"]  # 重复 %A 行累积
    assert first["keywords"] == ["穗粒数", "水稻", "分子调控"]  # 分号拆分
    assert first["affiliations"] == ["中国农业科学院水稻研究所"]  # %+ 机构标签
    assert first["language"] == "zh"
    assert first["cnki_id"] == "DBZY202305001"  # %U 详情页 filename 提取
    assert first["doi"] == "10.16819/j.cnki.rice.2023.05.001"  # %R
    assert first["year"] == 2023
    assert second["cnki_id"] == ""
    assert second["year"] == 2019


def test_crlf_line_endings_equivalent():
    text = (FIXTURES / "wos_sample.txt").read_text("utf-8")
    lf = reference_import.parse_references(text, "wos-txt")
    crlf = reference_import.parse_references(text.replace("\n", "\r\n"), "wos-txt")
    assert lf == crlf  # Windows 导出 CRLF 不破坏 3 空格续行缩进判断


def test_wos_tab_delimited_quotes_lists_and_citations():
    text = (
        "PT\tAU\tAF\tTI\tSO\tPY\tDI\tDE\tC1\tCR\n"
        'J\tZhang W; Li M\tZhang, Wei; Li, Ming\t"Rice\tgrain\nquality"\t'
        "Rice Science\t2024\t10.1000/rice\tRice; Grain\t"
        '"[Zhang, Wei; Wang, Hong] Rice Inst, Wuhan, China; [Li, Ming] Rice Univ, USA"\t'
        '"Smith, DOI 10.1000/a; Wang, DOI 10.1000/b"\n'
    )
    assert reference_import.detect_format(text) == "wos-txt"
    [record] = reference_import.parse_references(text, "wos-txt")
    assert record["title"] == "Rice grain quality"
    assert record["authors"] == ["Zhang, Wei", "Li, Ming"]
    assert record["affiliations"] == ["Rice Inst, Wuhan, China", "Rice Univ, USA"]
    assert record["cited_dois"] == ["10.1000/a", "10.1000/b"]
    with pytest.raises(ValueError, match="列数"):
        reference_import.parse_references("PT\tTI\tPY\nJ\tRice\n", "wos-txt")


def test_pubmed_wrapped_affiliation_is_one_address_and_lid_doi():
    [record] = reference_import.parse_references(
        "PMID- 12345\nTI  - Rice\nLID - 10.1000/rice [doi]\nAD  - Rice Institute,\n      Wuhan, China.\n", "pubmed-nbib"
    )
    assert record["doi"] == "10.1000/rice"
    assert record["affiliations"] == ["Rice Institute, Wuhan, China."]


def test_endnote_database_provider_is_not_an_affiliation():
    [record] = reference_import.parse_references("%0 Journal Article\n%T Rice\n%W CNKI\n%+ Rice Institute\n", "endnote")
    assert record["affiliations"] == ["Rice Institute"]


# ---------------------------------------------------------------- 入库（SQLite 内存）


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
        for model in (LiteraturePaper, LiteratureCitation):
            await conn.run_sync(model.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        monkeypatch.setattr(
            reference_import.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(session)
        )
        yield session
    await engine.dispose()


async def test_import_and_cross_format_dedup(env):
    nbib = (FIXTURES / "pubmed_sample.nbib").read_bytes()
    report = await reference_import.import_references(1, nbib, source_name="pubmed.nbib")
    assert report["format_detected"] == "pubmed-nbib"
    assert report["encoding"] == "utf-8"
    assert report["papers_written"] == 2

    # 同文件重导 → 全部 skip（DOI 路径 + 指纹路径）
    report = await reference_import.import_references(1, nbib)
    assert report["papers_written"] == 0 and report["papers_skipped"] == 2

    # 跨格式 RIS：第一条 DOI 重叠、第二条题名+年份指纹重叠 → 全部命中去重
    ris = (FIXTURES / "ris_sample.ris").read_bytes()
    report = await reference_import.import_references(1, ris)
    assert report["papers_written"] == 0 and report["papers_skipped"] == 2

    papers = (await env.execute(select(LiteraturePaper).order_by(LiteraturePaper.id))).scalars().all()
    assert len(papers) == 2
    assert "10.1016/j.rsci.2024.07.001" in {paper.doi for paper in papers}
    zh = next(paper for paper in papers if paper.language == "zh")
    assert zh.rid.startswith("nd-")  # 无 DOI → nd- 指纹（与 bib_import 口径一致）
    assert zh.pmid == "12345678"
    assert zh.source_main == "pubmed-nbib"


async def test_import_wos_writes_citation_edges(env):
    wos = (FIXTURES / "wos_sample.txt").read_bytes()
    report = await reference_import.import_references(1, wos)
    assert report["papers_written"] == 2
    assert report["citations_written"] == 4  # 3 + 1（两条记录 CR 中的 DOI 边）

    # 重导：文献全 skip，引用边不重复计数（预取判重）
    report = await reference_import.import_references(1, wos)
    assert report["papers_skipped"] == 2 and report["citations_written"] == 0
    edges = (await env.execute(select(LiteratureCitation))).scalars().all()
    assert len(edges) == 4


async def test_import_endnote_identity_paths(env):
    report = await reference_import.import_references(1, (FIXTURES / "endnote_sample.txt").read_bytes())
    assert report["papers_written"] == 2
    rids = {paper.rid for paper in (await env.execute(select(LiteraturePaper))).scalars().all()}
    assert any(rid.startswith("doi-") for rid in rids)  # %R DOI 优先
    assert any(rid.startswith("nd-") for rid in rids)  # 无标识 → 指纹

    # 无 DOI 但有 %U CNKI 键 → cnki- RID（make_rid 现成能力）
    cnki_text = (
        "%0 Journal Article\n%T 机构主导身份示例\n%D 2020\n"
        "%U https://kns.cnki.net/kcms2/article/detail?dbcode=CJFD&filename=ZZSL202001003\n"
    )
    report = await reference_import.import_references(1, cnki_text.encode("utf-8"))
    assert report["papers_written"] == 1
    rids = {paper.rid for paper in (await env.execute(select(LiteraturePaper))).scalars().all()}
    assert "cnki-ZZSL202001003" in rids


async def test_import_encoding_variants(env):
    text = (FIXTURES / "endnote_sample.txt").read_text("utf-8")
    report = await reference_import.import_references(1, text.encode("gb18030"))
    assert report["encoding"] == "gb18030" and report["papers_written"] == 2
    # 另一租户独立计数（UTF-16 BOM 路径）
    report = await reference_import.import_references(2, text.encode("utf-16"))
    assert report["encoding"] == "utf-16" and report["papers_written"] == 2
    tenant_ids = set((await env.execute(select(LiteraturePaper.tenant_id))).scalars().all())
    assert tenant_ids == {1, 2}
    per_tenant = (
        await env.execute(select(LiteraturePaper.tenant_id, LiteraturePaper.id).order_by(LiteraturePaper.id))
    ).all()
    counts: dict[int, int] = {}
    for tenant_id, _paper_id in per_tenant:
        counts[tenant_id] = counts.get(tenant_id, 0) + 1
    assert counts == {1: 2, 2: 2}  # 租户互不可见、各算各的


async def test_budget_and_invalid_inputs(env, monkeypatch):
    monkeypatch.setattr(reference_import, "MAX_REFERENCE_RECORDS", 1)
    with pytest.raises(ValueError, match="安全预算"):
        await reference_import.import_references(1, (FIXTURES / "pubmed_sample.nbib").read_bytes())
    monkeypatch.setattr(reference_import, "MAX_REFERENCE_RECORDS", 50_000)
    # 无任何标签的文本 → 嗅探失败
    with pytest.raises(ValueError, match="无法识别"):
        await reference_import.import_references(1, "普通文本\n没有标签行\n".encode())
    # 有格式但记录全无身份字段（仅 %0 类型行）
    with pytest.raises(ValueError, match="有效文献记录"):
        await reference_import.import_references(1, b"%0 Journal Article\n\n")


async def test_acl_conflict_on_skip(env, monkeypatch):
    await reference_import.import_references(1, (FIXTURES / "pubmed_sample.nbib").read_bytes())
    # 跳过的记录若落在不可访问范围（scope.rids 不含）→ 与 bib_import 同口径拒绝
    scope = SimpleNamespace(rids=frozenset({"nd-untitled"}), tenant_id=1)
    monkeypatch.setattr(reference_import, "current_access", lambda: scope)
    with pytest.raises(ValueError, match="不可访问"):
        await reference_import.import_references(1, (FIXTURES / "pubmed_sample.nbib").read_bytes())


async def test_pmid_only_records_do_not_collapse(env):
    report = await reference_import.import_references(1, b"PMID- 12345\n\nPMID- 67890\n")
    assert report["papers_written"] == 2
    assert len(set((await env.execute(select(LiteraturePaper.rid))).scalars())) == 2


async def test_same_upload_duplicates_are_not_acl_conflicts(env, monkeypatch):
    monkeypatch.setattr(reference_import, "current_access", lambda: SimpleNamespace(tenant_id=1, rids=frozenset()))
    report = await reference_import.import_references(1, b"TY  - JOUR\nTI  - Rice\nDO  - 10.1000/rice\nER  -\n" * 2)
    assert (report["papers_written"], report["papers_skipped"]) == (1, 1)


async def test_pmid_alias_uses_existing_authorized_rid_for_citations(env, monkeypatch):
    env.add(LiteraturePaper(tenant_id=1, rid="nd-existing", pmid="12345", title="Rice", meta_locked=True))
    await env.commit()
    monkeypatch.setattr(
        reference_import, "current_access", lambda: SimpleNamespace(tenant_id=1, rids=frozenset({"nd-existing"}))
    )
    report = await reference_import.import_references(
        1, b"FN Clarivate\nVR 1.0\nPT J\nTI Rice\nPM 12345\nDI 10.1000/rice\nCR Smith, DOI 10.1000/a\nER\n"
    )
    assert report["papers_written"] == 0 and report["citations_written"] == 1
    [edge] = (await env.execute(select(LiteratureCitation))).scalars().all()
    assert edge.citing_rid == "nd-existing"
    [paper] = (await env.execute(select(LiteraturePaper))).scalars().all()
    assert paper.doi is None and paper.meta_locked is True


async def test_conflicting_identity_aliases_roll_back(env):
    env.add_all(
        [
            LiteraturePaper(tenant_id=1, rid="doi-10.1000@a", doi="10.1000/a"),
            LiteraturePaper(tenant_id=1, rid="nd-b", pmid="12345"),
        ]
    )
    await env.commit()
    with pytest.raises(ValueError, match="身份冲突"):
        await reference_import.import_references(
            1, b"PMID- 99999\nTI  - New\n\nPMID- 12345\nTI  - Conflict\nAID - 10.1000/a [doi]\n"
        )
    assert len((await env.execute(select(LiteraturePaper))).scalars().all()) == 2


async def test_parallel_import_fails_before_reading_or_writing(env, monkeypatch):
    monkeypatch.setattr(env, "get_bind", lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))

    async def busy(_statement):
        return False

    monkeypatch.setattr(env, "scalar", busy)
    with pytest.raises(ValueError, match="正在导入"):
        await reference_import.import_references(1, b"PMID- 12345\nTI  - Rice\n")
    assert not (await env.execute(select(LiteraturePaper))).scalars().all()


async def test_new_import_entry_preserves_legacy_bibtex_channel(monkeypatch):
    async def legacy(tenant, text, *, source_name):
        assert tenant == 7 and source_name == "references.bib"
        assert text.startswith("@article")
        return {"papers_written": 1, "papers_skipped": 0, "records": 1}

    monkeypatch.setattr(reference_import.bib_import, "import_bib", legacy)
    report = await reference_import.import_references(7, b"@article{rice, title={Rice}}", source_name="references.bib")
    assert report["papers_written"] == 1 and report["format_detected"] == "bibtex" and report["encoding"] == "utf-8"
