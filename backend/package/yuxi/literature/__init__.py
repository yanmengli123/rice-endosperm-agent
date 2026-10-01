"""文献域（Literature Domain）——PaperAgent 能力进入 Yuxi 的唯一边界（ADR-0010）。

P0 只包含领域契约与身份归一；**不 import 任何 PaperAgent 代码**
（P1 起仅本包内部允许触碰锁版本 vendor 的 ``paperparse``，
守卫见 ``test/unit/literature/test_boundaries.py``）。

证据相关类型一律复用 ``yuxi.knowledge.products.contracts``
（EvidenceEnvelope / WikiNavigationHit），本域不出现第二套证据载体。
"""

from yuxi.literature.contracts import (
    READER_RENDER_URI_SCHEME,
    READER_RENDER_VERSION,
    ReaderRenderResult,
    ResourceId,
    ResourceKind,
)
from yuxi.literature.identity.kinds import (
    ATTACHMENT_KINDS,
    KINDS,
    KIND_PREFIX,
)
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
    "READER_RENDER_URI_SCHEME",
    "READER_RENDER_VERSION",
    "ReaderRenderResult",
    "ResourceId",
    "ResourceKind",
    "doi_to_dirname",
    "is_doi",
    "kind_from_rid",
    "library_candidates",
    "make_rid",
    "normalize_doi",
]
