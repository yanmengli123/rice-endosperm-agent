"""视觉观察 Provider：调用多模态模型产出 VisualObservationEnvelope。

职责边界：provider 只做「看图说话」——识图、提文字、认 panel、给观察片段；
页码/文件/anchor 的决定权在确定性裁决器（figure_image_locator）。provider
不可用（未配置 / 调用失败 / 输出非法）时返回 None，定位链路失败关闭，
绝不回退到让生成模型自由回答页码。

能力状态机（D1）：**配置存在 ≠ 能力可用**。spec 非空只是 CONFIGURED；真实
可用性由 canary（真实图片 → provider → schema 校验）确认为 READY，运行期
按观察结果降级为 PROVIDER_FAILED / SCHEMA_INVALID / IMAGE_UNREADABLE。
观察缓存以 ``processed_image_sha256 + model_spec + prompt_version +
schema_version`` 复合键（pHash 只做候选搜索，不做缓存身份——有碰撞）。
"""

from __future__ import annotations

import base64
import hashlib
from collections import OrderedDict

from yuxi.config.app import config as app_config
from yuxi.knowledge.vision.visual_observation import (
    VISUAL_OBSERVATION_SCHEMA_VERSION,
    VisualObservationEnvelope,
    parse_visual_observation,
)
from yuxi.utils import logger

VISION_PROVIDER_VERSION = "vision_provider_v2"
OBSERVATION_PROMPT_VERSION = "observation_prompt_v1"
VISION_CAPABILITY_STATUS_VERSION = "vision_capability_v1"

# 能力状态：CONFIGURED 是静态配置事实；READY/失败态只能由真实 canary/调用确立
VISION_NOT_CONFIGURED = "NOT_CONFIGURED"
VISION_CONFIGURED = "CONFIGURED"
VISION_READY = "READY"
VISION_PROVIDER_FAILED = "PROVIDER_FAILED"
VISION_SCHEMA_INVALID = "SCHEMA_INVALID"
VISION_IMAGE_UNREADABLE = "IMAGE_UNREADABLE"

OBSERVATION_PROMPT = """你是科研图像观察器。观察这张来自生物学论文的图片，只输出一个 JSON 对象
（不要任何其他文字、不要 Markdown 围栏），schema 如下：

{
  "schema_version": "visual-observation.v1",
  "figure_label": "图片中可见的图表编号，如 'Figure 1'；不可见填 null",
  "panel_labels": ["可见的 panel 子编号，如 'a', 'b'"],
  "visible_entities": ["图中可见的基因名/蛋白名/标记，如 'OsMYB73-GFP'"],
  "visible_text": ["图中可见的坐标轴标题/图例文字/短标签（逐字照抄，仅作未验证观察）"],
  "inferred_caption_fragments": ["推测该图题注可能包含的关键短语（模型推断，非逐字，永不作为绑定信号）"],
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

规则：只描述可见内容，不推测不编造；visible_text 尽量逐字照抄图中文字；
禁止输出页码、文件名、文献信息——这些字段在 schema 中不存在，加了会被整体拒绝。"""

_PROVIDER_SINGLETON: dict[str, object] = {}
# 观察缓存：复合键（图片字节 sha256 + spec + prompt 版本 + schema 版本），
# 有界 LRU。pHash 不参与缓存身份（感知哈希有碰撞，两张近似图共享观察会串结果）。
_OBSERVATION_CACHE_LIMIT = 32
# 发往视觉模型前的确定性降采样：最长边 ≤ 1024、JPEG q82。整页级 PNG（>500KB）在外部
# API 上会超时；观察任务只需读编号/可见文字/结构，降采样不改变契约语义
_OBSERVATION_MAX_SIDE = 1024
_OBSERVATION_JPEG_QUALITY = 82


def _downscale_for_observation(image_bytes: bytes) -> tuple[bytes, str]:
    """返回 (payload, mime)；任何失败回退原字节 + image/png（不阻断观察）。"""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as source:
            image = source.convert("RGB")
        longest = max(image.size)
        if longest > _OBSERVATION_MAX_SIDE:
            scale = _OBSERVATION_MAX_SIDE / float(longest)
            image = image.resize(
                (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                Image.Resampling.LANCZOS,
            )
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=_OBSERVATION_JPEG_QUALITY, optimize=True)
        return buffer.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - 降采样失败按原图发送
        return image_bytes, "image/png"


_OBSERVATION_CACHE: OrderedDict[tuple[str, str, str, str], VisualObservationEnvelope] = OrderedDict()
# canary 结论（进程级缓存；key = spec）
_CAPABILITY_CACHE: dict[str, dict[str, str]] = {}


class NullVisionProvider:
    """未配置视觉模型时的空实现：观察不可用 → 图片定位失败关闭。"""

    async def describe(self, image_bytes: bytes) -> VisualObservationEnvelope | None:
        return None

    @property
    def available(self) -> bool:
        return False

    @property
    def ready(self) -> bool:
        return False


class ChatModelVisionProvider:
    """OpenAI 风格多模态 chat 模型观察器（image_url 内容块）。"""

    def __init__(self, model_spec: str):
        self.model_spec = model_spec

    @property
    def available(self) -> bool:
        return bool(self.model_spec)

    @property
    def ready(self) -> bool:
        """运行门禁（G5）：spec 非空 ≠ 能力可用——必须 canary 实测 READY。

        canary 判定 PROVIDER_FAILED/SCHEMA_INVALID 后进程内缓存生效，运行时
        不再调用 provider（状态机与实际执行行为一致）。
        """
        return bool(self.model_spec) and current_vision_status().get("status") == VISION_READY

    async def describe(self, image_bytes: bytes) -> VisualObservationEnvelope | None:
        if not image_bytes:
            return None
        cache_key = (
            hashlib.sha256(image_bytes).hexdigest(),
            self.model_spec,
            OBSERVATION_PROMPT_VERSION,
            VISUAL_OBSERVATION_SCHEMA_VERSION,
        )
        cached = _OBSERVATION_CACHE.get(cache_key)
        if cached is not None:
            _OBSERVATION_CACHE.move_to_end(cache_key)
            return cached.model_copy(deep=True)
        observation = await self._describe_uncached(image_bytes)
        if observation is not None:
            _OBSERVATION_CACHE[cache_key] = observation
            while len(_OBSERVATION_CACHE) > _OBSERVATION_CACHE_LIMIT:
                _OBSERVATION_CACHE.popitem(last=False)
        return observation

    async def _describe_uncached(self, image_bytes: bytes) -> VisualObservationEnvelope | None:
        try:
            from langchain_core.messages import HumanMessage

            from yuxi.models.chat import select_model

            adapter = select_model(self.model_spec)
            payload, mime = _downscale_for_observation(image_bytes)
            encoded = base64.b64encode(payload).decode("ascii")
            message = HumanMessage(
                content=[
                    {"type": "text", "text": OBSERVATION_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                ]
            )
            # adapter.call 经 convert_to_messages 归一化，需要"消息序列"；传单个 HumanMessage 会被
            # 当成 (field, value) 元组迭代而报 "Unexpected message type: 'content'"
            response = await adapter.call([message])
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


def _canary_image_bytes() -> bytes | None:
    """合成一张极小 PNG 作为 canary 输入（依赖缺失时返回 None）。"""
    try:
        import io

        from PIL import Image, ImageDraw

        image = Image.new("RGB", (64, 48), "white")
        draw = ImageDraw.Draw(image)
        draw.text((4, 16), "YUXI CANARY", fill="black")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 - PIL 缺失按图片不可读处理
        return None


async def probe_vision_capability(*, force: bool = False) -> dict[str, str]:
    """真实图片 canary：CONFIGURED ≠ READY，可用性必须由真实链路确立。

    结论进程级缓存（``force=True`` 重探）。返回
    ``{model, status, detail, version}``；status ∈ NOT_CONFIGURED /
    READY / IMAGE_UNREADABLE / PROVIDER_FAILED / SCHEMA_INVALID。
    """
    spec = str(getattr(app_config, "vision_model_spec", "") or "")
    result = {
        "model": spec,
        "status": VISION_NOT_CONFIGURED,
        "detail": "vision_model_spec 为空：视觉通道失败关闭（确定性指纹不受影响）",
        "version": VISION_CAPABILITY_STATUS_VERSION,
    }
    if not spec:
        return result
    if not force and spec in _CAPABILITY_CACHE:
        return _CAPABILITY_CACHE[spec]
    if force:
        _CAPABILITY_CACHE.pop(spec, None)
    canary = _canary_image_bytes()
    if canary is None:
        result["status"] = VISION_IMAGE_UNREADABLE
        result["detail"] = "canary 图片合成失败（图像栈不可用）"
    else:
        provider = ChatModelVisionProvider(spec)
        try:
            observation = await provider._describe_uncached(canary)  # noqa: SLF001 - canary 不进观察缓存
        except Exception as exc:  # noqa: BLE001
            observation = None
            result["status"] = VISION_PROVIDER_FAILED
            result["detail"] = f"provider 调用异常: {type(exc).__name__}"
        if observation is None and result["status"] == VISION_NOT_CONFIGURED:
            result["status"] = VISION_PROVIDER_FAILED
            result["detail"] = "canary 观察失败（provider 报错或输出未通过 schema 校验）"
        elif observation is not None:
            result["status"] = VISION_READY
            result["detail"] = "真实图片 canary 通过：观察契约 schema 校验成功"
    _CAPABILITY_CACHE[spec] = result
    return result


def mark_observation_schema_invalid(spec: str) -> None:
    """运行期观察解析失败 → 能力状态降级 SCHEMA_INVALID（下次 canary 前生效）。"""
    if not spec:
        return
    _CAPABILITY_CACHE[spec] = {
        "model": spec,
        "status": VISION_SCHEMA_INVALID,
        "detail": "运行期观察输出未通过 schema 校验",
        "version": VISION_CAPABILITY_STATUS_VERSION,
    }


def current_vision_status() -> dict[str, str]:
    spec = str(getattr(app_config, "vision_model_spec", "") or "")
    if spec and spec in _CAPABILITY_CACHE:
        return _CAPABILITY_CACHE[spec]
    return {
        "model": spec,
        "status": VISION_CONFIGURED if spec else VISION_NOT_CONFIGURED,
        "detail": "尚未执行 canary（配置存在 ≠ 能力可用）" if spec else "vision_model_spec 为空",
        "version": VISION_CAPABILITY_STATUS_VERSION,
    }


__all__ = [
    "OBSERVATION_PROMPT_VERSION",
    "VISION_CAPABILITY_STATUS_VERSION",
    "VISION_CONFIGURED",
    "VISION_IMAGE_UNREADABLE",
    "VISION_NOT_CONFIGURED",
    "VISION_PROVIDER_FAILED",
    "VISION_PROVIDER_VERSION",
    "VISION_READY",
    "VISION_SCHEMA_INVALID",
    "ChatModelVisionProvider",
    "NullVisionProvider",
    "current_vision_status",
    "get_vision_provider",
    "mark_observation_schema_invalid",
    "probe_vision_capability",
]
