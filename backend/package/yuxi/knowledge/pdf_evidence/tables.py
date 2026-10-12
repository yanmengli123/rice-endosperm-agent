"""Logical cells and verified original geometry, including continuations."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser

import fitz

from yuxi.knowledge.pdf_evidence.text_geometry import locate_text, normalize_with_map

TABLE_BUILDER_VERSION = "pdf-table-cells.v3"


def _norm(value):
    return normalize_with_map(str(value or "")).text


class TableHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cells = []
        self.row = -1
        self.column = 0
        self.occupied = set()
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row += 1
            self.column = 0
        elif tag in {"td", "th"}:
            if self.row < 0 or len(self.cells) >= 20_000:
                raise ValueError("PDF_TABLE_STRUCTURE_BUDGET_EXCEEDED")
            while (self.row, self.column) in self.occupied:
                self.column += 1
            options = dict(attrs)
            row_span = max(1, int(options.get("rowspan") or 1))
            column_span = max(1, int(options.get("colspan") or 1))
            if row_span > 1000 or column_span > 1000 or row_span * column_span > 20_000:
                raise ValueError("PDF_TABLE_SPAN_BUDGET_EXCEEDED")
            self.current = {
                "row_index": self.row,
                "column_index": self.column,
                "row_span": row_span,
                "column_span": column_span,
                "is_header": tag == "th",
                "raw_value": "",
            }
            for r in range(self.row, self.row + row_span):
                for c in range(self.column, self.column + column_span):
                    self.occupied.add((r, c))
            self.column += column_span
        elif tag == "br" and self.current is not None:
            self.current["raw_value"] += "\n"

    def handle_data(self, data):
        if self.current is not None:
            self.current["raw_value"] += data

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.current is not None:
            self.current["raw_value"] = self.current["raw_value"].strip()
            self.cells.append(self.current)
            self.current = None


def _expected_rows(cells):
    if not cells:
        return []
    width = max(c["column_index"] + c["column_span"] for c in cells)
    height = max(c["row_index"] + c["row_span"] for c in cells)
    if width * height > 100_000:
        raise ValueError("PDF_TABLE_STRUCTURE_BUDGET_EXCEEDED")
    rows = [[""] * width for _ in range(height)]
    for c in cells:
        rows[c["row_index"]][c["column_index"]] = _norm(c["raw_value"])
    return rows


def _detect_rows(document, block):
    page = document[block["page_index"]]
    page.set_rotation(0)
    for strategy in ("lines", "text"):
        # MinerU's normalized integer boxes can exclude an outer rule by a
        # fraction of a point. Include that rule without accepting nearby tables.
        parent = fitz.Rect(block["bbox"])
        clip = (parent + (-1, -1, 1, 1)) & page.rect
        found = page.find_tables(clip=clip, strategy=strategy)
        rows = []
        for candidate in found.tables:
            candidate_rect = fitz.Rect(candidate.bbox)
            if not candidate_rect.get_area() or (candidate_rect & parent).get_area() / candidate_rect.get_area() < 0.98:
                continue
            for row, values in zip(candidate.rows, candidate.extract(), strict=True):
                rows.append(
                    {
                        "values": [_norm(v) for v in values],
                        "boxes": row.cells,
                        "block_id": block["block_id"],
                        "page_index": block["page_index"],
                    }
                )
        if rows:
            return rows
    return []


def _unique_row_matches(expected, detected):
    matches = {}
    for physical in detected:
        positions = [i for i, row in enumerate(expected) if row == physical["values"] and any(row)]
        if len(positions) == 1:
            matches.setdefault(positions[0], []).append(physical)
    return {i: rows[0] for i, rows in matches.items() if len(rows) == 1}


def build_tables(document, blocks, region_by_block, *, native_pages=None):
    tables = []
    for block in blocks:
        if block["block_type"] != "table":
            continue
        raw = block["raw"]
        html = str(raw.get("table_body") or "")
        caption_value = raw.get("table_caption") or []
        caption = " ".join(str(v) for v in caption_value) if isinstance(caption_value, list) else str(caption_value)
        parser = TableHTMLParser()
        parser.feed(html)
        detected = _detect_rows(document, block)
        region = region_by_block[block["block_id"]]
        candidates = []
        # Proximity alone cannot establish that a blank block continues a table.
        if not html and detected:
            for table in tables:
                previous = region_by_block[table["metadata"]["source_block_ids"][-1]]
                matches = _unique_row_matches(_expected_rows(table["cells"]), detected)
                already = set(table["metadata"]["physical_rows"])
                if (
                    region["page_index"] == previous["page_index"] + 1
                    and abs(region["bbox"][0] - previous["bbox"][0]) <= 3
                    and abs(region["bbox"][2] - previous["bbox"][2]) <= 3
                    and matches
                    and len(matches) == len(detected)
                    and not already.intersection(matches)
                ):
                    candidates.append((table, matches))
        if len(candidates) == 1:
            table, matches = candidates[0]
            table["region_ids"].append(region["region_id"])
            table["metadata"]["source_block_ids"].append(block["block_id"])
            table["metadata"]["physical_rows"].update(matches)
            table["metadata"]["continuation_status"] = "VERIFIED_ROWS_AND_COLUMNS"
        else:
            matches = _unique_row_matches(_expected_rows(parser.cells), detected)
            table = {
                "table_id": "pt_" + hashlib.sha256(block["block_id"].encode()).hexdigest()[:40],
                "caption": caption,
                "region_ids": [region["region_id"]],
                "cells": parser.cells,
                "metadata": {
                    "version": TABLE_BUILDER_VERSION,
                    "html": html,
                    "footnotes": raw.get("table_footnote") or [],
                    "source_block_ids": [block["block_id"]],
                    "physical_rows": matches,
                    "continuation_status": "SINGLE_PART" if html else "UNRESOLVED",
                },
            }
            tables.append(table)
    for table in tables:
        physical_rows = table["metadata"].pop("physical_rows")
        for cell in table["cells"]:
            cell["region_id"] = None
            cell["metadata"] = {
                "locator_status": "UNRESOLVED",
                "geometry_precision": "REGION",
                "table_region_id": table["region_ids"][0],
                "offset_basis": "unicode_code_point",
                "row_header": next(
                    (
                        c["raw_value"]
                        for c in table["cells"]
                        if c["row_index"] == cell["row_index"] and c["column_index"] == 0
                    ),
                    "",
                ),
                "column_headers": [
                    c["raw_value"]
                    for c in table["cells"]
                    if c["is_header"]
                    and c["row_index"] <= cell["row_index"]
                    and c["column_index"] <= cell["column_index"] < c["column_index"] + c["column_span"]
                ],
                "footnotes": table["metadata"]["footnotes"],
            }
            row = physical_rows.get(cell["row_index"])
            if row:
                boxes = [
                    physical_rows[r]["boxes"][c]
                    for r in range(cell["row_index"], cell["row_index"] + cell["row_span"])
                    for c in range(cell["column_index"], cell["column_index"] + cell["column_span"])
                    if r in physical_rows
                    and c < len(physical_rows[r]["boxes"])
                    and physical_rows[r]["page_index"] == row["page_index"]
                    and physical_rows[r]["boxes"][c]
                ]
                if boxes and all(
                    r in physical_rows and physical_rows[r]["page_index"] == row["page_index"]
                    for r in range(cell["row_index"], cell["row_index"] + cell["row_span"])
                ):
                    rect = fitz.Rect(boxes[0])
                    for box in boxes[1:]:
                        rect |= fitz.Rect(box)
                    cell["bbox"] = list(rect)
                    cell["metadata"].update(
                        {
                            "locator_status": "VERIFIED",
                            "geometry_precision": "CELL",
                            "table_region_id": region_by_block[row["block_id"]]["region_id"],
                        }
                    )
            # Without a cell boundary, a uniquely matched complete value retains
            # its actual glyph/word precision; it is never relabelled as CELL.
            if "bbox" not in cell and cell["raw_value"] and native_pages:
                choices = []
                for block_id in table["metadata"]["source_block_ids"]:
                    parent = region_by_block[block_id]
                    located = locate_text(cell["raw_value"], native_pages[parent["page_index"]], parent["bbox"])
                    if located["status"] == "VERIFIED":
                        choices.append((parent, located))
                if len(choices) == 1:
                    parent, located = choices[0]
                    quads = located["quads"]
                    cell["bbox"] = [
                        min(q[i] for q in quads for i in (0, 2, 4, 6)),
                        min(q[i] for q in quads for i in (1, 3, 5, 7)),
                        max(q[i] for q in quads for i in (0, 2, 4, 6)),
                        max(q[i] for q in quads for i in (1, 3, 5, 7)),
                    ]
                    cell["metadata"].update(
                        {
                            "locator_status": "VERIFIED",
                            "geometry_precision": located["precision"],
                            "table_region_id": parent["region_id"],
                            "quads": quads,
                        }
                    )
    return tables
