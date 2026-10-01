"""防腐层契约：文献域允许触碰什么、禁止触碰什么（ADR-0010 不变量 L0-1）。

把「禁止事项」变成可断言的常量 + 单一判定入口，供 CI 守卫测试
（``test/unit/literature/test_boundaries.py``）调用。检查基于 **ast 的
import 语句扫描**——注释/文档字符串里提及 PaperAgent 包名不误报。
"""

from __future__ import annotations

import ast

#: PaperAgent 三个算法包的顶层名。P1 起仅 ``yuxi/literature/**`` 允许 import
#: （paperparse 锁版本 vendor 作阅读渲染引擎；paperkb/paperlit 永不作为
#: 运行时依赖——ADR-0010 决议 5b）。
FORBIDDEN_UPSTREAM_IMPORTS: tuple[str, ...] = ("paperparse", "paperkb", "paperlit")

#: 允许触碰上游供应商代码的唯一包前缀。
UPSTREAM_GATEWAY_PACKAGE = "yuxi.literature"

#: 证据通道黑名单产品类型（派生知识产品；门禁本体在 products 注册中心，
#: 此常量供守卫测试断言黑名单不漂移）。
EVIDENCE_DENIED_PRODUCT_TYPES: tuple[str, ...] = ("literature_notes", "llmwiki")


class BoundaryViolationError(ValueError):
    """违反文献域防腐层不变量（L0-1 / L0-3）。"""


def find_forbidden_imports(source: str, forbidden_roots: tuple[str, ...] | list[str]) -> list[str]:
    """解析源码，返回其中 import 到的禁止模块根列表（无违例返回空）。

    匹配语义：import 的模块等于禁止根，或以其为前缀
    （``forbidden=("paperparse",)`` 时 ``import paperparse.api`` 命中；
    ``forbidden=("yuxi.literature",)`` 时 ``from yuxi.literature.contracts import X`` 命中）。
    相对导入（``from . import x``）不视为跨模块依赖。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # 扫描目标必须是可解析的合法源码
        raise BoundaryViolationError(f"源码解析失败（无法完成防腐守卫扫描）: {exc}") from exc

    violations: set[str] = set()

    def _check(module_name: str) -> None:
        for root in forbidden_roots:
            if module_name == root or module_name.startswith(root + "."):
                violations.add(root)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # 包内相对导入
            if node.module:
                _check(node.module)
    return sorted(violations)


def _is_within(module_path: str, package: str) -> bool:
    """module_path 是否位于 package 包内（精确匹配或以其为点分前缀）。"""
    return module_path == package or module_path.startswith(package + ".")


def assert_no_upstream_leak(module_path: str, source: str) -> None:
    """守卫（L0-1）：非文献域模块不得 import PaperAgent 三个包。

    ``module_path`` 为点分模块路径；``yuxi.literature.**`` 是唯一豁免区
    （UPSTREAM_GATEWAY_PACKAGE）。
    """
    if _is_within(module_path, UPSTREAM_GATEWAY_PACKAGE):
        return
    violations = find_forbidden_imports(source, FORBIDDEN_UPSTREAM_IMPORTS)
    if violations:
        raise BoundaryViolationError(
            f"{module_path} import 了 PaperAgent 供应商包 {violations}——"
            f"上游代码只允许经 {UPSTREAM_GATEWAY_PACKAGE}.** 依赖（ADR-0010 L0-1）"
        )


def assert_dependency_direction(module_path: str, source: str) -> None:
    """守卫（L0-1 依赖方向）：``yuxi.knowledge.**`` 不得反向依赖 ``yuxi.literature``。

    知识域是文献域的下层（文献域复用产品注册中心/证据契约），反向依赖即架构倒挂。
    """
    if not _is_within(module_path, "yuxi.knowledge"):
        return
    violations = find_forbidden_imports(source, ("yuxi.literature",))
    if violations:
        raise BoundaryViolationError(
            f"{module_path} 反向依赖了 yuxi.literature——依赖方向只允许 literature → knowledge（ADR-0010 L0-1）"
        )


def assert_not_evidence_capable(kb_type: str) -> None:
    """守卫（L0-3）：断言 kb_type 属派生产品种类（不得具备证据能力），失败即抛。

    与 ``require_capability`` 同风格；判定本体走产品注册中心
    （空 kb_type 在此 helper 下 fail-closed——空值映射到权威源，故同样抛错）。
    """
    from yuxi.knowledge.products.registry import get_product_spec

    spec = get_product_spec(kb_type)
    if spec.category != "derived_product":
        raise BoundaryViolationError(
            f"kb_type={kb_type!r} 不是派生产品（category={spec.category}），"
            f"不得用于文献笔记类派生产品断言（ADR-0010 L0-3）"
        )
