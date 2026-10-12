"""文献域身份包：资源类型前缀与 RID 归一（ADR-0010）。

语义移植自 PaperAgent ``paperkb/doi.py`` 与 ``paperkb/layout.py``
（版本锚定 paperagent 1.4.0 / paperparse 2.3.0）；与 PaperAgent 的差异
在各函数 docstring 中显式声明。
"""

from yuxi.literature.identity.kinds import ATTACHMENT_KINDS, KIND_PREFIX, KINDS
from yuxi.literature.identity.rid import (
    doi_to_dirname,
    is_doi,
    kind_from_rid,
    library_candidates,
    make_rid,
    normalize_doi,
)

__all__ = [
    "ATTACHMENT_KINDS",
    "KINDS",
    "KIND_PREFIX",
    "doi_to_dirname",
    "is_doi",
    "kind_from_rid",
    "library_candidates",
    "make_rid",
    "normalize_doi",
]
