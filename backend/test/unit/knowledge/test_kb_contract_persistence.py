"""Source Contract 落库回归测试。

背景缺陷：_persist_kb 的 record_fields 白名单只放行 share_config/created_by，
契约字段（contract_key/version/digest/snapshot 等）被静默过滤；而 manager.create_database
仍把契约回显进响应，导致"建库响应带契约、库行空契约"，门禁只能回落 legacy 全命令。
"""

import types

from yuxi.knowledge.base import KnowledgeBase


class FakeKnowledgeBase(KnowledgeBase):
    @property
    def kb_type(self) -> str:
        return "fake"

    async def _create_kb_instance(self, slug: str, config: dict):
        return None

    async def _initialize_kb_instance(self, instance) -> None:
        pass

    async def index_file(self, slug: str, file_id: str, operator_id: str | None = None) -> dict:
        return {}

    async def update_content(self, slug: str, file_ids: list[str], params: dict | None = None) -> list[dict]:
        return []

    async def aquery(self, query_text: str, slug: str, **kwargs) -> list[dict]:
        return []

    def get_query_params_config(self, slug: str, **kwargs) -> dict:
        return {"options": []}

    async def delete_file(self, slug: str, file_id: str) -> None:
        pass

    async def get_file_basic_info(self, slug: str, file_id: str) -> dict:
        return {}

    async def get_file_content(self, slug: str, file_id: str) -> dict:
        return {}

    async def get_file_info(self, slug: str, file_id: str) -> dict:
        return {}

    async def _save_metadata(self) -> None:
        pass


CONTRACT_RECORD_FIELDS = {
    "contract_key": "csv_record",
    "contract_version": "1.0.0",
    "contract_digest": "sha256:deadbeef",
    "contract_snapshot": {"contract_key": "csv_record", "version": "1.0.0"},
    "content_domain": "水稻胚乳",
    "tool_description": None,
    "governance_status": "DRAFT",
}


async def test_create_database_persists_contract_fields(tmp_path, monkeypatch):
    created_payloads = []

    class FakeKnowledgeBaseRepository:
        async def get_by_kb_id(self, kb_id):
            return None

        async def create(self, payload):
            created_payloads.append(payload)
            return types.SimpleNamespace(**payload)

        async def update(self, kb_id, data):
            raise AssertionError("create_database should insert new database metadata")

    monkeypatch.setattr(
        "yuxi.repositories.knowledge_base_repository.KnowledgeBaseRepository",
        FakeKnowledgeBaseRepository,
    )

    kb = FakeKnowledgeBase(str(tmp_path))
    record_fields = {"share_config": {"access_level": "user"}, "created_by": "root", **CONTRACT_RECORD_FIELDS}

    await kb.create_database(
        "CSV 结构化数据集",
        "契约落库回归",
        embedding_model_spec="provider:embedding",
        record_fields=record_fields,
        auto_generate_questions=False,
    )

    assert len(created_payloads) == 1
    payload = created_payloads[0]
    for key, value in CONTRACT_RECORD_FIELDS.items():
        assert payload[key] == value, f"契约字段 {key} 未随首行落库"
    # 非白名单字段仍应被过滤
    assert "created_by" not in payload["additional_params"]


async def test_persist_kb_update_path_applies_contract_fields(tmp_path, monkeypatch):
    update_calls = []

    class FakeKnowledgeBaseRepository:
        async def get_by_kb_id(self, kb_id):
            return types.SimpleNamespace(kb_id=kb_id)

        async def create(self, payload):
            raise AssertionError("已存在的知识库不应走 create")

        async def update(self, kb_id, data):
            update_calls.append((kb_id, dict(data)))
            return types.SimpleNamespace(kb_id=kb_id)

    monkeypatch.setattr(
        "yuxi.repositories.knowledge_base_repository.KnowledgeBaseRepository",
        FakeKnowledgeBaseRepository,
    )

    kb = FakeKnowledgeBase(str(tmp_path))
    kb.databases_meta = {
        "kb_test": {
            "name": "词典库",
            "description": "",
            "kb_type": "fake",
            "embedding_model_spec": None,
            "llm_model_spec": None,
            "metadata": {"chunk_preset_id": "general"},
            "query_params": {"options": []},
        }
    }

    await kb._persist_kb(
        "kb_test",
        record_fields={"share_config": {"access_level": "user"}, "created_by": "root", **CONTRACT_RECORD_FIELDS},
    )

    assert len(update_calls) == 1
    data = update_calls[0][1]
    for key, value in CONTRACT_RECORD_FIELDS.items():
        assert data[key] == value, f"更新路径契约字段 {key} 丢失"


class _FakeManagerRepo:
    """manager 回读测试的仓储替身：rows 模拟 knowledge_bases 表行。

    nullify_contract_on_create 模拟持久化链路丢契约字段（回归前的白名单缺陷）：
    ORM 行上属性始终存在、值为 None，回读后响应应暴露空值而不是回显入参。
    """

    def __init__(self, nullify_contract_on_create: bool = False):
        self.rows: dict[str, types.SimpleNamespace] = {}
        self.nullify_contract_on_create = nullify_contract_on_create

    async def get_by_kb_id(self, kb_id):
        return self.rows.get(kb_id)

    async def create(self, payload):
        stored = dict(payload)
        if self.nullify_contract_on_create:
            for key in CONTRACT_RECORD_FIELDS:
                stored[key] = None
        row = types.SimpleNamespace(**stored)
        self.rows[payload["kb_id"]] = row
        return row


def _patch_manager_env(monkeypatch, shared_repo):
    class FakeKnowledgeBaseRepository:
        def __init__(self):
            self._delegate = shared_repo

        async def get_by_kb_id(self, kb_id):
            return await self._delegate.get_by_kb_id(kb_id)

        async def create(self, payload):
            return await self._delegate.create(payload)

    monkeypatch.setattr(
        "yuxi.repositories.knowledge_base_repository.KnowledgeBaseRepository",
        FakeKnowledgeBaseRepository,
    )

    class FakeFactory:
        @staticmethod
        def is_type_supported(kb_type):
            return True

        @staticmethod
        def get_available_types():
            return {"milvus": FakeKnowledgeBase}

    monkeypatch.setattr("yuxi.knowledge.manager.KnowledgeBaseFactory", FakeFactory)


async def _run_manager_create(tmp_path, shared_repo):
    from yuxi.knowledge.manager import KnowledgeBaseManager

    manager = KnowledgeBaseManager(str(tmp_path))

    async def fake_exists(name, *, uid=None):
        return False

    manager.database_name_exists = fake_exists
    kb = FakeKnowledgeBase(str(tmp_path))

    def fake_get_instance(kb_type):
        return kb

    manager._get_or_create_kb_instance = fake_get_instance

    return await manager.create_database(
        "响应回读回归库",
        "contract fields must come from the persisted row",
        kb_type="milvus",
        embedding_model_spec=None,
        llm_model_spec=None,
        share_config={"access_level": "user"},
        created_by="root",
        contract_fields=dict(CONTRACT_RECORD_FIELDS),
        auto_generate_questions=False,
    )


async def test_manager_create_response_matches_persisted_row(tmp_path, monkeypatch):
    shared = _FakeManagerRepo()
    _patch_manager_env(monkeypatch, shared)

    result = await _run_manager_create(tmp_path, shared)

    row = shared.rows[result["kb_id"]]
    for key, value in CONTRACT_RECORD_FIELDS.items():
        assert result[key] == value, f"响应契约字段 {key} 与入参不一致"
        assert getattr(row, key) == value, f"库行契约字段 {key} 与入参不一致"


async def test_manager_create_response_exposes_lost_contract_instead_of_echoing(tmp_path, monkeypatch):
    shared = _FakeManagerRepo(nullify_contract_on_create=True)
    _patch_manager_env(monkeypatch, shared)

    result = await _run_manager_create(tmp_path, shared)

    # 持久化丢失时响应必须暴露空值，而不是把入参契约回显成"假成功"
    assert result["contract_key"] is None
    assert result["contract_version"] is None
    assert result["governance_status"] is None
