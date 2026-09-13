"""视觉观察 Provider：调用多模态模型产出 VisualObservationEnvelope。

职责边界：provider 只做「看图说话」——识图、提文字、认 panel、给题注片段；
页码/文件/anchor 的决定权在确定性裁决器（figure_image_locator）。provider
不可用（未配置 / 调用失败 / 输出非法）时返回 None，定位链路失败关闭，
绝不回退到让生成模型自由回答页码。
"""

from __future__ import annotations

import base64

from yuxi.config.app import config as app_config
from yuxi.knowledge.vision.visual_observation import (
    VisualObservationEnvelope,
    parse_visual_observation,
)
from yuxi.utils import logger

VISION_PROVIDER_VERSION = "vision_provider_v1"

OBSERVATION_PROMPT = """你是科研图像观察器。观察这张来自生物学论文的图片，只输出一个 JSON 对象
（不要任何其他文字、不要 Markdown 围栏），schema 如下：

{
  "schema_version": "visual-observation.v1",
  "figure_label": "图片中可见的图表编号，如 'Figure 1'；不可见填 null",
  "panel_labels": ["可见的 panel 子编号，如 'a', 'b'"],
  "visible_entities": ["图中可见的基因名/蛋白名/标记，如 'OsMYB73-GFP'"],
  "visible_text": ["图中可见的坐标轴标题/图例文字/短标签"],
  "caption_fragments": ["这张图对应的题注可能包含的关键短语"],
  "visual_structure": {
    "bar_chart": false,
    "microscopy": false,
    "tissue_images": false,
    "gel": false,
    "phylogenetic_tree": false,
    "line_chart": false
  },
  "confidence": 0.0
}

规则：只描述可见内容，不推测不编造；禁止输出页码、文件名、文献信息——这些字段在 schema 中不存在，加了会被整体拒绝。"""

_PROVIDER_SINGLETON: dict[str, object] = {}


class NullVisionProvider:
    """未配置视觉模型时的空实现：观察不可用 → 图片定位失败关闭。"""

    async def describe(self, image_bytes: bytes) -> VisualObservationEnvelope | None:
        return None

    @property
    def available(self) -> bool:
        return False


class ChatModelVisionProvider:
    """OpenAI 风格多模态 chat 模型观察器（image_url 内容块）。"""

    def __init__(self, model_spec: str):
        self.model_spec = model_spec

    @property
    def available(self) -> bool:
        return bool(self.model_spec)

    async def describe(self, image_bytes: bytes) -> VisualObservationEnvelope | None:
        if not image_bytes:
            return None
        try:
            from langchain_core.messages import HumanMessage

            from yuxi.models.chat import select_model

            adapter = select_model(self.model_spec)
            encoded = base64.b64encode(image_bytes).decode("ascii")
            message = HumanMessage(
                content=[
                    {"type": "text", "text": OBSERVATION_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                ]
            )
            response = await adapter.call(message)
            return parse_visual_observation(getattr(response, "content", None))
        except Exception as exc:  # noqa: BLE001 - provider 故障按观察不可用处理
            logger.error(f"vision provider failed (observation unavailable): {exc}")
            return None


def get_vision_provider() -> NullVisionProvider | ChatModelVisionProvider:
    spec = str(getattr(app_config, "vision_model_spec", "") or "")
    if not spec:
        return NullVisionProvider()
    cache_key = f"vision:{spec}"
    if cache_key not in _PROVIDER_SINGLETON:
        _PROVIDER_SINGLETON[cache_key] = ChatModelVisionProvider(spec)
    return _PROVIDER_SINGLETON[cache_key]  # type: ignore[return-value]


__all__ = [
    "VISION_PROVIDER_VERSION",
    "ChatModelVisionProvider",
    "NullVisionProvider",
    "get_vision_provider",
]
