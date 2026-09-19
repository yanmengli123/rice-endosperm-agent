"""仓储接口面测试：服务层调用的每一个仓储方法，在真实仓储类上必须存在。

事故背景（2026-09-19）：R1 把 6 个队列方法以 4 空格缩进追加到了模块级函数
``_triple_dict`` 的函数体之后——它们成了嵌套死代码，import 合法、类上无方法、
fake 仓储的单测全绿，直到前端点击队列才以 500 爆出。本测试用 AST 扫描服务源码
中 ``self.<repo>.<method>(...)`` 与 ``getattr(self.<repo>, "<method>", ...)`` 的
**真实调用点**，逐一断言真实仓储类/模块提供它们——fake 与真实接口的任何漂移
（漏写、改名、缩进嵌套错位）从此在单测阶段爆红，而不是等前端点出 500。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from yuxi.knowledge.graphs.doclex.service import DocLexService
from yuxi.knowledge.graphs.graph_review_service import GraphReviewService
from yuxi.knowledge.graphs.llm_graph_promotion import LLMGraphPromotionService
from yuxi.knowledge.graphs.milvus_graph_service import MilvusGraphService
from yuxi.repositories import knowledge_graph_repository as graph_repository_module
from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository
from yuxi.repositories.knowledge_chunk_repository import KnowledgeChunkRepository
from yuxi.repositories.knowledge_doclex_repository import (
    KnowledgeDoclexFigureRepository,
    KnowledgeDoclexRepository,
)
from yuxi.repositories.knowledge_graph_repository import KnowledgeGraphRepository
from yuxi.repositories.knowledge_graph_review_repository import KnowledgeGraphReviewRepository

_PACKAGE = Path(__file__).resolve().parents[3] / "package" / "yuxi"

# (服务源文件, 仓储属性名 → 真实仓储类)
_SURFACE = {
    "knowledge/graphs/graph_review_service.py": {
        "review_repo": KnowledgeGraphReviewRepository,
        "graph_repo": KnowledgeGraphRepository,
        "chunk_repo": KnowledgeChunkRepository,
        "kb_repo": KnowledgeBaseRepository,
    },
    "knowledge/graphs/milvus_graph_service.py": {
        "review_repo": KnowledgeGraphReviewRepository,
        "graph_repo": KnowledgeGraphRepository,
        "chunk_repo": KnowledgeChunkRepository,
        "kb_repo": KnowledgeBaseRepository,
    },
    "knowledge/graphs/doclex/service.py": {
        "repo": KnowledgeDoclexRepository,
        "graph_repo": KnowledgeGraphRepository,
        "figure_repo": KnowledgeDoclexFigureRepository,
    },
    "knowledge/graphs/llm_graph_promotion.py": {
        "graph_repo": KnowledgeGraphRepository,
    },
}


def _collect_repo_calls(source: str, repo_attrs: set[str]) -> dict[str, set[str]]:
    """AST 收集 self.<repo_attr>.<method>(...) 与 getattr(self.<repo_attr>, "<method>") 调用点。"""
    calls: dict[str, set[str]] = {}
    tree = ast.parse(source)

    def record(attr: str, method: str) -> None:
        calls.setdefault(attr, set()).add(method)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # self.<repo>.<method>(...)
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Attribute)
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "self"
            and func.value.attr in repo_attrs
        ):
            record(func.value.attr, func.attr)
            continue
        # getattr(self.<repo>, "<method>", ...) —— 可选能力防御调用，真实类仍必须提供
        if (
            isinstance(func, ast.Name)
            and func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Attribute)
            and isinstance(node.args[0].value, ast.Name)
            and node.args[0].value.id == "self"
            and node.args[0].attr in repo_attrs
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            record(node.args[0].attr, node.args[1].value)
    return calls


def _surface_calls() -> list[tuple[str, str, object, str]]:
    """[(文件, 仓储属性, 真实仓储类, 方法名)] 的全量清单。"""
    entries: list[tuple[str, str, object, str]] = []
    for relative_path, attr_to_class in _SURFACE.items():
        source = (_PACKAGE / relative_path).read_text(encoding="utf-8")
        for attr, methods in _collect_repo_calls(source, set(attr_to_class)).items():
            for method in sorted(methods):
                entries.append((relative_path, attr, attr_to_class[attr], method))
    assert entries, "AST 扫描不应为空：服务层至少存在一处仓储调用"
    return entries


# 显式关键清单（双保险）：即使扫描逻辑或文件布局变化，核心方法仍被钉死
_CRITICAL_SURFACE = [
    (KnowledgeGraphReviewRepository, "list_gate_reviews"),
    (KnowledgeGraphReviewRepository, "get_gate_review"),
    (KnowledgeGraphReviewRepository, "resolve_gate_review_row"),
    (KnowledgeGraphReviewRepository, "list_conflicts"),
    (KnowledgeGraphReviewRepository, "get_conflict"),
    (KnowledgeGraphReviewRepository, "resolve_conflict_row"),
    (KnowledgeGraphReviewRepository, "list_promoted_aliases"),
    (KnowledgeChunkRepository, "record_graph_attempt"),
    (KnowledgeChunkRepository, "revive_graph_chunks"),
    (KnowledgeChunkRepository, "count_graph_dead_by_kb_id"),
    (KnowledgeChunkRepository, "count_stale_graph_cache_by_kb_id"),
    (KnowledgeGraphRepository, "aggregate_hallucination_rate"),
    (KnowledgeGraphRepository, "register_conflicts"),
    (KnowledgeDoclexFigureRepository, "replace_figure_mentions"),
    (KnowledgeDoclexFigureRepository, "figure_mentions_for_chunks"),
]

# 模块级函数（服务层 from-import 直调，AST 属性扫描覆盖不到）
_CRITICAL_MODULE_FUNCTIONS = [
    (graph_repository_module, "upsert_gate_reviews"),
    (graph_repository_module, "refresh_triple_conflict_statement"),
]


def test_all_service_repo_calls_exist_on_real_repositories():
    failures = []
    for relative_path, attr, repo_class, method in _surface_calls():
        if not hasattr(repo_class, method):
            failures.append(f"{relative_path}: self.{attr}.{method}() 缺失于 {repo_class.__name__}")
    assert not failures, "服务层调用的仓储方法在真实仓储上不存在（fake 漂移）：\n" + "\n".join(failures)


def test_critical_surface_methods_exist():
    for repo_class, method in _CRITICAL_SURFACE:
        assert hasattr(repo_class, method), f"{repo_class.__name__}.{method} 缺失"


def test_critical_module_functions_exist():
    for module, function in _CRITICAL_MODULE_FUNCTIONS:
        assert callable(getattr(module, function, None)), f"{module.__name__}.{function} 缺失"


@pytest.mark.parametrize(
    "service_class",
    [GraphReviewService, MilvusGraphService, DocLexService, LLMGraphPromotionService],
)
def test_service_classes_instantiable_without_io(service_class):
    """服务类构造必须零 I/O（无 DB 时 import/构造不炸——队列路由依赖这一点）。"""
    assert service_class() is not None


def test_queue_methods_attached_to_class_not_nested():
    """事故回归钉子：AST 确认队列方法真实挂在 KnowledgeGraphReviewRepository 类体
    （而不是任何函数体内的嵌套定义）。"""
    source = (_PACKAGE / "repositories/knowledge_graph_review_repository.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nested = [item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
            leaked = [name for name in nested if name in {"list_gate_reviews", "list_conflicts"}]
            assert not leaked, f"队列方法嵌套在 {node.name} 体内：{leaked}"
