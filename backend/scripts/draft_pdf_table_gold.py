"""表格金标草案与核验（RC→GA §②）：真实论文表格 → 人工复核金标 → 回归对比。

金标闭环（schema ``pdf-table-gold.v1``，与 scripts/verify_pdf_table_gold.py 同源）：

1. **草案**（本脚本 ``draft`` 子命令）：用**产线同一受控解析器**
   （``project_publishable_table`` 完整七道发布门禁）
   从活动版本提取实际单元格，落 ``reviewed: false`` 草案；
2. **人工复核**：对照论文出版 PDF 逐值核对草案（改正/补漏/删多），
   置 ``reviewed: true`` 并填 ``reviewed_by``——草案只是排版起点，
   **绝不**把草案当金标直接判 PASS（否则是自我循环验证）；
3. **核验**（本脚本 ``verify`` 子命令）：重新提取活动版本单元格，
   与已复核金标做 `evaluate_table_gold` 对比（坐标召回/精确值召回/精确率）。

容器内执行：

    docker compose exec -T api uv run --no-sync python scripts/draft_pdf_table_gold.py \
        draft --kb-id <kb> --file-id <file> --label "Table 1" --output /tmp/table1-gold-draft.json
    # 人工复核 /tmp/table1-gold-draft.json 后：
    docker compose exec -T api uv run --no-sync python scripts/draft_pdf_table_gold.py \
        verify --gold /tmp/table1-gold-draft.json --output /tmp/table1-gold-report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from sqlalchemy import select

from yuxi.knowledge.contracts.table_asset_projection import project_publishable_table
from yuxi.knowledge.evidence.caption_locator import canonical_figure_label, extract_figure_label
from yuxi.knowledge.pdf_evidence.table_gold import evaluate_table_gold, table_gold_passes, validate_reviewed_gold
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_knowledge import (
    EvidenceAnchorRecord,
    KnowledgeFile,
    KnowledgeParseRevision,
)

GOLD_SCHEMA = "pdf-table-gold.v1"
_LABEL_PATTERN = re.compile(r"^Table\s+(S?\d+)\b", re.IGNORECASE)


async def _extract_cells(*, kb_id: str, file_id: str, label: str) -> dict:
    pg_manager.initialize()
    async with pg_manager.get_async_session_context() as db:
        file_row = (
            await db.execute(
                select(KnowledgeFile).where(KnowledgeFile.kb_id == kb_id, KnowledgeFile.file_id == file_id)
            )
        ).scalar_one()
        revision = (
            await db.execute(
                select(KnowledgeParseRevision).where(
                    KnowledgeParseRevision.revision_id == file_row.active_parse_revision_id
                )
            )
        ).scalar_one()
        match = _LABEL_PATTERN.match(label.strip())
        if not match:
            raise SystemExit("--label 形如 'Table 1'")
        prefix = f"Table {match.group(1).upper()}"
        anchors = (
            (
                await db.execute(
                    select(EvidenceAnchorRecord)
                    .where(
                        EvidenceAnchorRecord.parse_revision_id == revision.revision_id,
                    )
                    .order_by(EvidenceAnchorRecord.quote)
                )
            )
            .scalars()
            .all()
        )
        table_anchors = [
            anchor
            for anchor in anchors
            if canonical_figure_label(extract_figure_label(anchor.quote)) == canonical_figure_label(prefix)
            and str(anchor.quote or "").lstrip().lower().startswith("table")
        ]
        if not table_anchors:
            raise SystemExit(f"未找到 {prefix} 的题注锚点")
        if len(table_anchors) != 1:
            raise SystemExit(f"{prefix} 题注有歧义，需先修复题注关联")
        anchor = table_anchors[0]
        table, reason = await project_publishable_table(
            db,
            kb_id=kb_id,
            file_id=file_id,
            revision_id=revision.revision_id,
            anchor_id=anchor.anchor_id,
            evidence_id=anchor.anchor_id,
            page=anchor.page,
            label=prefix,
            caption=str(anchor.quote or ""),
            publish_allowed=True,
        )
        if table is None:
            raise SystemExit(f"产线表格投影未通过：{reason}")
    rows = table.rows
    cells = [
        {
            "table_id": f"{prefix.replace(' ', '').lower()}",
            "row_index": row_index,
            "column_index": column_index,
            "raw_value": cell.text,
        }
        for row_index, row in enumerate(rows)
        for column_index, cell in enumerate(row)
    ]
    return {
        "kb_id": kb_id,
        "file_id": file_id,
        "parse_revision_id": revision.revision_id,
        "source_sha256": revision.source_sha256,
        "anchor_id": anchor.anchor_id,
        "caption": str(anchor.quote or "")[:200],
        "label": prefix,
        "cells": cells,
        "row_count": len(rows),
        "column_count": max((len(row) for row in rows), default=0),
        "truncated": table.truncated,
        "limited": table.limited,
    }


async def draft(args: argparse.Namespace) -> None:
    extracted = await _extract_cells(kb_id=args.kb_id, file_id=args.file_id, label=args.label)
    gold = {
        "schema": GOLD_SCHEMA,
        "kb_id": extracted["kb_id"],
        "file_id": extracted["file_id"],
        "parse_revision_id": extracted["parse_revision_id"],
        "source_sha256": extracted["source_sha256"],
        "extraction_source": "chunk_html",
        "exhaustive": not extracted["truncated"] and not extracted["limited"],
        "reviewed": False,
        "reviewed_by": None,
        **{
            key: extracted[key]
            for key in ("anchor_id", "label", "caption", "row_count", "column_count", "truncated", "limited")
        },
        "cells": extracted["cells"],
        "review_instructions": (
            "草案由产线受控解析器自动提取，reviewed=false 不得用于核验。"
            "请对照论文出版 PDF 逐值核对：改正错值、补漏行、删多行，"
            "确认无误后置 reviewed=true 并填写 reviewed_by。"
        ),
    }
    Path(args.output).write_text(json.dumps(gold, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"草案已落 {args.output}：{gold['row_count']} 行 × {gold['column_count']} 列，"
        f"{len(gold['cells'])} 单元格（reviewed=false，待人工核对）"
    )


async def verify(args: argparse.Namespace) -> None:
    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    try:
        validate_reviewed_gold(gold)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    extracted = await _extract_cells(
        kb_id=gold["kb_id"], file_id=gold["file_id"], label=gold.get("label") or gold.get("caption") or "Table 1"
    )
    if extracted["source_sha256"] != gold.get("source_sha256"):
        raise SystemExit("gold source_sha256 与当前活动版本不一致——请先重出草案再复核")
    metrics = evaluate_table_gold(
        expected_cells=list(gold.get("cells") or []),
        actual_cells=extracted["cells"],
        exhaustive=bool(gold.get("exhaustive", True)),
    )
    passed = table_gold_passes(metrics)
    report = {
        "schema": "pdf-table-gold-verification.v1",
        "extraction_source": "chunk_html",
        "status": "PASSED" if passed else "FAILED",
        "gold": str(Path(args.gold).resolve()),
        "reviewed_by": gold.get("reviewed_by"),
        "kb_id": extracted["kb_id"],
        "file_id": extracted["file_id"],
        "parse_revision_id": extracted["parse_revision_id"],
        "source_sha256": extracted["source_sha256"],
        "truncated": extracted["truncated"],
        "limited": extracted["limited"],
        "metrics": metrics,
    }
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "metrics": {k: v for k, v in metrics.items() if k not in ("missing", "unexpected", "value_mismatches")},
            },
            ensure_ascii=False,
        )
    )
    if not passed:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    draft_parser = sub.add_parser("draft", help="从活动版本提取金标草案（reviewed=false）")
    draft_parser.add_argument("--kb-id", required=True)
    draft_parser.add_argument("--file-id", required=True)
    draft_parser.add_argument("--label", required=True, help="表格标签，如 'Table 1'")
    draft_parser.add_argument("--output", required=True)

    verify_parser = sub.add_parser("verify", help="已复核金标 vs 活动版本回归对比")
    verify_parser.add_argument("--gold", required=True)
    verify_parser.add_argument("--output", required=True)

    args = parser.parse_args()
    asyncio.run(draft(args) if args.command == "draft" else verify(args))


if __name__ == "__main__":
    main()
