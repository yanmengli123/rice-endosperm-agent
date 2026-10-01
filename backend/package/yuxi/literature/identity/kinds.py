"""资源类型与前缀的**单一来源**（ADR-0010 决议 1）。

语义移植自 PaperAgent ``paperkb/doi.py::KIND_PREFIX`` 与
``paperkb/layout.py::KINDS``（版本锚定 paperagent 1.4.0）。

禁止在业务代码里裸写前缀字符串——**``standard`` 的前缀是 ``std__`` 而非
``standard__``**（PaperAgent ``layout._prefix_to_kind`` 记载的漂移教训：
两处定义必然漂移，一处写错则 kind_from_rid 误判）。
"""

from __future__ import annotations

#: 用户可拥有的资源类型（与 contracts.ResourceKind 一一对应）。
KINDS: tuple[str, ...] = (
    "paper",
    "thesis",
    "book",
    "chapter",
    "patent",
    "standard",
    "report",
    "note",
)

#: 依附资料类型：挂父资源、不单独成文献、不进知识库列表。
#: 移植自 PaperAgent layout.ATTACHMENT_KINDS（含 Yuxi 侧暂未使用的 cover/response，
#: 保持与上游目录布局的导入兼容）。
ATTACHMENT_KINDS: tuple[str, ...] = ("si", "review", "data", "note", "cover", "response")

#: 类型前缀表（kind → RID 前缀）。paper 无前缀（保持目录名与历史一致）；
#: si/review/note 三类**子资源**前缀用于依附资料 RID 合成（如 ``si__<父RID>``），
#: report/data 无前缀（上游即如此：无 DOI 的报告/数据以 ``nd-`` 指纹身份落地）。
KIND_PREFIX: dict[str, str] = {
    "paper": "",
    "si": "si__",
    "review": "review__",
    "note": "note__",
    "thesis": "thesis__",
    "book": "book__",
    "chapter": "chapter__",
    "patent": "patent__",
    "standard": "std__",
}
