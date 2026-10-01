"""文献域 L1/L2/L3 编译包（P2，ADR-0010）。

语义移植自 PaperAgent ``paperkb``（提示词/价值分/渲染器逐字保留；LLM 换
Yuxi 模型链）。**paperkb 永不作为运行时依赖**（决议 5b）。
"""

from yuxi.literature.notes.compiler import compile_l1_l2, compile_l3, render_l1_note, render_l2_wiki
from yuxi.literature.notes.value_score import L2_THRESHOLD, L3_THRESHOLD, auto_upgrade_levels, compute_value_score

__all__ = [
    "L2_THRESHOLD",
    "L3_THRESHOLD",
    "auto_upgrade_levels",
    "compile_l1_l2",
    "compile_l3",
    "compute_value_score",
    "render_l1_note",
    "render_l2_wiki",
]
