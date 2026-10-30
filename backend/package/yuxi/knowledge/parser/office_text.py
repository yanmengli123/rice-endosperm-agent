"""Local document extraction that preserves identifiers and reads every XLS sheet."""

from pathlib import Path
import re

from bs4 import BeautifulSoup
from markdownify import markdownify


def xls_to_markdown(path: Path) -> str:
    import pandas as pd

    sheets = pd.read_excel(path, engine="xlrd", sheet_name=None, header=None, dtype=object, keep_default_na=False)
    blocks = []
    for name, frame in sheets.items():
        if frame.empty:
            continue
        if len(sheets) > 1:
            blocks.append(f"## {name}")
        blocks.append(frame.to_markdown(index=False, headers=[""] * len(frame.columns), disable_numparse=True))
    return "\n\n".join(blocks)


def html_to_markdown(content: str) -> str:
    # Offline extraction never loads linked resources or executes scripts.
    document = BeautifulSoup(content, "html.parser")
    for tag in document.find_all(["script", "style", "noscript", "iframe", "object", "embed", "template"]):
        tag.decompose()
    for image in document.find_all("img"):
        image.replace_with(image.get("alt", ""))
    for media in document.find_all(["video", "audio", "source"]):
        media.decompose()
    return markdownify(str(document), heading_style="ATX")


def mineru_options(params: dict) -> dict:
    backend = params.get("backend", "hybrid-auto-engine")
    method = params.get("parse_method", "auto")
    if not isinstance(backend, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", backend):
        raise ValueError("不支持的 MinerU backend")
    if method not in {"auto", "txt", "ocr"}:
        raise ValueError("MinerU parse_method 必须是 auto、txt 或 ocr")
    return {"backend": backend, "parse_method": method}
