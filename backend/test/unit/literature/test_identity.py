"""文献域身份归一契约测试（ADR-0010）。

身份语义移植自 PaperAgent ``paperkb/doi.py`` + ``paperkb/layout.py``
（paperagent 1.4.0）。这些测试同时是**导入兼容契约**：同一 DOI 必须得到
与 PaperAgent 字节级一致的 RID/目录名，否则 P3 导入器会把这些文献落到
平行目录（上游 attachments.library_candidates 记载的事故形态）。
"""

from __future__ import annotations

import dataclasses
import re

import pytest

from yuxi.literature.contracts import (
    READER_RENDER_VERSION,
    ReaderRenderResult,
    ResourceId,
)
from yuxi.literature.identity.kinds import KIND_PREFIX
from yuxi.literature.identity.rid import (
    doi_to_dirname,
    is_doi,
    kind_from_rid,
    library_candidates,
    make_rid,
    normalize_doi,
)

pytestmark = pytest.mark.unit


# --- DOI 归一（T7） -------------------------------------------------------------


def test_normalize_doi_strips_url_prefix_and_trailing_dot_and_preserves_case():
    # URL 前缀/尾点归一（Yuxi 超集增强；上游对 URL 形态直接判非 DOI）
    assert normalize_doi("https://doi.org/10.1002/adma.202407106") == "10.1002/adma.202407106"
    assert normalize_doi("http://dx.doi.org/10.1038/ncomms8258") == "10.1038/ncomms8258"
    assert normalize_doi("doi.org/10.1038/ncomms8258") == "10.1038/ncomms8258"
    assert normalize_doi("10.1038/ncomms8258.") == "10.1038/ncomms8258"
    # 大小写保留（PaperAgent 为 WOS 导出保真刻意不做小写归一；
    # casefold 会破坏导入兼容的 RID 字节级一致——ADR-0010 已知限制 §5）
    assert normalize_doi(" 10.1002/Adma.202407106 ") == "10.1002/Adma.202407106"
    assert normalize_doi(' "10.1002/adma.202407106" ') == "10.1002/adma.202407106"
    assert normalize_doi(None) == ""
    assert normalize_doi("") == ""


def test_doi_dirname_is_injective_and_matches_paperagent_layout():
    assert doi_to_dirname("10.1002/adma.202407106") == "10.1002_adma.202407106"
    assert doi_to_dirname("10.1002/adma:202407106") == "10.1002_adma~202407106"
    # 字面 "_" 与 "/" 单射区分（上游 2026-09-11 修复的串档问题：不同 DOI 落同一目录）
    assert doi_to_dirname("10.1002/a_b") != doi_to_dirname("10.1002/a/b")
    # 不含分隔符的保留键原样保留（_global/_qa 等不可被改写）
    assert doi_to_dirname("_global") == "_global"
    # 含不安全字符 → 追加 --<md5前6> 消歧后缀（幂等、确定性）
    dirty = doi_to_dirname("10.1002/adma (special) 2024")
    assert re.search(r"--[0-9a-f]{6}$", dirty) is not None
    assert doi_to_dirname("10.1002/adma (special) 2024") == dirty
    # DOI 形态唯一判据：拒绝"文件名当 DOI"（上游幽灵文献修复）
    assert is_doi("10.1002/adma.202407106") is True
    assert is_doi("JMADE-D-26-04277_R1_reviewer") is False
    assert is_doi("https://doi.org/10.1002/adma.202407106") is True  # Yuxi 超集：URL 形态可救回


# --- RID 生成（T8） -------------------------------------------------------------


def test_make_rid_uses_std_prefix_for_standard_kind():
    # PaperAgent layout.py:58-63 教训：standard 的前缀是 std__ 而非 standard__
    rid = make_rid("standard", isbn="GB/T 7714")
    assert rid == "std__isbn-GB-T-7714"
    assert not rid.startswith("standard__")
    assert kind_from_rid(rid) == "standard"


def test_make_rid_priority_chain_and_paper_default():
    assert make_rid("paper", doi="10.1002/adma.202407106") == "doi-10.1002_adma.202407106"
    assert make_rid("book", isbn="9783527345678") == "book__isbn-9783527345678"
    # DOI 优先于其余外部标识
    assert make_rid("book", doi="10.1002/adma.202407106", isbn="9783527345678") == "book__doi-10.1002_adma.202407106"
    # 子资源：kind 命中前缀 + parent_rid → <前缀><父RID>（依附资料挂父资源，物理隔离）
    assert make_rid("si", parent_rid="doi-10.1002_adma.202407106") == "si__doi-10.1002_adma.202407106"
    # 无任何标识 → nd- 内容指纹身份（同文件重复导入天然去重）；指纹小写化
    assert make_rid(fingerprint="ABC123DEF456") == "nd-abc123def456"
    assert make_rid() == "nd-untitled"


# --- RID 反推（T9） -------------------------------------------------------------


def test_kind_from_rid_prefers_longest_prefix_and_defaults_to_paper():
    assert kind_from_rid("thesis__cnki-12345") == "thesis"
    assert kind_from_rid("chapter__isbn-9783527345678-ch03") == "chapter"
    assert kind_from_rid("std__xyz") == "standard"
    assert kind_from_rid("patent__CN-101") == "patent"
    # 无类型前缀：doi- 与 nd- 都按论文处理（上游默认）
    assert kind_from_rid("doi-10.1002_adma.202407106") == "paper"
    assert kind_from_rid("nd-abc123def456") == "paper"
    assert kind_from_rid("") == "paper"
    # 前缀表单一来源：全部前缀可往返，不许业务代码裸写前缀字符串
    for kind, prefix in KIND_PREFIX.items():
        if prefix:
            assert kind_from_rid(prefix + "x") == kind


# --- 三键归一（T10） ------------------------------------------------------------


def test_library_candidates_collapse_rid_doi_and_dirname():
    rid = "doi-10.1002_adma.202407106"
    doi = "10.1002/adma.202407106"
    # 三种键写法归一到同一候选目录名集合（顺序即优先级、去重）
    assert library_candidates(rid, doi) == (rid, "10.1002_adma.202407106")
    assert library_candidates(rid) == (rid, "10.1002_adma.202407106")
    # 只给裸 DOI：候选 = 裸 DOI 原样 + 规范目录名
    assert library_candidates(doi) == (doi, "10.1002_adma.202407106")
    assert library_candidates("") == ()


# --- 领域契约类型（T11） ---------------------------------------------------------


def test_resource_id_requires_non_empty_rid():
    with pytest.raises(ValueError, match="不能为空"):
        ResourceId(rid="", kind="paper")
    resource = ResourceId(rid="doi-10.1002_adma.202407106", kind="paper", doi="10.1002/adma.202407106")
    assert resource.kind == "paper"


def test_reader_render_result_cannot_carry_evidence_fields():
    # 类型层防御（与 WikiNavigationHit 同构）：派生物在构造层就无法携带证据字段，
    # "顺手塞进证据列表"直接构造不出来——ADR-0010 决议 2
    field_names = {field.name for field in dataclasses.fields(ReaderRenderResult)}
    for forbidden in ("content", "text", "quote", "anchor_id", "evidence_id", "metadata"):
        assert forbidden not in field_names


def test_reader_render_result_requires_kbasset_logical_uri():
    render = ReaderRenderResult(
        parse_revision_id="rev_1",
        render_version=READER_RENDER_VERSION,
        markdown_uri="kbasset://tenants/1/literature/papers/doi-10.1002_adma.202407106/render.md",
        image_count=3,
        latex_count=12,
    )
    assert render.render_version == "reader_render_v1"
    # 本地路径 / MinIO/S3 物理路径 / 空值一律拒绝（物理布局不泄漏进知识数据）
    for bad_uri in ("/tmp/local.md", "minio://knowledgebuckets/x.md", "s3://bucket/x.md", ""):
        with pytest.raises(ValueError, match="kbasset"):
            ReaderRenderResult(
                parse_revision_id="rev_1",
                render_version=READER_RENDER_VERSION,
                markdown_uri=bad_uri,
            )
