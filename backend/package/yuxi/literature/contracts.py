"""文献域类型契约（P0）。

只定义 Yuxi 尚不存在的文献域概念；证据相关类型一律复用
``yuxi.knowledge.products.contracts``（EvidenceEnvelope / WikiNavigationHit），
避免出现第二套证据载体。

防御性设计与 ``WikiNavigationHit`` 同源：``ReaderRenderResult`` 刻意不含
content/text/quote/anchor 字段——「派生物被顺手塞进证据列表」在类型层
就构造不出来，而不是靠 code review 拦截（ADR-0010 决议 2）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: 资源类型（用户可拥有的文献种类；依附资料 kind 见 identity.kinds.ATTACHMENT_KINDS）。
#: 语义移植自 PaperAgent ``paperkb/layout.py`` 的 KINDS（排除 si/review/data
#: 三类依附资料），版本锚定 paperagent 1.4.0。
ResourceKind = Literal["paper", "thesis", "book", "chapter", "patent", "standard", "report", "note"]

#: 阅读视图派生物的渲染契约版本。进派生对象身份（``ReaderRenderResult.render_version``），
#: **不进** parser_fingerprint——阅读视图的变更不得触碰解析权威层的缓存身份。
READER_RENDER_VERSION = "reader_render_v1"

#: 双通道渲染版本（决议 12：可选增强，PaddleOCR 辅通道 + 第三信号裁决票，
#: 0 LLM 成本产出 review.json）。以 render_version 编码模式——与离线版共用
#: (revision_id, render_version) 唯一键，两行可并存（离线版保审计轨迹）。
READER_RENDER_DUAL_VERSION = "reader_render_v1-dual"

#: 文献域 LLM 调用超时（秒）。整篇大 prompt（全文前缀 + 六维任务/翻译）在
#: 长上下文模型上普遍超过对话轮默认的 45s（agents.models.CHAT_MODEL_REQUEST_
#: TIMEOUT_SECONDS）——e2e 实测编译 L1 稳定 45s 整超时。显式放宽到 10 分钟，
#: 仅影响文献域 select_model 调用点，不改对话默认。
LITERATURE_LLM_TIMEOUT_S = 600

#: 阅读视图派生物唯一合法的资产引用方案：kbasset:// 逻辑资产 URI。
#: 禁止本地路径与 MinIO 物理路径出现在文献域契约里（物理布局永不泄漏进知识数据）。
READER_RENDER_URI_SCHEME = "kbasset://"


@dataclass(frozen=True, slots=True)
class ResourceId:
    """文献资源身份。kind 进名字（与 PaperAgent「单一物理根 + 类型进名字」同源，
    由 :func:`yuxi.literature.identity.rid.make_rid` 生成 RID）。"""

    rid: str
    kind: ResourceKind
    doi: str = ""

    def __post_init__(self) -> None:
        if not (self.rid or "").strip():
            raise ValueError("ResourceId.rid 不能为空（请经 make_rid 生成）")


@dataclass(frozen=True, slots=True)
class ReaderRenderResult:
    """P14 阅读视图派生物（Navigation/Reading 平面产物，**不是证据**）。

    刻意不含 content/text/quote/anchor 字段：正文只经逻辑资产 URI 引用，
    使本类型在构造层就无法混入证据通道（ADR-0010 不变量 L0-2）。
    """

    parse_revision_id: str
    render_version: str
    markdown_uri: str
    image_count: int = 0
    latex_count: int = 0
    source_digest: str = ""

    def __post_init__(self) -> None:
        if not (self.markdown_uri or "").startswith(READER_RENDER_URI_SCHEME):
            raise ValueError(
                f"markdown_uri 必须是 {READER_RENDER_URI_SCHEME} 逻辑资产 URI，禁止本地/物理路径: {self.markdown_uri!r}"
            )
