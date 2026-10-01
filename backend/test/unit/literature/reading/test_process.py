"""渲染子进程执行器测试（ADR-0010 决议 7）。

真实渲染用例是**算法保真 smoke**：在子进程里跑完整的 vendor P14 M1-M6
（本地骨架 + md 对齐 + 修复 + 图抽取），证明 vendor 树在 Yuxi 运行时可用。
"""

from __future__ import annotations

import re

import pymupdf
import pytest

from yuxi.literature.reading.process import RenderProcessError, render_in_subprocess

pytestmark = pytest.mark.unit

_TITLE = "Starch synthesis in rice endosperm"
_BODY_PARAS = [
    "The OsNF-YB1 transcription factor regulates starch biosynthesis genes in rice.",
    "Endosperm cells accumulate starch granules during grain filling stages.",
    "Enzyme activity assays confirmed the regulatory effect on AGPLase.",
    "Field experiments across three seasons showed consistent yield gains.",
    "These results establish a framework for metabolic engineering of rice.",
]


def _build_fixture_pdf(path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 90
    page.insert_text((72, y), _TITLE, fontsize=16, fontname="helv")
    y += 34
    for para in _BODY_PARAS:
        page.insert_text((72, y), para, fontsize=10, fontname="helv")
        y += 20
    doc.save(str(path))
    doc.close()


def _build_fixture_md(path) -> None:
    lines = [f"# {_TITLE}", ""]
    lines.extend(f"{para}\n" for para in _BODY_PARAS)
    path.write_text("\n".join(lines), encoding="utf-8")


def test_render_timeout_kills_child(tmp_path):
    (tmp_path / "input.md").write_text("# x\n", encoding="utf-8")
    with pytest.raises(RenderProcessError, match="超时"):
        render_in_subprocess(
            pdf_path=str(tmp_path / "x.pdf"),
            md_path=str(tmp_path / "input.md"),
            work_dir=str(tmp_path / "run"),
            timeout_s=2,
            _test_hook="sleep",
        )


def test_render_child_crash_is_structured_failure(tmp_path):
    (tmp_path / "input.md").write_text("# x\n", encoding="utf-8")
    with pytest.raises(RenderProcessError):
        render_in_subprocess(
            pdf_path=str(tmp_path / "x.pdf"),
            md_path=str(tmp_path / "input.md"),
            work_dir=str(tmp_path / "run"),
            _test_hook="crash",
        )


def test_offline_render_produces_readable_markdown(tmp_path):
    """真实渲染：vendor P14 M1-M6 全链路（0 网络，上游 parse_regression 同款模式）。"""
    pdf_path = tmp_path / "source.pdf"
    md_path = tmp_path / "full.md"
    _build_fixture_pdf(pdf_path)
    _build_fixture_md(md_path)

    result = render_in_subprocess(pdf_path=str(pdf_path), md_path=str(md_path), work_dir=str(tmp_path / "run"))

    assert result["ok"] is True
    markdown = result["markdown"]
    assert markdown.strip(), "渲染产物不得为空"
    # 渲染质量 smoke：标题与正文段落被保留（骨架对齐 + 修复未丢正文）
    assert "Starch synthesis" in markdown
    assert "OsNF-YB1" in markdown
    assert re.fullmatch(r"[0-9a-f]{64}", result["source_digest"])
