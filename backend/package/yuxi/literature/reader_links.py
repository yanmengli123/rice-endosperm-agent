"""PaperAgent/Obsidian reader targets, never server filesystem paths."""

import re
from urllib.parse import unquote

PRODUCT_TABS = {
    "_note": "noteL1",
    "L1_note": "noteL1",
    "_wiki": "noteL2",
    "L2_wiki": "noteL2",
    "_relations": "noteL3",
    "L3_relations": "noteL3",
    "render": "original",
    "en": "original",
    "document": "original",
    "zh": "translation",
    "summary": "translation",
    "en_zh": "parallel",
}


def parse_reader_target(target: str) -> dict:
    value = unquote(target.strip())
    if value.startswith("[[") and value.endswith("]]"):
        value = value[2:-2]
    value = value.split("|", 1)[0].strip()
    if not value or len(value) > 500 or any(ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError("文献双链无效")
    identity, _, anchor = value.partition("#")
    if re.match(r"^(?:[a-z]+:|/)", identity, re.I):
        raise ValueError("不接受外部地址或服务器路径")
    parts = identity.split("/")
    relative = identity.startswith(("./", "../")) or not identity
    if parts[0] in {".", "..", "knowledge_base", "library"}:
        parts = parts[1:]
    if not parts:
        raise ValueError("文献双链缺少目标")
    if any(part in {".", ".."} for part in parts) or any(":" in part for part in parts):
        raise ValueError("文献双链越过受支持的目录范围")
    last = re.sub(r"\.md$", "", parts[-1], flags=re.I)
    tab, variant = None, None
    if last in PRODUCT_TABS:
        tab = PRODUCT_TABS[last]
        variant = last if last in {"zh", "summary", "en_zh"} else None
        parts = parts[:-1]
    else:
        parts[-1] = last
        suffix = re.search(r"_(?:L([123])(?:_(?:note|deep|relations))?|note|wiki|deep|relations)$", last, re.I)
        if suffix:
            tab = (
                f"noteL{suffix[1]}"
                if suffix[1]
                else {"_note": "noteL1", "_wiki": "noteL2", "_deep": "noteL2", "_relations": "noteL3"}[
                    suffix[0].lower()
                ]
            )
            parts[-1] = last[: suffix.start()]
    return {
        "identity": "/".join(parts),
        "tab": tab,
        "variant": variant,
        "anchor": anchor,
        "relative": relative or not parts,
    }
