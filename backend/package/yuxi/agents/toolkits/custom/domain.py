"""自定义工具领域常量与生命周期。

与 MCP 的 11 态生命周期不同，HTTP 定义工具没有构建/部署阶段，
只需要「草稿 → 测连通过 → 可启用」三态；连接配置一旦变更即回 DRAFT。
"""

from __future__ import annotations

import re
from enum import StrEnum

SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
ARG_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
TEMPLATE_REF_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

MAX_ARGS_PROPERTIES = 16
MAX_TIMEOUT_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_TEXT_LENGTH = 4000
MIN_MAX_TEXT_LENGTH = 200
MAX_MAX_TEXT_LENGTH = 50000
MAX_IMPORT_OPERATIONS = 20


class CustomToolLifecycle(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    FAILED = "FAILED"


CUSTOM_TOOL_TYPES = ("http",)
CUSTOM_TOOL_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
CUSTOM_TOOL_DATA_ACCESS_LEVELS = ("PUBLIC", "INTERNAL", "CONTROLLED", "HUMAN_SENSITIVE")
CUSTOM_TOOL_DEPENDENCY_MODES = ("OPTIONAL", "REQUIRED", "AUTHORITATIVE")
CUSTOM_TOOL_ARG_TYPES = ("string", "integer", "number", "boolean", "array")


class CustomToolError(ValueError):
    """自定义工具契约/策略校验失败（路由映射 400）。"""


class CustomToolReferenceError(ValueError):
    """删除被智能体引用的工具（路由映射 409）。"""


__all__ = [
    "ARG_NAME_PATTERN",
    "CustomToolError",
    "CustomToolLifecycle",
    "CustomToolReferenceError",
    "SLUG_PATTERN",
    "TEMPLATE_REF_PATTERN",
    "CUSTOM_TOOL_ARG_TYPES",
    "CUSTOM_TOOL_DATA_ACCESS_LEVELS",
    "CUSTOM_TOOL_DEPENDENCY_MODES",
    "CUSTOM_TOOL_HTTP_METHODS",
    "CUSTOM_TOOL_TYPES",
    "DEFAULT_MAX_TEXT_LENGTH",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_ARGS_PROPERTIES",
    "MAX_IMPORT_OPERATIONS",
    "MAX_MAX_TEXT_LENGTH",
    "MAX_TIMEOUT_SECONDS",
    "MIN_MAX_TEXT_LENGTH",
]
