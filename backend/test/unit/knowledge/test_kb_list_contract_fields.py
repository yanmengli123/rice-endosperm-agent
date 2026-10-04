"""列表端点的 Source Contract 字段回归测试。

背景缺陷：``KnowledgeBase._load_metadata`` 构建的 ``databases_meta`` 缓存未包含
契约冻结字段，而 ``manager.get_databases()`` 的列表路径正是读这份缓存。结果
``GET /api/knowledge/databases`` 返回的每条记录 ``contract_key`` 为空，前端按
契约过滤（如 ``/literature`` 只列 ``pdf_evidence`` 写入目标）永远得到空列表，
表现为「导入 PDF 契约处理」无可选知识源、无法上传。

契约：列表缓存与 ``manager.get_database_info()`` 的契约字段集必须一致；
``contract_snapshot`` 属大 JSON，刻意不进列表（前端走独立契约 API 获取）。
"""

from types import SimpleNamespace

import pytest

from yuxi.knowledge.base import KnowledgeBase

# 与 manager.get_database_info() 保持一致的契约字段（不含 contract_snapshot）
CONTRACT_FIELDS = (
    "contract_key",
    "contract_version",
    "contract_digest",
    "content_domain",
    "tool_description",
    "governance_status",
)


class FakeKnowledgeBase(KnowledgeBase):
    @property
    def kb_type(self) -> str:
        return "fake"

    async def _create_kb_instance(self, kb_id: str, config: dict):
        return None

    async def _initialize_kb_instance(self, instance) -> None:
        pass

    async def index_file(self, kb_id: str, file_id: str, operator_id: str | None = None) -> dict:
        return {}

    async def update_content(self, kb_id: str, file_ids: list[str], params: dict | None = None) -> list[dict]:
        return []

    async def aquery(self, query_text: str, kb_id: str, **kwargs) -> list[dict]:
        return []

    def get_query_params_config(self, kb_id: str, **kwargs) -> dict:
        return {"options": []}

    async def delete_file(self, kb_id: str, file_id: str) -> None:
        pass

    async def get_file_basic_info(self, kb_id: str, file_id: str) -> dict:
        return {}

    async def get_file_content(self, kb_id: str, file_id: str) -> dict:
        return {}

    async def get_file_info(self, kb_id: str, file_id: str) -> dict:
        return {}


def _kb_row(**overrides):
    data = {
        "kb_id": "kb_pdf_1",
        "kb_type": "fake",
        "name": "PDF 证据库",
        "description": None,
        "embedding_model_spec": None,
        "llm_model_spec": None,
        "query_params": None,
        "additional_params": None,
        "created_at": None,
        "contract_key": "pdf_evidence",
        "contract_version": "1.0.0",
        "contract_digest": "sha256:abc",
        "contract_snapshot": {"big": "payload"},
        "content_domain": "research",
        "tool_description": "文献证据",
        "governance_status": "PUBLISHED",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


class _StubRepo:
    def __init__(self, rows):
        self._rows = rows

    async def get_all(self):
        return list(self._rows)


@pytest.fixture
def fake_kb(monkeypatch, tmp_path):
    instance = FakeKnowledgeBase(str(tmp_path))
    instance._metadata_loaded = False
    instance.benchmarks_meta = {}
    return instance


@pytest.mark.asyncio
async def test_list_metadata_carries_contract_fields(fake_kb, monkeypatch):
    """列表缓存必须带契约字段——否则前端按 contract_key 过滤必然为空。"""
    import yuxi.repositories.knowledge_base_repository as repo_mod

    monkeypatch.setattr(repo_mod, "KnowledgeBaseRepository", lambda: _StubRepo([_kb_row()]))

    await fake_kb._load_metadata()

    meta = fake_kb.databases_meta["kb_pdf_1"]
    for field in CONTRACT_FIELDS:
        assert field in meta, f"列表元数据缺少契约字段 {field}"
    assert meta["contract_key"] == "pdf_evidence"
    assert meta["governance_status"] == "PUBLISHED"


@pytest.mark.asyncio
async def test_list_metadata_keeps_null_contract_fields_visible(fake_kb, monkeypatch):
    """存量未回填契约的库也要输出键（值为 None），保证前端过滤语义稳定。"""
    import yuxi.repositories.knowledge_base_repository as repo_mod

    row = _kb_row(contract_key=None, contract_version=None, governance_status=None)
    monkeypatch.setattr(repo_mod, "KnowledgeBaseRepository", lambda: _StubRepo([row]))

    await fake_kb._load_metadata()

    meta = fake_kb.databases_meta["kb_pdf_1"]
    assert "contract_key" in meta and meta["contract_key"] is None
    # governance_status 有 DRAFT 兜底，不能为 None
    assert meta["governance_status"] == "DRAFT"


@pytest.mark.asyncio
async def test_list_metadata_excludes_contract_snapshot(fake_kb, monkeypatch):
    """contract_snapshot 是大 JSON，列表刻意不输出以控制响应体积。"""
    import yuxi.repositories.knowledge_base_repository as repo_mod

    monkeypatch.setattr(repo_mod, "KnowledgeBaseRepository", lambda: _StubRepo([_kb_row()]))

    await fake_kb._load_metadata()

    assert "contract_snapshot" not in fake_kb.databases_meta["kb_pdf_1"]
