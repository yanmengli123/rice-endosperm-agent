"""防腐层守卫（ADR-0010 不变量 L0-1 的 CI 断言）。

对标 PaperAgent ``backend/tests/test_doc_name_source.py`` 的守卫风格：
不变量不靠 review 记忆，靠测试变红。半年后有人图省事
``from paperparse.api import ...`` 直接进 ``knowledge/``，这里会先红。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yuxi.literature.boundaries import (
    EVIDENCE_DENIED_PRODUCT_TYPES,
    FORBIDDEN_UPSTREAM_IMPORTS,
    UPSTREAM_GATEWAY_PACKAGE,
    BoundaryViolationError,
    assert_dependency_direction,
    assert_no_upstream_leak,
    assert_not_evidence_capable,
    find_forbidden_imports,
)

pytestmark = pytest.mark.unit


def _yuxi_package_root() -> Path:
    """以已装载模块锚定源码根（宿主机与容器内布局均成立）。"""
    import yuxi.knowledge.products.registry as registry_module

    # .../package/yuxi/knowledge/products/registry.py → parents[2] = .../package/yuxi
    return Path(registry_module.__file__).resolve().parents[2]


def _backend_root() -> Path:
    # package/yuxi → package → backend（宿主机）或 /app（容器）
    return _yuxi_package_root().parents[1]


def _iter_modules(root: Path, dotted_prefix: str):
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        dotted = dotted_prefix + "." + ".".join(rel.with_suffix("").parts)
        yield dotted, path


def _read_source(path: Path) -> str:
    # utf-8-sig：兼容带 BOM 的源文件（BOM 字符会让 ast.parse 失败）
    return path.read_text(encoding="utf-8-sig")


# --- 守卫自身的判定语义 ----------------------------------------------------------


def test_guard_detects_imports_not_comments():
    # 真实 import（各形态）命中
    assert find_forbidden_imports("import paperparse", FORBIDDEN_UPSTREAM_IMPORTS) == ["paperparse"]
    assert find_forbidden_imports("from paperkb.api import x", FORBIDDEN_UPSTREAM_IMPORTS) == ["paperkb"]
    assert find_forbidden_imports("import paperparse.api as pp\nimport paperlit", FORBIDDEN_UPSTREAM_IMPORTS) == [
        "paperlit",
        "paperparse",
    ]
    assert find_forbidden_imports("from paperparse.core import p14", FORBIDDEN_UPSTREAM_IMPORTS) == ["paperparse"]
    # 注释/字符串里提及包名不误报（ast 级 import 扫描，而非子串扫描）
    assert (
        find_forbidden_imports(
            "# paperparse 不可进 knowledge\nDOC = 'paperkb 只是规格来源（ADR-0010 决议 5b）'",
            FORBIDDEN_UPSTREAM_IMPORTS,
        )
        == []
    )
    # 包内相对导入不视为跨模块依赖
    assert find_forbidden_imports("from . import contracts", FORBIDDEN_UPSTREAM_IMPORTS) == []
    # 扫描目标必须是合法源码（语法错误显式失败，不允许静默跳过）
    with pytest.raises(BoundaryViolationError):
        find_forbidden_imports("def (", FORBIDDEN_UPSTREAM_IMPORTS)


def test_gateway_package_is_the_only_upstream_exemption():
    # 文献域内部（P1 起的 vendor 网关层）允许触碰上游
    assert_no_upstream_leak(f"{UPSTREAM_GATEWAY_PACKAGE}.parse_gateway", "import paperparse")
    # 域外一律禁止
    with pytest.raises(BoundaryViolationError, match="L0-1"):
        assert_no_upstream_leak("yuxi.knowledge.parser.mineru", "import paperparse")
    with pytest.raises(BoundaryViolationError, match="L0-1"):
        assert_no_upstream_leak("server.routers.literature_router", "from paperkb.api import compile_now")


# --- L0-1 全量源码扫描（T12，长期价值最高的一条） -------------------------------


def test_knowledge_and_server_never_import_paperagent_packages():
    scanned: set[str] = set()

    for dotted, path in _iter_modules(_yuxi_package_root(), "yuxi"):
        if dotted == UPSTREAM_GATEWAY_PACKAGE or dotted.startswith(UPSTREAM_GATEWAY_PACKAGE + "."):
            continue  # 文献域是唯一豁免区
        assert_no_upstream_leak(dotted, _read_source(path))
        scanned.add(dotted)

    server_root = _backend_root() / "server"
    assert server_root.is_dir(), f"server 源码根不存在: {server_root}"
    for dotted, path in _iter_modules(server_root, "server"):
        assert_no_upstream_leak(dotted, _read_source(path))
        scanned.add(dotted)

    # 守卫自身有效性：必须真的扫到了已知核心模块（防止根路径推导错误导致空转绿灯）
    assert "yuxi.knowledge.scope_gateway" in scanned
    assert "yuxi.knowledge.products.registry" in scanned
    assert any(name.startswith("server.routers.") for name in scanned)


def test_knowledge_does_not_reverse_depend_on_literature():
    # 依赖方向只允许 literature → knowledge；knowledge → literature 即架构倒挂
    scanned: set[str] = set()
    knowledge_root = _yuxi_package_root() / "knowledge"
    for dotted, path in _iter_modules(knowledge_root, "yuxi.knowledge"):
        assert_dependency_direction(dotted, _read_source(path))
        scanned.add(dotted)
    assert "yuxi.knowledge.scope_gateway" in scanned


# --- L0-3 派生产品断言 -----------------------------------------------------------


def test_evidence_denied_product_types_are_derived_and_others_fail_closed():
    for kb_type in EVIDENCE_DENIED_PRODUCT_TYPES:
        assert_not_evidence_capable(kb_type)  # literature_notes / llmwiki 通过
    with pytest.raises(BoundaryViolationError):
        assert_not_evidence_capable("milvus")  # 权威源不得冒充派生产品
    with pytest.raises(BoundaryViolationError):
        assert_not_evidence_capable("")  # 空 kb_type 在此 helper 下 fail-closed
    with pytest.raises(BoundaryViolationError):
        assert_not_evidence_capable("literature-notes-typo")  # 未登记类型 fail-closed
