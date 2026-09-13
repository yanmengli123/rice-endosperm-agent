"""科研视觉定位子系统：观察契约（Observation）+ 感知哈希 + 图片定位裁决。

Authority 永远不在视觉模型手里——页码/文献/anchor 只由确定性裁决产生。
"""

from yuxi.knowledge.vision.figure_image_locator import resolve_figure_image_locator
from yuxi.knowledge.vision.visual_observation import VisualObservationEnvelope

__all__ = ["VisualObservationEnvelope", "resolve_figure_image_locator"]
