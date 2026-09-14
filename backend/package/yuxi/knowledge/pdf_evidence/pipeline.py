from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from yuxi.knowledge.chunking.ragflow_like.parsers.academic import ACADEMIC_CHUNKER_VERSION
from yuxi.knowledge.evidence.document_partition import classify_anchor_partitions
from yuxi.knowledge.parser.factory import DocumentProcessorFactory
from yuxi.knowledge.pdf_evidence.aligner import ALIGNER_VERSION, align_texts_to_anchors
from yuxi.knowledge.pdf_evidence.contracts import ParserArtifact, PipelineResult, UnifiedArticle
from yuxi.knowledge.pdf_evidence.grobid import GrobidClient
from yuxi.knowledge.pdf_evidence.mineru_layout import (
    MINERU_LAYOUT_ADAPTER_VERSION,
    build_mineru_anchors,
    extract_mineru_blocks,
)
from yuxi.knowledge.pdf_evidence.native import PYMUPDF_PROVIDER_VERSION, inspect_native_pdf
from yuxi.knowledge.pdf_evidence.page_map import PHYSICAL_PAGE_MAP_VERSION, build_physical_page_map
from yuxi.knowledge.vision.figure_ingestor import FIGURE_INGESTOR_VERSION
from yuxi.utils import logger

# v1.1: 图片引用由 MinIO URL 改为 kbasset:// 逻辑 URI（鉴权 Asset API 渲染），
# canonical Markdown 内容变化，重新解析需生成新 parse revision。
# v3.0: chart/chart_caption 纳入视觉块（Figure 5 事故），assets 携带视觉块
# 记录供 Figure Ingestor 建资产指纹索引——重解析生成新 parse revision。
# v3.1: 图片对象定位改为 parse-revision 专属前缀，Figure Ingestor 版本进入
# parser fingerprint，避免下游索引修复后错误复用旧 revision。
PIPELINE_VERSION = "scientific_pdf_v3.2"
QUALITY_PROFILE_VERSION = "pdf_evidence_v2"
ANCHOR_MARKER = "<!-- yuxi-evidence-anchor:{anchor_id};page={page} -->"


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_parser_fingerprint(source_sha256: str, params: dict[str, Any]) -> str:
    identity = {
        "pipeline": PIPELINE_VERSION,
        "source_sha256": source_sha256,
        "mineru_engine": params.get("ocr_engine") or "mineru_official",
        "mineru_model": (params.get("ocr_engine_config") or {}).get("model_version", "vlm"),
        "grobid_version": os.getenv("GROBID_VERSION", "0.9.1"),
        "grobid_profile": "no-consolidation-v1",
        "native_provider": f"pymupdf-{PYMUPDF_PROVIDER_VERSION}",
        "aligner": ALIGNER_VERSION,
        "mineru_layout_adapter": MINERU_LAYOUT_ADAPTER_VERSION,
        "figure_ingestor": FIGURE_INGESTOR_VERSION,
        "academic_chunker": ACADEMIC_CHUNKER_VERSION,
        "quality_profile": QUALITY_PROFILE_VERSION,
    }
    return hashlib.sha256(_stable_json(identity).encode("utf-8")).hexdigest()


def _markdown_title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return Path(fallback).stem


def _markdown_sections(markdown: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    stack: list[str] = []
    content: list[str] = []

    def flush() -> None:
        body = "\n".join(content).strip()
        if body:
            sections.append({"path": list(stack), "markdown": body})
        content.clear()

    for line in markdown.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not match:
            content.append(line)
            continue
        flush()
        level = len(match.group(1))
        stack[level - 1 :] = [match.group(2).strip()]
    flush()
    return sections


def annotate_markdown_with_anchors(markdown: str, anchors: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Attach only deterministic high-confidence anchors in monotonic reading order."""
    blocks = re.split(r"(\n\s*\n)", markdown)
    content_blocks = [block for block in blocks if block.strip() and not re.fullmatch(r"\n\s*\n", block)]
    matches = align_texts_to_anchors(content_blocks, anchors)
    match_iter = iter(matches)
    alignments: list[dict[str, Any]] = []
    annotated: list[str] = []
    for block in blocks:
        if not block.strip() or re.fullmatch(r"\n\s*\n", block):
            annotated.append(block)
            continue
        candidate = next(match_iter)
        if candidate and not block.lstrip().startswith("#"):
            markers = "\n".join(
                ANCHOR_MARKER.format(anchor_id=anchor_id, page=page)
                for anchor_id, page in zip(
                    candidate.get("anchor_ids") or [candidate["anchor_id"]],
                    candidate.get("anchor_pages") or [candidate["page"]],
                    strict=True,
                )
            )
            annotated.append(markers + "\n" + block)
            alignments.append({"block_hash": hashlib.sha256(block.encode()).hexdigest(), **candidate})
        else:
            annotated.append(block)
    return "".join(annotated), alignments


def _eligible_markdown_block_count(markdown: str) -> int:
    return sum(
        1
        for block in re.split(r"\n\s*\n", markdown)
        if len(re.sub(r"<[^>]+>", " ", block).strip()) >= 12 and not block.lstrip().startswith("#")
    )


class ScientificPdfPipeline:
    """Deterministic PDF evidence pipeline. It never uses an LLM to repair source facts."""

    async def run(
        self,
        file_path: str | Path,
        params: dict[str, Any] | None = None,
        *,
        stage_callback: Callable[[str, str, dict[str, Any] | None], Awaitable[None]] | None = None,
        mineru_cache: tuple[str, list[ParserArtifact]] | None = None,
    ) -> PipelineResult:
        async def emit(stage: str, status: str, detail: dict[str, Any] | None = None) -> None:
            if stage_callback:
                await stage_callback(stage, status, detail)

        path = Path(file_path)
        if path.suffix.lower() != ".pdf":
            raise ValueError("科研 PDF 证据链路只接受 .pdf 文件")
        params = dict(params or {})
        await emit("NATIVE", "RUNNING")
        try:
            native = await asyncio.to_thread(inspect_native_pdf, path)
        except Exception as exc:
            await emit(
                "NATIVE",
                "FAILED",
                {"error_code": type(exc).__name__, "error_detail": str(exc)},
            )
            raise
        await emit("NATIVE", "SUCCEEDED")
        fingerprint = build_parser_fingerprint(native.pdf_sha256, params)
        artifacts = [
            ParserArtifact(
                kind="pymupdf_native",
                filename="pymupdf-native.json",
                content=_stable_json(native.to_dict()).encode("utf-8"),
                content_type="application/json",
            )
        ]

        markdown = ""
        mineru_ok = False
        mineru_error: str | None = None
        engine = str(params.get("ocr_engine") or "mineru_official")
        engine_params = dict(params.get("ocr_engine_config") or {})
        engine_params.update(
            {
                "image_bucket": "knowledgebases",
                "image_prefix": params.get("image_prefix") or "evidence/assets",
                # Markdown 必须携带 kbasset:// 逻辑 URI（由 ingest 层按 file/revision 构造），
                # 绝不携带 MinIO URL / 预签名 URL。
                "asset_uri_builder": params.get("asset_uri_builder"),
                "ocr_fallback_to_text": False,
            }
        )
        await emit("MINERU", "RUNNING")
        if mineru_cache is not None:
            markdown, cached_artifacts = mineru_cache
            artifacts.extend(cached_artifacts)
            mineru_ok = bool(markdown.strip())
        else:
            try:
                processor = DocumentProcessorFactory.get_processor(engine)
                structured_method = getattr(processor, "process_file_with_artifacts", None)
                if structured_method:
                    result = await asyncio.to_thread(structured_method, str(path), engine_params)
                    markdown = str(result["markdown"])
                    artifacts.extend(result.get("artifacts") or [])
                else:
                    markdown = await asyncio.to_thread(processor.process_file, str(path), engine_params)
                mineru_ok = bool(markdown.strip())
            except Exception as exc:  # noqa: BLE001
                mineru_error = f"{type(exc).__name__}: {exc}"
                logger.warning(f"科研 PDF 主解析器不可用，评估原生文本降级: {path.name}: {exc}")

        if not mineru_ok and native.quality["native_text_usable"]:
            markdown = "\n\n".join(page["text"] for page in native.pages if page["text"])
        if mineru_ok:
            await emit("MINERU", "REUSED" if mineru_cache is not None else "SUCCEEDED")
        else:
            await emit(
                "MINERU",
                "DEGRADED" if markdown.strip() else "FAILED",
                {"error_code": "MINERU_UNAVAILABLE", "error_detail": mineru_error},
            )
        if not markdown.strip():
            await emit("GROBID", "SKIPPED")
            await emit("UNIFIED", "SKIPPED")
            await emit("QUALITY", "RUNNING")
            qa_report = {
                "accepted": False,
                "capability": "REJECTED",
                "native": native.quality,
                "mineru": {"ok": False, "error": mineru_error},
                "grobid": {"attempted": False, "ok": False},
                "reasons": ["MinerU 未生成正文，且 PDF 没有可用原生文本层"],
            }
            await emit(
                "QUALITY",
                "REJECTED",
                {"error_code": "NO_USABLE_TEXT", "error_detail": qa_report["reasons"][0]},
            )
            return PipelineResult("REJECTED", None, qa_report, artifacts, "", fingerprint)

        grobid_data: dict[str, Any] = {}
        grobid_ok = False
        grobid_error: str | None = None
        grobid_attempted = bool(native.quality["native_text_usable"] and params.get("grobid_enabled", True))
        await emit("GROBID", "RUNNING")
        if grobid_attempted:
            try:
                tei_xml, grobid_data = await GrobidClient().process(path)
                artifacts.append(
                    ParserArtifact(
                        kind="grobid_tei",
                        filename="grobid.tei.xml",
                        content=tei_xml.encode("utf-8"),
                        content_type="application/xml",
                    )
                )
                grobid_ok = True
            except Exception as exc:  # noqa: BLE001
                grobid_error = f"{type(exc).__name__}: {exc}"
                logger.warning(f"GROBID 增强不可用，保留正文能力: {path.name}: {exc}")
        if grobid_ok:
            await emit("GROBID", "SUCCEEDED")
        elif grobid_attempted:
            await emit(
                "GROBID",
                "DEGRADED",
                {"error_code": "GROBID_UNAVAILABLE", "error_detail": grobid_error},
            )
        else:
            await emit("GROBID", "SKIPPED")

        await emit("UNIFIED", "RUNNING")
        mineru_blocks = extract_mineru_blocks(artifacts, native.page_geometry) if mineru_ok else []
        mineru_anchors = (
            build_mineru_anchors(
                native.pdf_sha256,
                artifacts,
                page_geometry=native.page_geometry,
                blocks=mineru_blocks,
            )
            if mineru_blocks
            else []
        )
        # MinerU content_list geometry is the primary locator because it is
        # emitted from the same semantic blocks as canonical Markdown.
        # PyMuPDF remains a medium-confidence fallback for old/parser-lite
        # output, never a reason to claim full highlight capability.
        canonical_anchors = mineru_anchors or native.anchors
        anchor_dicts = classify_anchor_partitions([anchor.to_dict() for anchor in canonical_anchors])
        annotated_markdown, alignments = await asyncio.to_thread(
            annotate_markdown_with_anchors,
            markdown,
            anchor_dicts,
        )
        citation_mentions = list(grobid_data.get("citation_mentions") or [])
        citation_matches = await asyncio.to_thread(
            align_texts_to_anchors,
            [str(mention.get("context") or mention.get("text") or "") for mention in citation_mentions],
            anchor_dicts,
        )
        for mention, match in zip(citation_mentions, citation_matches, strict=True):
            if match:
                mention["anchor_id"] = match["anchor_id"]
                mention["anchor_confidence"] = match["score"]
        physical_page_map = build_physical_page_map(native, mineru_blocks, grobid_data)
        # 视觉块记录（R-P2）：image/figure/chart 块的 img_path + 页码 + bbox +
        # 题注文本随 article.assets 持久化，供 Figure Ingestor 下载图片字节、
        # 计算 sha256/pHash/panel 指纹并落 figure_assets 索引。
        visual_assets = [
            {
                "kind": "figure",
                "block_type": str(block["block_type"]),
                "img_path": str(block.get("source_path") or ""),
                "page_index": int(block["page_index"]),
                "page": int(block["page"]),
                "bbox": list(block["bbox"]),
                "caption": str(block["text"] or ""),
                "block_id": str(block.get("block_id") or ""),
            }
            for block in mineru_blocks
            if block["block_type"] in {"image", "figure", "chart"}
        ]
        grobid_metadata = grobid_data.get("metadata") or {}
        article = UnifiedArticle(
            schema_version="2.0",
            source_sha256=native.pdf_sha256,
            title=str(grobid_metadata.get("title") or _markdown_title(markdown, path.name)),
            markdown=markdown,
            metadata={
                **grobid_metadata,
                "filename": path.name,
                "page_count": native.page_count,
                "page_geometry": native.page_geometry,
            },
            sections=_markdown_sections(markdown),
            references=list(grobid_data.get("references") or []),
            citation_mentions=citation_mentions,
            anchors=anchor_dicts,
            assets=visual_assets,
            parser_provenance={
                "pipeline_version": PIPELINE_VERSION,
                "parser_fingerprint": fingerprint,
                "canonical_content_source": "mineru" if mineru_ok else "pymupdf",
                "pymupdf": {"ok": True},
                "mineru": {
                    "ok": mineru_ok,
                    "engine": engine,
                    "error": mineru_error,
                    "reused": mineru_cache is not None,
                },
                "grobid": {"attempted": grobid_attempted, "ok": grobid_ok, "error": grobid_error},
                "alignment": {"version": ALIGNER_VERSION, "matches": alignments},
                "locator": {
                    "schema_version": "evidence_anchor_v2",
                    "primary_source": "mineru" if mineru_anchors else "pymupdf_fallback",
                    "physical_page_map_version": PHYSICAL_PAGE_MAP_VERSION,
                    "validation": physical_page_map["validation"],
                },
            },
        )
        artifact_payload = _stable_json(article.to_dict()).encode("utf-8")
        artifacts.append(
            ParserArtifact(
                kind="unified_article",
                filename="unified-article.json",
                content=artifact_payload,
                content_type="application/json",
            )
        )
        evidence_map_payload = _stable_json(
            {
                "schema_version": "evidence_map_v1",
                "source_sha256": native.pdf_sha256,
                "physical_page_map": physical_page_map,
                "anchors": anchor_dicts,
                "alignments": alignments,
            }
        ).encode("utf-8")
        artifacts.append(
            ParserArtifact(
                kind="evidence_map",
                filename="evidence-map.json",
                content=evidence_map_payload,
                content_type="application/json",
            )
        )
        await emit("UNIFIED", "SUCCEEDED")

        eligible_blocks = _eligible_markdown_block_count(markdown)
        high_quality_alignments = [
            match
            for match in alignments
            if match.get("locatable")
            and match.get("locator_quality") == "HIGH"
            and float(match.get("score", 0)) >= 0.85
        ]
        locator_coverage = len(high_quality_alignments) / eligible_blocks if eligible_blocks else 0.0
        structured_tables = [anchor for anchor in mineru_anchors if anchor.anchor_type == "table"]
        structured_figures = [
            anchor
            for anchor in mineru_anchors
            if anchor.anchor_type in {"image", "figure", "chart"}
            and re.search(
                r"(?:^|\s)(?:fig(?:ure)?\.?|图)\s*[\dA-Za-z]",
                anchor.quote,
                re.IGNORECASE,
            )
        ]
        visual_figure_assets = [asset for asset in visual_assets if asset["img_path"]]
        invalid_geometry_count = int(physical_page_map["validation"].get("invalid_mineru_block_count") or 0)
        full_locator_ready = bool(mineru_anchors and locator_coverage >= 0.75 and invalid_geometry_count == 0)
        if mineru_ok and grobid_ok and full_locator_ready:
            capability = "INDEXED_FULL"
        elif mineru_ok:
            capability = "INDEXED_CONTENT_ONLY"
        else:
            capability = "INDEXED_TEXT_ONLY"
        await emit("QUALITY", "RUNNING")
        qa_report = {
            "accepted": True,
            "capability": capability,
            "native": native.quality,
            "mineru": {"ok": mineru_ok, "error": mineru_error, "reused": mineru_cache is not None},
            "grobid": {"attempted": grobid_attempted, "ok": grobid_ok, "error": grobid_error},
            "quality_profile": QUALITY_PROFILE_VERSION,
            "counts": {
                "pages": native.page_count,
                "sections": len(article.sections),
                "anchors": len(article.anchors),
                "references": len(article.references),
                "citation_mentions": len(article.citation_mentions),
                "aligned_blocks": len(alignments),
                "high_confidence_aligned_blocks": len(high_quality_alignments),
                "eligible_markdown_blocks": eligible_blocks,
                "structured_tables": len(structured_tables),
                "structured_figures": len(structured_figures),
                "figure_assets": len(visual_figure_assets),
            },
            "locator_profile": {
                "schema_version": "evidence_anchor_v2",
                "primary_source": "mineru" if mineru_anchors else "pymupdf_fallback",
                "high_confidence_coverage": round(locator_coverage, 4),
                "minimum_full_coverage": 0.75,
                "invalid_geometry_count": invalid_geometry_count,
            },
            "capabilities": {
                "fulltext_search": True,
                "academic_structure": bool(grobid_ok and article.sections),
                "citation_navigation": bool(
                    grobid_ok
                    and article.references
                    and any(mention.get("anchor_id") for mention in article.citation_mentions)
                ),
                "pdf_highlight": bool(high_quality_alignments),
                "figure_retrieval": bool(structured_figures),
                "table_retrieval": bool(structured_tables),
                "formula_retrieval": bool(re.search(r"\$[^$]+\$|\\\[.+?\\\]", markdown, re.DOTALL)),
            },
        }
        await emit("QUALITY", "SUCCEEDED")
        return PipelineResult(capability, article, qa_report, artifacts, annotated_markdown, fingerprint)
