"""Prepare physical evidence outside database transactions from immutable PDF bytes."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fitz

from yuxi.knowledge.evidence.sentence_splitter import split_evidence_units
from yuxi.knowledge.pdf_evidence.contracts import NativePdfSnapshot, ParserArtifact
from yuxi.knowledge.pdf_evidence.geometry import bbox_is_on_page
from yuxi.knowledge.pdf_evidence.region_ocr import recognize_region
from yuxi.knowledge.pdf_evidence.tables import build_tables
from yuxi.knowledge.pdf_evidence.text_geometry import COORDINATE_FRAME, locate_text, locate_text_fragments

PHYSICAL_EVIDENCE_VERSION = "pdf-physical-evidence.v2"


def prepare_physical_evidence(
    path: Path, native: NativePdfSnapshot, blocks: list[dict], anchors: list[dict], params: dict
) -> dict[str, Any]:
    regions, images, assets = [], [], []
    geometry = {p["page_index"]: p for p in native.page_geometry}
    region_by_block = {}
    ocr_report = {"attempted": 0, "ready": 0, "unavailable": 0, "budget_exceeded": 0}
    visual_count = sum(b["block_type"] in {"figure", "image", "chart"} for b in blocks)
    max_assets = int(params.get("max_figure_assets", 200))
    if visual_count > max_assets:
        raise ValueError(f"PDF_FIGURE_BUDGET_EXCEEDED: {visual_count}>{max_assets}")
    with fitz.open(path) as document:
        for block in blocks:
            frame = geometry[block["page_index"]]
            if not bbox_is_on_page(block["bbox"], frame):
                raise ValueError(f"PDF_REGION_OUT_OF_PAGE: {block['block_id']}")
            region_id = (
                "pr_"
                + hashlib.sha256(
                    f"{native.pdf_sha256}|{PHYSICAL_EVIDENCE_VERSION}|{block['block_id']}".encode()
                ).hexdigest()[:40]
            )
            selector = {
                "version": PHYSICAL_EVIDENCE_VERSION,
                "coordinate_frame": COORDINATE_FRAME,
                "canonical_to_pdf": frame["canonical_to_pdf"],
                "precision": "REGION",
                "page_width": frame["width"],
                "page_height": frame["height"],
                "source_bbox": list(block.get("source_bbox", block["bbox"])),
                "geometry_status": "VERIFIED",
            }
            region = {
                "region_id": region_id,
                "source_block_id": block["block_id"],
                "kind": block["block_type"],
                "page_index": block["page_index"],
                "page_label": frame["page_label"],
                "bbox": list(block["bbox"]),
                "text": block["text"] or None,
                "selector": selector,
            }
            regions.append(region)
            region_by_block[block["block_id"]] = region
            page = document[block["page_index"]]
            visual = block["block_type"] in {"figure", "image", "chart"}
            has_native_region_text = any(
                block["bbox"][0] <= (c["bbox"][0] + c["bbox"][2]) / 2 <= block["bbox"][2]
                and block["bbox"][1] <= (c["bbox"][1] + c["bbox"][3]) / 2 <= block["bbox"][3]
                for c in native.pages[block["page_index"]]["characters"]
                if c.get("provider") != "ocr"
            )
            if visual or (
                block["block_type"] in {"text", "ref_text", "table", "caption"}
                and (not native.pages[block["page_index"]].get("native_text_usable") or not has_native_region_text)
            ):
                if params.get("region_ocr_enabled", True):
                    ocr_report["attempted"] += 1
                    try:
                        ocr = recognize_region(
                            page,
                            block["bbox"],
                            dpi=int(params.get("region_ocr_dpi", 144)),
                            min_confidence=float(params.get("region_ocr_min_confidence", 0.9)),
                        )
                    except Exception as exc:
                        ocr = {"status": "UNAVAILABLE", "words": [], "error": f"{type(exc).__name__}: {exc}"[:300]}
                    ocr_report[
                        {"READY": "ready", "BUDGET_EXCEEDED": "budget_exceeded"}.get(ocr["status"], "unavailable")
                    ] += 1
                    selector["ocr"] = ocr
                    if not visual and ocr["words"]:
                        native.pages[block["page_index"]]["characters"].extend(
                            {**word, "line_id": f"{region_id}:{word['line_id']}"} for word in ocr["words"]
                        )
            if not visual:
                continue
            page.set_rotation(0)
            pix = page.get_pixmap(
                matrix=fitz.Matrix(2, 2), clip=fitz.Rect(block["bbox"]), colorspace=fitz.csRGB, alpha=False
            )
            png = pix.tobytes("png")
            kind = f"pdf_region_image_{region_id[3:]}"
            images.append(ParserArtifact(kind, f"{region_id}.png", png, "image/png"))
            ocr_words = selector.get("ocr", {}).get("words", [])
            labels = [
                w["text"].strip("()[]").upper()
                for w in ocr_words
                if len(w["text"].strip("()[]")) == 1
                and w["text"].strip("()[]").isalpha()
                and w["confidence"] >= 0.95
                and w["bbox"][0] <= block["bbox"][0] + (block["bbox"][2] - block["bbox"][0]) * 0.3
                and w["bbox"][1] <= block["bbox"][1] + (block["bbox"][3] - block["bbox"][1]) * 0.3
            ]
            if ocr_words:
                transcript = " ".join(w["text"] for w in ocr_words)
                digest = hashlib.sha256(transcript.encode()).hexdigest()
                parent = next(
                    (
                        a
                        for a in anchors
                        if any(f.get("source_block_id") == block["block_id"] for f in a.get("fragments", []))
                    ),
                    {},
                )
                anchors.append(
                    {
                        "anchor_id": "ea_" + hashlib.sha256(f"{region_id}|ocr|{digest}".encode()).hexdigest()[:40],
                        "page": block["page"],
                        "bbox": list(block["bbox"]),
                        "word_start": 0,
                        "word_end": len(ocr_words),
                        "quote": transcript,
                        "quote_hash": digest,
                        "prefix_hash": hashlib.sha256(b"").hexdigest(),
                        "suffix_hash": hashlib.sha256(b"").hexdigest(),
                        "anchor_type": "figure_ocr",
                        "locator_quality": "MEDIUM",
                        "confidence": min(w["confidence"] for w in ocr_words),
                        "source": "rapidocr",
                        "locatable": True,
                        "document_partition": parent.get("document_partition", "UNKNOWN"),
                        "partition_confidence": parent.get("partition_confidence", 0),
                        "fragments": [
                            {
                                "page_index": block["page_index"],
                                "bbox": list(block["bbox"]),
                                "source_block_id": block["block_id"],
                                "coordinate_space": "pdf_points",
                            }
                        ],
                    }
                )
            assets.append(
                {
                    "kind": "figure",
                    "block_type": block["block_type"],
                    "img_path": f"{region_id}.png",
                    "original_img_path": block.get("source_path"),
                    "page_index": block["page_index"],
                    "page": block["page"],
                    "bbox": list(block["bbox"]),
                    "caption": block["text"],
                    "block_id": block["block_id"],
                    "region_id": region_id,
                    "image_artifact_kind": kind,
                    "ocr_text": " ".join(w["text"] for w in ocr_words) or None,
                    "panel_label": "",
                    "locator_metadata": {
                        **selector,
                        "detected_panel_labels": labels,
                        "panel_association": "single_label_in_layout_region" if len(labels) == 1 else "UNRESOLVED",
                    },
                }
            )
        tables = build_tables(document, blocks, region_by_block, native_pages=native.pages)
        for table in tables:
            if table["metadata"]["continuation_status"] == "VERIFIED_ROWS_AND_COLUMNS":
                source_ids = table["metadata"]["source_block_ids"]
                parent_anchor = next(
                    (a for a in anchors if any(f.get("source_block_id") == source_ids[0] for f in a["fragments"])), None
                )
                if parent_anchor:
                    fragments = list(parent_anchor["fragments"])
                    included = {f.get("source_block_id") for f in fragments}
                    for continuation in anchors:
                        if continuation is not parent_anchor:
                            for fragment in continuation["fragments"]:
                                block_id = fragment.get("source_block_id")
                                if block_id in source_ids[1:] and block_id not in included:
                                    fragments.append(fragment)
                                    included.add(block_id)
                    parent_anchor["fragments"] = fragments
        for table in tables:
            parent = next(r for r in regions if r["region_id"] == table["region_ids"][0])
            for cell in table["cells"]:
                if "bbox" not in cell:
                    continue
                cell_parent = next(r for r in regions if r["region_id"] == cell["metadata"]["table_region_id"])
                cell_id = (
                    "pr_"
                    + hashlib.sha256(
                        f"{table['table_id']}|{cell['row_index']}|{cell['column_index']}".encode()
                    ).hexdigest()[:40]
                )
                cell["region_id"] = cell_id
                regions.append(
                    {
                        **cell_parent,
                        "region_id": cell_id,
                        "kind": "table_cell",
                        "bbox": cell["bbox"],
                        "text": cell["raw_value"],
                        "source_block_id": (
                            f"{cell_parent['source_block_id']}:cell:{cell['row_index']}:{cell['column_index']}"
                        ),
                        "selector": {
                            **cell_parent["selector"],
                            "precision": cell["metadata"]["geometry_precision"],
                            "quads": cell["metadata"].get("quads", []),
                            "parent_region_id": cell_parent["region_id"],
                        },
                    }
                )
    block_by_id = {b["block_id"]: b for b in blocks}
    for asset in assets:
        observed = set(asset["locator_metadata"]["detected_panel_labels"])
        verified_labels = []
        for fragment in block_by_id[asset["block_id"]].get("physical_fragments", []):
            label = re.fullmatch(r"\(([A-Za-z])\)", fragment.get("text", "").strip())
            if label is None:
                continue
            located = locate_text(fragment["text"], native.pages[fragment["page_index"]], fragment["bbox"])
            if located["status"] == "VERIFIED":
                observed.add(label.group(1).upper())
                verified_labels.append(
                    {
                        "label": label.group(1).upper(),
                        "region_id": region_by_block[fragment["source_block_id"]]["region_id"],
                        "precision": located["precision"],
                        "quads": located["quads"],
                    }
                )
        asset["locator_metadata"]["detected_panel_labels"] = sorted(observed)
        asset["locator_metadata"]["label_sources"] = verified_labels
    for anchor in anchors:
        for fragment in anchor.get("fragments", []):
            frame = geometry[fragment["page_index"]]
            region = region_by_block.get(fragment.get("source_block_id"))
            if region is None:
                region_id = (
                    "pr_"
                    + hashlib.sha256(
                        f"{native.pdf_sha256}|{fragment.get('source_block_id', anchor['anchor_id'])}".encode()
                    ).hexdigest()[:40]
                )
                region = {
                    "region_id": region_id,
                    "source_block_id": fragment.get("source_block_id") or anchor["anchor_id"],
                    "kind": anchor["anchor_type"],
                    "page_index": fragment["page_index"],
                    "page_label": frame["page_label"],
                    "bbox": list(fragment["bbox"]),
                    "text": anchor["quote"],
                    "selector": {
                        "coordinate_frame": COORDINATE_FRAME,
                        "canonical_to_pdf": frame["canonical_to_pdf"],
                        "precision": "REGION",
                        "geometry_status": "VERIFIED",
                    },
                }
                region["selector"].update({"page_width": frame["width"], "page_height": frame["height"]})
                regions.append(region)
                region_by_block[region["source_block_id"]] = region
            fragment.update(
                {
                    "region_id": region["region_id"],
                    "coordinate_frame": COORDINATE_FRAME,
                    "canonical_to_pdf": frame["canonical_to_pdf"],
                    "page_label": frame["page_label"],
                    "precision": "REGION",
                    "page_width": frame["width"],
                    "page_height": frame["height"],
                }
            )
    units = split_evidence_units(anchors=[SimpleNamespace(**a) for a in anchors], markdown_body="")
    by_anchor = {a["anchor_id"]: a for a in anchors}
    prepared_units = []
    for unit in units:
        anchor = by_anchor[unit.anchor_id]
        pages = native.pages
        if anchor["anchor_type"] == "figure_ocr":
            pages = list(native.pages)
            for fragment in anchor["fragments"]:
                index = fragment["page_index"]
                region = region_by_block[fragment["source_block_id"]]
                pages[index] = {**pages[index], "characters": region["selector"]["ocr"]["words"]}
        locator = locate_text_fragments(
            unit.quote,
            anchor["fragments"],
            pages,
            source_text=anchor["quote"],
            source_start=unit.source_start,
            source_end=unit.source_end,
        )
        prepared_units.append({**asdict(unit), "locator": locator})
    for table in tables:
        parent = next(r for r in regions if r["region_id"] == table["region_ids"][0])
        anchor = next(
            (
                a
                for a in anchors
                if any(f.get("source_block_id") == parent["source_block_id"] for f in a.get("fragments", []))
            ),
            None,
        )
        if anchor is None:
            continue
        for cell in table["cells"]:
            if not cell["raw_value"]:
                continue
            region = next((r for r in regions if r["region_id"] == cell.get("region_id")), parent)
            fragment = {
                "page_index": region["page_index"],
                "page_number": region["page_index"] + 1,
                "bbox": region["bbox"],
                "region_id": region["region_id"],
                "coordinate_space": "pdf_points",
                "origin": "top_left",
                **region["selector"],
                "page_label": region["page_label"],
            }
            prepared_units.append(
                {
                    "evidence_type": "table_cell",
                    "quote": cell["raw_value"],
                    "container_label": table["caption"] or None,
                    "row_key": cell["metadata"]["row_header"],
                    "sentence_index": len(prepared_units),
                    "anchor_id": anchor["anchor_id"],
                    "source_start": 0 if cell.get("region_id") else None,
                    "source_end": len(cell["raw_value"]) if cell.get("region_id") else None,
                    "offset_scope": f"region:{region['region_id']}"
                    if cell.get("region_id")
                    else f"table:{table['table_id']}:cell:{cell['row_index']}:{cell['column_index']}",
                    "context": {
                        "table_id": table["table_id"],
                        "row_index": cell["row_index"],
                        "column_index": cell["column_index"],
                        **cell["metadata"],
                    },
                    "locator": {
                        "precision": region["selector"]["precision"] if cell.get("region_id") else "REGION",
                        "status": cell["metadata"]["locator_status"],
                        "fragments": [fragment],
                    },
                }
            )
    return {
        "regions": regions,
        "tables": tables,
        "evidence_units": prepared_units,
        "assets": assets,
        "artifacts": images,
        "ocr_report": ocr_report,
        "version": PHYSICAL_EVIDENCE_VERSION,
    }
