"""应用配置模块。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import tomli
import tomli_w
from pydantic import BaseModel, Field, PrivateAttr

from yuxi.config import cache as runtime_cache
from yuxi.config.ocr_registry import PROCESSOR_TYPES
from yuxi.utils.logging_config import logger

READONLY_CONFIG_FIELDS = frozenset({"save_dir"})
DEFAULT_OCR_ENGINE = "rapid_ocr"


def _get_available_ocr_engines() -> set[str]:
    return {"disable", *PROCESSOR_TYPES}


def _normalize_default_ocr_engine(value: Any) -> str:
    engine = str(value or "").strip() or DEFAULT_OCR_ENGINE
    if engine not in _get_available_ocr_engines():
        raise ValueError(f"不支持的默认 OCR 引擎: {engine}")
    return engine


class Config(BaseModel):
    """应用配置类。

    `save_dir` 只在启动时决定配置文件位置，运行时不可修改。管理员保存配置时先写
    `base.toml`，再把可运行时同步的字段写入 Redis 快照（`yuxi:runtime_config`）。
    其他进程通过 `start_runtime_sync()` 启动的后台线程周期性拉取该快照刷新内存值。
    """

    save_dir: str = Field(default="saves", description="保存目录", exclude=True)
    enable_content_guard: bool = Field(default=False, description="是否启用内容审查")
    enable_content_guard_llm: bool = Field(default=False, description="是否启用LLM内容审查")
    default_model: str = Field(
        default="siliconflow-cn:Pro/MiniMaxAI/MiniMax-M2.5",
        description="默认对话模型",
    )
    fast_model: str = Field(
        default="siliconflow-cn:Pro/MiniMaxAI/MiniMax-M2.5",
        description="快速响应模型",
    )
    embed_model: str = Field(
        default="siliconflow-cn:BAAI/bge-m3",
        description="默认 Embedding 模型",
    )
    reranker: str = Field(
        default="siliconflow-cn:BAAI/bge-reranker-v2-m3",
        description="默认 Re-Ranker 模型",
    )
    content_guard_llm_model: str = Field(
        default="siliconflow-cn:Pro/MiniMaxAI/MiniMax-M2.5",
        description="内容审查LLM模型",
    )
    vision_model_spec: str = Field(
        default="",
        description="科研图像观察视觉模型（多模态 chat spec；留空=视觉通道失败关闭，不做图片定位）",
    )
    figure_card_enabled: bool = Field(
        default=False,
        description="对话流图卡：是否随 citation_ready 发布论文原图（投影与审计恒开启，本开关只控制 SSE 发布）",
    )
    figure_ref_anchor_enabled: bool = Field(
        default=False,
        description=(
            "图表锚点（ADR-0008）：是否在回答正文签发〔图表F#〕锚点芯片并随 citation_ready 发布"
            " figure_refs（解析与审计恒开启，与 figure_card_enabled 分立）"
        ),
    )
    figure_semantic_gate_enabled: bool = Field(
        default=False,
        description=(
            "图表语义门禁【破坏性】：是否允许按条件/数值判据**删除**断言句。默认关闭。"
            "独立于 figure_ref_anchor_enabled——删句不可搭在渲染特性开关上"
            "（2026-09-27 事故：门禁绑在锚点开关上，且判据缺省时 fail-open 未实现，"
            "开关全开即删真实正文）。仅当条件/表格数据确实随调用传入时才可能删句；"
            "无数据时不判断。"
        ),
    )
    graph_card_enabled: bool = Field(
        default=False,
        description="对话关系图：是否发布规范关系子图（投影与审计恒开启，本开关只控制 SSE 与消息附件）",
    )
    panel_locator_enabled: bool = Field(
        default=True,
        description=(
            "Panel 级定位精度（ADR-0008 P1，灰度开关）：关闭后锚点芯片不再按"
            " panel 精确匹配图资产，panel_status 降级 WHOLE_FIGURE_FALLBACK/"
            "CAPTION_FALLBACK（整图卡或题注卡照常发布）。只降精度不撤通道；"
            "物理定位服务与历史已存载荷不受影响。"
        ),
    )
    table_cell_enabled: bool = Field(
        default=True,
        description=(
            "表格卡片与单元格定位通道（ADR-0008 P2，灰度开关）：关闭后 tables[]"
            " 不再随 citation_ready 发布，Table 锚点芯片 suppressed_reason="
            "table_channel_disabled、visual_status 回落题注/仅引用。解析与审计恒"
            "开启；历史已存载荷不受影响。"
        ),
    )
    default_ocr_engine: str = Field(default=DEFAULT_OCR_ENGINE, description="默认 OCR 解析引擎")
    literature_deepread_enabled: bool = Field(
        default=True,
        description=(
            "文献深读增强（默认开启，新运行冻结开关）：paper 文献专属会话注入单篇"
            "文档结构地图（Navigation 平面）与文献深读作答策略（结论先行/分域/证据不足"
            "明说）。闸门信号为 scope_snapshot 携带 literature_binding（当前仅 paper 会话"
            "写入该绑定，global-qa 等其他 kind 不生效）；普通科研问答行为逐字节不变。地图"
            "不进证据通道，深读策略不豁免定位硬约束与 source_guard 门禁；门禁指标回退时"
            "可关闭新运行增强（含章节标签），恢复沿用原快照；正在执行的运行可取消。生产灰度建议先置 False 逐步开启。"
        ),
    )
    literature_coreference_enabled: bool = Field(
        default=True,
        description=(
            "文献追问指代消解（默认开启，新运行冻结开关）：仅 paper 会话读取当前输入之前"
            "最近已完成用户轮次；仅补入最近单一实体，当前问题有明确实体或存在指代歧义时不改写。"
            "不改用户可见文本、不参与证据资格判定；关闭后新运行不启用，恢复沿用原快照。"
        ),
    )
    literature_rerank_enabled: bool = Field(
        default=False,
        description=(
            "文献证据重排（默认关闭，新运行冻结开关/模型/凭据引用）：仅 paper 会话通过治理 scorer "
            "调用供应商，沿用配额、BYOK、append-only 账本和调用前后文献权限/版本复验。"
            "单次最多 48 候选、单文档 4000 字符、供应商 3 秒；严格索引双射/数量/有限分数校验，"
            "不使用静默 0.5。供应商故障回退原序，来源权限或版本变化必须拒绝继续用证据。"
            "在证据契约与引用编号构建前排序；须完成质量评测后再开启。"
        ),
    )

    _config_file: Path | None = PrivateAttr(default=None)
    _runtime_sync_thread: Any = PrivateAttr(default=None)

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}

    def __init__(self, **data):
        super().__init__(**data)
        self._setup_paths()
        self._load_user_config()

    def _setup_paths(self) -> None:
        self._config_file = Path(self.save_dir) / "config" / "base.toml"
        self._config_file.parent.mkdir(parents=True, exist_ok=True)

    def _load_user_config(self) -> None:
        if not self._config_file or not self._config_file.exists():
            logger.info(f"Config file not found, using defaults: {self._config_file}")
            return

        logger.info(f"Loading config from {self._config_file}")
        try:
            with open(self._config_file, "rb") as f:
                user_config = tomli.load(f)

            for key, value in user_config.items():
                if key in READONLY_CONFIG_FIELDS:
                    logger.warning(f"Readonly config key ignored: {key}")
                elif key in type(self).model_fields:
                    try:
                        setattr(self, key, self._normalize_config_value(key, value))
                    except ValueError as exc:
                        logger.warning(f"Invalid config key ignored: {key} ({exc})")
                else:
                    logger.warning(f"Unknown config key: {key}")

        except Exception as e:
            logger.error(f"Failed to load config from {self._config_file}: {e}")

    def start_runtime_sync(self, interval: float = runtime_cache.RUNTIME_CONFIG_SYNC_INTERVAL_SECONDS) -> None:
        """启动后台线程周期性从 Redis 同步运行时配置。多次调用仅启动一次。"""
        self._runtime_sync_thread = runtime_cache.start_runtime_sync(
            self,
            self._runtime_sync_thread,
            interval=interval,
        )

    def refresh(self) -> None:
        """从 Redis 快照刷新公开配置字段到内存；Redis 不可用或无快照时保持当前值。"""
        runtime_cache.refresh_runtime_config(self)

    def save(self) -> None:
        if not self._config_file:
            logger.warning("Config file path not set")
            return

        logger.info(f"Saving config to {self._config_file}")
        user_modified = {}
        for field_name, field_info in type(self).model_fields.items():
            if field_info.exclude:
                continue
            current_value = getattr(self, field_name)
            if current_value != field_info.default:
                user_modified[field_name] = current_value

        try:
            with open(self._config_file, "wb") as f:
                tomli_w.dump(user_modified, f)
            logger.info(f"Config saved to {self._config_file}")
            runtime_cache.save_runtime_config(self)
        except Exception as e:
            logger.error(f"Failed to save config to {self._config_file}: {e}")

    def dump_config(self) -> dict[str, Any]:
        config_dict = self.model_dump()
        fields_info = {}
        for field_name, field_info in Config.model_fields.items():
            if field_info.exclude:
                continue
            fields_info[field_name] = {
                "des": field_info.description,
                "default": field_info.default,
                "type": field_info.annotation.__name__
                if hasattr(field_info.annotation, "__name__")
                else str(field_info.annotation),
                "exclude": field_info.exclude if hasattr(field_info, "exclude") else False,
            }
        config_dict["_config_items"] = fields_info
        return config_dict

    def update(self, other: dict[str, Any]) -> None:
        for key, value in other.items():
            if self.can_update(key):
                self.set_value(key, value)
            elif key in READONLY_CONFIG_FIELDS:
                logger.warning(f"Readonly config key ignored: {key}")
            else:
                logger.warning(f"Unknown config key: {key}")

    def can_update(self, key: object) -> bool:
        return isinstance(key, str) and key in type(self).model_fields and key not in READONLY_CONFIG_FIELDS

    def set_value(self, key: str, value: Any) -> None:
        if not self.can_update(key):
            raise ValueError(f"配置项不可修改: {key}")
        setattr(self, key, self._normalize_config_value(key, value))

    def _normalize_config_value(self, key: str, value: Any) -> Any:
        if key == "default_ocr_engine":
            return _normalize_default_ocr_engine(value)
        return value


config = Config()
