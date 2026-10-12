import fitz

from yuxi.knowledge.pdf_evidence.tables import TableHTMLParser, build_tables


def _grid(page, rows, x=40, y=60):
    for r in range(len(rows) + 1):
        page.draw_line((x, y + 30 * r), (x + 200, y + 30 * r))
    for c in range(3):
        page.draw_line((x + 100 * c, y), (x + 100 * c, y + 30 * len(rows)))
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            page.insert_text((x + 100 * c + 5, y + 30 * r + 18), value)


def _block(index, html):
    return {
        "block_type": "table",
        "block_id": f"table:{index}",
        "page_index": index,
        "bbox": [39, 59, 241, 121],
        "raw": {"table_body": html, "table_caption": ["Table 1 Rice"], "table_footnote": ["Mean ± SD"]},
    }


def test_continuation_requires_complete_unique_row_and_column_matches():
    html = (
        "<table><tr><th>Gene</th><th>Value</th></tr><tr><td>Wx</td><td>1.2</td></tr>"
        "<tr><td>SSII</td><td>2.3</td></tr><tr><td>GBSS</td><td>3.4</td></tr></table>"
    )
    with fitz.open() as doc:
        _grid(doc.new_page(), [["Gene", "Value"], ["Wx", "1.2"]])
        _grid(doc.new_page(), [["SSII", "2.3"], ["GBSS", "3.4"]])
        blocks = [_block(0, html), _block(1, "")]
        regions = {b["block_id"]: {**b, "region_id": f"r{i}"} for i, b in enumerate(blocks)}
        tables = build_tables(doc, blocks, regions)
        assert len(tables) == 1
        table = tables[0]
        assert table["region_ids"] == ["r0", "r1"]
        assert table["metadata"]["continuation_status"] == "VERIFIED_ROWS_AND_COLUMNS"
        target = next(c for c in table["cells"] if c["raw_value"] == "2.3")
        assert target["metadata"]["table_region_id"] == "r1"
        assert target["metadata"]["column_headers"] == ["Value"]
        assert target["metadata"]["row_header"] == "SSII"
        assert target["metadata"]["footnotes"] == ["Mean ± SD"]
        assert target["bbox"] == [140, 60, 240, 90]


def test_adjacent_unrelated_table_is_not_merged():
    with fitz.open() as doc:
        _grid(doc.new_page(), [["Gene", "Value"], ["Wx", "1.2"]])
        _grid(doc.new_page(), [["Other", "X"], ["Different", "99"]])
        blocks = [
            _block(0, "<table><tr><td>Gene</td><td>Value</td></tr><tr><td>Wx</td><td>1.2</td></tr></table>"),
            _block(1, ""),
        ]
        regions = {b["block_id"]: {**b, "region_id": f"r{i}"} for i, b in enumerate(blocks)}
        assert len(build_tables(doc, blocks, regions)) == 2


def test_merged_cells_preserve_occupied_grid_and_header_values():
    parser = TableHTMLParser()
    parser.feed(
        "<table><tr><th rowspan='2'>Gene</th><th colspan='2'>Rice</th></tr><tr><th>A</th><th>B</th></tr>"
        "<tr><td>Wx</td><td>1</td><td>2</td></tr></table>"
    )
    assert [(c["row_index"], c["column_index"]) for c in parser.cells] == [
        (0, 0),
        (0, 1),
        (1, 1),
        (1, 2),
        (2, 0),
        (2, 1),
        (2, 2),
    ]
    assert parser.cells[0]["row_span"] == 2
    assert parser.cells[1]["column_span"] == 2
