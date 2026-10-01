"""文献域翻译包（P3，ADR-0010）。

latextap.py 逐字移植自 PaperAgent ``paperkb/translate/latextap.py``（公式
[[MATHn]] 占位/回填，逐字节还原契约）；提示词与整篇策略语义移植自
``translate/pipeline.py``；LLM 走 Yuxi 模型链（不引 openai 直连）。
"""

from yuxi.literature.translate.latextap import reassemble, split_text
from yuxi.literature.translate.service import (
    enqueue_literature_translation,
    process_literature_translation,
    reconcile_stale_literature_translations,
)

__all__ = [
    "enqueue_literature_translation",
    "process_literature_translation",
    "reassemble",
    "reconcile_stale_literature_translations",
    "split_text",
]
