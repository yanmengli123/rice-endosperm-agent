from __future__ import annotations

from typing import Any

from yuxi.knowledge.chunking.ragflow_like.parsers import academic, book, general, laws, qa, semantic, separator
from yuxi.knowledge.chunking.ragflow_like.presets import map_to_internal_parser_id, normalize_chunk_preset_id
from yuxi.knowledge.utils.text_utils import sanitize_extracted_text


def _build_chunk_records(
    text_chunks: list[str], file_id: str, filename: str, source_text: str | None = None
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    search_from = 0

    for idx, chunk_content in enumerate(text_chunks):
        text = sanitize_extracted_text(chunk_content or "").strip()
        if not text:
            continue

        start_char_pos = None
        end_char_pos = None
        if source_text:
            found_at = source_text.find(text, search_from)
            if found_at >= 0:
                start_char_pos = found_at
                end_char_pos = found_at + len(text)
                search_from = end_char_pos

        records.append(
            {
                "id": f"{file_id}_chunk_{idx}",
                "content": text,
                "file_id": file_id,
                "filename": filename,
                "chunk_index": idx,
                "source": filename,
                "chunk_id": f"{file_id}_chunk_{idx}",
                "start_char_pos": start_char_pos,
                "end_char_pos": end_char_pos,
                "start_token_pos": None,
                "end_token_pos": None,
                "extraction_result": None,
            }
        )

    return records


def _dispatch_markdown_parser(
    preset_id: str, filename: str, markdown_content: str, parser_config: dict[str, Any]
) -> list[str]:
    parser_id = map_to_internal_parser_id(preset_id)
    # 注入源文件名供分块器做文献标题/标识符兜底（不覆盖调用方显式设置）
    parser_config = {"source_filename": filename, **parser_config}

    if parser_id == "naive":
        return general.chunk_markdown(markdown_content, parser_config)
    if parser_id == "qa":
        return qa.chunk_markdown(filename, markdown_content, parser_config)
    if parser_id == "book":
        return book.chunk_markdown(markdown_content, parser_config)
    if parser_id == "laws":
        return laws.chunk_markdown(filename, markdown_content, parser_config)
    if parser_id == "semantic":
        return semantic.chunk_markdown(markdown_content, parser_config)
    if parser_id == "separator":
        return separator.chunk_markdown(markdown_content, parser_config)

    return general.chunk_markdown(markdown_content, parser_config)


def chunk_markdown(
    markdown_content: str, file_id: str, filename: str, processing_params: dict[str, Any]
) -> list[dict[str, Any]]:
    params = dict(processing_params or {})
    preset_id = normalize_chunk_preset_id(params.get("chunk_preset_id"))
    parser_config = params.get("chunk_parser_config") if isinstance(params.get("chunk_parser_config"), dict) else {}

    sanitized_markdown = sanitize_extracted_text(markdown_content)
    if preset_id == "academic":
        academic_chunks = academic.chunk_markdown(sanitized_markdown, parser_config)
        records: list[dict[str, Any]] = []
        search_from = 0
        for index, chunk in enumerate(academic_chunks):
            text = sanitize_extracted_text(str(chunk.get("text") or "")).strip()
            if not text:
                continue
            section_path = [str(item) for item in chunk.get("section_path") or [] if item]
            pages = [int(page) for page in chunk.get("pages") or []]
            anchor_ids = [str(anchor_id) for anchor_id in chunk.get("anchor_ids") or [] if anchor_id]
            provenance = []
            if section_path:
                provenance.append(f"【章节】{' > '.join(section_path)}")
            if pages:
                page_label = str(pages[0]) if len(pages) == 1 else f"{pages[0]}-{pages[-1]}"
                provenance.append(f"【页码】{page_label}")
            if anchor_ids:
                provenance.append(f"【证据锚点】{'、'.join(anchor_ids)}")
            content = "\n".join([*provenance, text]) if provenance else text
            embedding_text = sanitize_extracted_text(str(chunk.get("embedding_text") or "")).strip()
            retrieval_content = "\n".join([*provenance, embedding_text]) if embedding_text else content
            found_at = sanitized_markdown.find(text, search_from)
            end_at = found_at + len(text) if found_at >= 0 else None
            if end_at is not None:
                search_from = end_at
            chunk_id = f"{file_id}_chunk_{index}"
            records.append(
                {
                    "id": chunk_id,
                    "content": content,
                    "retrieval_content": retrieval_content,
                    "file_id": file_id,
                    "filename": filename,
                    "chunk_index": index,
                    "source": filename,
                    "chunk_id": chunk_id,
                    "start_char_pos": found_at if found_at >= 0 else None,
                    "end_char_pos": end_at,
                    "start_token_pos": None,
                    "end_token_pos": None,
                    "tags": ["scientific_pdf", str(chunk.get("block_type") or "paragraph")],
                    "source_provenance": {
                        "schema_version": "scientific_pdf_chunk_v2",
                        "section_path": section_path,
                        "page_numbers": pages,
                        "evidence_anchor_ids": anchor_ids,
                        "block_type": str(chunk.get("block_type") or "paragraph"),
                        "retrieval_representation": chunk.get("retrieval_representation"),
                    },
                    "extraction_result": None,
                }
            )
        return records

    text_chunks = _dispatch_markdown_parser(preset_id, filename, sanitized_markdown, parser_config)
    return _build_chunk_records(text_chunks, file_id, filename, sanitized_markdown)


def chunk_file(
    file_content: str, file_id: str, filename: str, processing_params: dict[str, Any]
) -> list[dict[str, Any]]:
    # 当前链路中入库前均已转换为 markdown，因此与 chunk_markdown 保持同实现。
    return chunk_markdown(file_content, file_id, filename, processing_params)
