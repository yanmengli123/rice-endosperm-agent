from __future__ import annotations

import unicodedata

_PRESERVED_CONTROL_CHARACTERS = frozenset("\t\n\r")
_REPLACEMENT_CHARACTER = "�"
_QUALITY_MAX_REPLACEMENT_RATIO = 0.08
_QUALITY_MIN_SAMPLE_CHARS = 200


def sanitize_extracted_text(text: str) -> str:
    """Return UTF-8-safe text suitable for parsing, embedding, and storage."""
    if not text:
        return text

    sanitized: list[str] = []
    changed = False

    for character in text:
        if character in _PRESERVED_CONTROL_CHARACTERS:
            sanitized.append(character)
            continue

        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            sanitized.append(_REPLACEMENT_CHARACTER)
            changed = True
            continue

        if unicodedata.category(character) == "Cc":
            sanitized.append(" ")
            changed = True
            continue

        sanitized.append(character)

    return "".join(sanitized) if changed else text


class ParseQualityError(ValueError):
    """解析结果未达到入库质量门槛（空白/乱码）。"""


def validate_markdown_quality(markdown: str | None) -> str:
    """解析出口的质量门禁：拒绝空白结果与高比例乱码文本。

    规则刻意保持宽松：只拦截明显不可用的解析产物（空文本、U+FFFD 占比超阈值），
    更细的文献级质量评估由上传后的文件状态与人工审核承担。
    """
    text = str(markdown or "")
    if not text.strip():
        # 扫描件整页 OCR 失败时典型产物就是纯空白；空结果一律不得入库。
        raise ParseQualityError("解析结果为空白，疑似空白页、扫描件 OCR 失败或损坏文件，已拒绝入库。")

    replacement_count = text.count(_REPLACEMENT_CHARACTER)
    total_count = len(text)
    # 比率阈值只在足量样本上启用：短文本里单个坏字符即可造成高占比，属于误报
    if total_count >= _QUALITY_MIN_SAMPLE_CHARS and replacement_count / total_count > _QUALITY_MAX_REPLACEMENT_RATIO:
        percent = round(replacement_count / total_count * 100, 1)
        threshold = round(_QUALITY_MAX_REPLACEMENT_RATIO * 100, 1)
        raise ParseQualityError(
            f"解析结果乱码字符占比 {percent}% 超过 {threshold}%，疑似编码异常或字体损坏的文档，已拒绝入库。"
        )

    return text
