"""自定义工具门面单测：契约校验、CRUD 生命周期、测连状态机、OpenAPI 导入、目录合并。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.agents.toolkits.custom import service as custom_tool_service
from yuxi.agents.toolkits.custom.domain import (
    CustomToolError,
    CustomToolLifecycle,
    CustomToolReferenceError,
)
from yuxi.storage.postgres.models_business import Agent, CustomTool, MCPUserCredential

TENANT_ID = 1
UID = "admin"

VALID_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "gene_id": {"type": "string", "description": "基因 ID"},
        "page": {"type": "integer", "description": "页码"},
    },
    "required": ["gene_id"],
}
VALID_SPEC = {
    "base_url": "https://api.example.com",
    "path": "/v1/genes/{{gene_id}}",
    "method": "GET",
    "headers": {},
    "query": {"page": "{{page}}"},
    "body_params": [],
    "timeout_s": 10,
}


@pytest_asyncio.fixture
async def custom_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(CustomTool.__table__.create)
        await conn.run_sync(Agent.__table__.create)
        await conn.run_sync(MCPUserCredential.__table__.create)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


async def _create_valid_tool(session, slug: str = "fetch_gene_info") -> dict:
    return await custom_tool_service.create_custom_tool(
        session,
        tenant_id=TENANT_ID,
        uid=UID,
        slug=slug,
        name="基因查询",
        description="按基因 ID 查询基因注释信息",
        spec=dict(VALID_SPEC),
        args_schema=dict(VALID_ARGS_SCHEMA),
    )


async def test_create_custom_tool_defaults_to_draft(custom_session):
    data = await _create_valid_tool(custom_session)
    assert data["slug"] == "fetch_gene_info"
    assert data["lifecycle_status"] == CustomToolLifecycle.DRAFT.value
    assert data["enabled"] is False
    assert data["spec"]["timeout_s"] == 10
    assert data["args_schema"]["required"] == ["gene_id"]


async def test_create_rejects_duplicate_slug(custom_session):
    await _create_valid_tool(custom_session)
    with pytest.raises(CustomToolError, match="已存在"):
        await _create_valid_tool(custom_session)


async def test_create_rejects_builtin_slug_conflict(custom_session):
    with pytest.raises(CustomToolError, match="内置工具冲突"):
        await _create_valid_tool(custom_session, slug="present_artifacts")


async def test_create_rejects_bad_slug_format(custom_session):
    with pytest.raises(CustomToolError, match="标识"):
        await _create_valid_tool(custom_session, slug="1Bad-Slug")


async def test_args_schema_validation_rules(custom_session):
    bad_schemas = [
        {"type": "object", "properties": {"a": {"type": "string", "$ref": "#/x"}}},
        {"type": "array"},
        {"type": "object", "properties": {name: {"type": "string"} for name in [f"p{i}" for i in range(20)]}},
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["missing"]},
        {"type": "object", "properties": {"a": {"type": "file"}}},
    ]
    for schema in bad_schemas:
        with pytest.raises(CustomToolError):
            await custom_tool_service.create_custom_tool(
                custom_session,
                tenant_id=TENANT_ID,
                uid=UID,
                slug="schema_check",
                name="x",
                description="d",
                spec=dict(VALID_SPEC),
                args_schema=schema,
            )


async def test_spec_validation_rules(custom_session):
    bad_specs = [
        # 非公网地址
        {**VALID_SPEC, "base_url": "https://127.0.0.1"},
        # 模板引用未定义参数
        {**VALID_SPEC, "path": "/v1/genes/{{not_defined}}"},
        # 参数未被任何模板引用
        {**VALID_SPEC, "query": {}},
        # 内联密钥
        {**VALID_SPEC, "headers": {"Authorization": "Bearer abc123"}},
        # GET 不允许 body
        {**VALID_SPEC, "body_params": ["page"]},
        # path 携带查询串
        {**VALID_SPEC, "path": "/v1/genes/{{gene_id}}?x=1"},
    ]
    for spec in bad_specs:
        with pytest.raises(CustomToolError):
            await custom_tool_service.create_custom_tool(
                custom_session,
                tenant_id=TENANT_ID,
                uid=UID,
                slug="spec_check",
                name="x",
                description="d",
                spec=spec,
                args_schema=dict(VALID_ARGS_SCHEMA),
            )


async def test_update_connection_resets_lifecycle(custom_session):
    await _create_valid_tool(custom_session)
    tool = await custom_session.scalar(select(CustomTool).where(CustomTool.slug == "fetch_gene_info"))
    tool.lifecycle_status = CustomToolLifecycle.READY.value
    tool.enabled = True
    await custom_session.commit()

    updated = await custom_tool_service.update_custom_tool(
        custom_session,
        tenant_id=TENANT_ID,
        uid=UID,
        slug="fetch_gene_info",
        fields={"name": "基因查询 V2"},
    )
    assert updated["name"] == "基因查询 V2"
    assert updated["lifecycle_status"] == CustomToolLifecycle.READY.value  # 未动连接配置

    updated = await custom_tool_service.update_custom_tool(
        custom_session,
        tenant_id=TENANT_ID,
        uid=UID,
        slug="fetch_gene_info",
        fields={"spec": {**VALID_SPEC, "timeout_s": 20}},
    )
    assert updated["lifecycle_status"] == CustomToolLifecycle.DRAFT.value
    assert updated["enabled"] is False


async def test_enable_requires_ready(custom_session):
    await _create_valid_tool(custom_session)
    with pytest.raises(CustomToolError, match="READY"):
        await custom_tool_service.set_custom_tool_enabled(
            custom_session, tenant_id=TENANT_ID, uid=UID, slug="fetch_gene_info", enabled=True
        )

    tool = await custom_session.scalar(select(CustomTool).where(CustomTool.slug == "fetch_gene_info"))
    tool.lifecycle_status = CustomToolLifecycle.READY.value
    await custom_session.commit()
    enabled = await custom_tool_service.set_custom_tool_enabled(
        custom_session, tenant_id=TENANT_ID, uid=UID, slug="fetch_gene_info", enabled=True
    )
    assert enabled["enabled"] is True


async def test_delete_blocked_by_agent_reference(custom_session):
    await _create_valid_tool(custom_session)
    custom_session.add(
        Agent(
            id=1,
            tenant_id=TENANT_ID,
            slug="lab-assistant",
            backend_id="chatbot",
            name="实验室助手",
            config_json={"context": {"tools": ["fetch_gene_info"]}},
        )
    )
    await custom_session.commit()

    with pytest.raises(CustomToolReferenceError, match="lab-assistant"):
        await custom_tool_service.delete_custom_tool(custom_session, tenant_id=TENANT_ID, slug="fetch_gene_info")

    agent = await custom_session.scalar(select(Agent).where(Agent.slug == "lab-assistant"))
    agent.config_json = {"context": {"tools": []}}
    await custom_session.commit()
    assert (
        await custom_tool_service.delete_custom_tool(custom_session, tenant_id=TENANT_ID, slug="fetch_gene_info")
        is True
    )


async def test_custom_tool_connection_state_machine(custom_session, monkeypatch):
    await _create_valid_tool(custom_session)

    async def fake_execute_200(record, args, *, client_factory=None):
        return {"status_code": 200, "text": "ok", "duration_ms": 12}

    monkeypatch.setattr("yuxi.agents.toolkits.custom.service.execute_custom_http_tool", fake_execute_200)
    result = await custom_tool_service.test_custom_tool(custom_session, tenant_id=TENANT_ID, slug="fetch_gene_info")
    assert result["lifecycle_status"] == CustomToolLifecycle.READY.value
    assert result["health"]["status_code"] == 200

    async def fake_execute_500(record, args, *, client_factory=None):
        return {"status_code": 500, "text": "boom", "duration_ms": 12}

    monkeypatch.setattr("yuxi.agents.toolkits.custom.service.execute_custom_http_tool", fake_execute_500)
    result = await custom_tool_service.test_custom_tool(custom_session, tenant_id=TENANT_ID, slug="fetch_gene_info")
    assert result["lifecycle_status"] == CustomToolLifecycle.FAILED.value

    # 4xx 一律 FAILED：401 说明凭据失效，不允许「假健康」进入可启用状态
    async def fake_execute_401(record, args, *, client_factory=None):
        return {"status_code": 401, "text": "unauthorized", "duration_ms": 8}

    monkeypatch.setattr("yuxi.agents.toolkits.custom.service.execute_custom_http_tool", fake_execute_401)
    result = await custom_tool_service.test_custom_tool(custom_session, tenant_id=TENANT_ID, slug="fetch_gene_info")
    assert result["lifecycle_status"] == CustomToolLifecycle.FAILED.value
    assert "401" in result["health"]["error"]


async def test_import_openapi_tools_creates_drafts(custom_session):
    document = {
        "openapi": "3.0.0",
        "info": {"title": "Pets", "version": "1.0"},
        "paths": {
            "/pets": {
                "get": {
                    "operationId": "listPets",
                    "summary": "列出宠物",
                    "parameters": [
                        {"name": "page", "in": "query", "required": False, "schema": {"type": "integer"}},
                        {"name": "X-Token", "in": "header", "schema": {"type": "string"}},
                    ],
                },
                "post": {
                    "operationId": "createPet",
                    "summary": "创建宠物",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
                                    "required": ["name"],
                                }
                            }
                        }
                    },
                },
            }
        },
    }
    result = await custom_tool_service.import_openapi_tools(
        custom_session,
        tenant_id=TENANT_ID,
        uid=UID,
        document=document,
        base_url="https://pets.example.com",
        name_prefix="pets",
    )
    assert set(result["created"]) == {"pets_list_pets", "pets_create_pet"}

    list_tool = await custom_session.scalar(select(CustomTool).where(CustomTool.slug == "pets_list_pets"))
    assert list_tool.lifecycle_status == CustomToolLifecycle.DRAFT.value
    assert list_tool.enabled is False
    # header 参数不导入
    assert "X-Token" not in (list_tool.args_schema.get("properties") or {})
    assert "page" in list_tool.args_schema["properties"]
    assert list_tool.spec["query"] == {"page": "{{page}}"}

    post_tool = await custom_session.scalar(select(CustomTool).where(CustomTool.slug == "pets_create_pet"))
    assert sorted(post_tool.spec["body_params"]) == ["age", "name"]
    assert post_tool.args_schema["required"] == ["name"]


async def test_import_rejects_non_openapi_document(custom_session):
    with pytest.raises(CustomToolError):
        await custom_tool_service.import_openapi_tools(
            custom_session, tenant_id=TENANT_ID, uid=UID, document={"foo": 1}, base_url="https://x.example.com"
        )


async def test_merged_catalog_and_options(custom_session):
    await _create_valid_tool(custom_session)
    catalog = await custom_tool_service.build_merged_tool_catalog(custom_session, tenant_id=TENANT_ID)
    custom_entries = [item for item in catalog if item.get("source") == "custom"]
    builtin_entries = [item for item in catalog if item.get("source") != "custom"]
    assert [item["slug"] for item in custom_entries] == ["fetch_gene_info"]
    assert custom_entries[0]["category"] == "custom"
    assert custom_entries[0]["args"][0]["name"] == "gene_id"
    assert builtin_entries  # 内置目录仍然在

    options = await custom_tool_service.list_custom_tool_options(custom_session, tenant_id=TENANT_ID)
    assert [item["key"] for item in options] == ["fetch_gene_info"]

    only_custom = await custom_tool_service.build_merged_tool_catalog(
        custom_session, tenant_id=TENANT_ID, category="custom"
    )
    assert all(item.get("source") == "custom" for item in only_custom)


async def test_credential_must_be_active_in_tenant(custom_session):
    custom_session.add(
        MCPUserCredential(
            id=7,
            tenant_id=TENANT_ID,
            uid=UID,
            name="pets-key",
            auth_type="api_key",
            secret_ciphertext="enc.v1:fake",
            masked_hint="pk****key",
            status="active",
        )
    )
    await custom_session.commit()

    with pytest.raises(CustomToolError, match="凭据 999 不存在"):
        await custom_tool_service.create_custom_tool(
            custom_session,
            tenant_id=TENANT_ID,
            uid=UID,
            slug="with_credential",
            name="x",
            description="d",
            spec=dict(VALID_SPEC),
            args_schema=dict(VALID_ARGS_SCHEMA),
            credential_id=999,
        )

    created = await custom_tool_service.create_custom_tool(
        custom_session,
        tenant_id=TENANT_ID,
        uid=UID,
        slug="with_credential",
        name="x",
        description="d",
        spec=dict(VALID_SPEC),
        args_schema=dict(VALID_ARGS_SCHEMA),
        credential_id=7,
    )
    assert created["credential_id"] == 7


async def test_import_openapi_converts_path_placeholders(custom_session):
    """Petstore 风格：OpenAPI `{petId}` 必须转成平台运行时模板 `{{petId}}`，
    且导入结果能通过与手工新建相同的契约门禁（导入后可直接测连）。"""
    document = {
        "openapi": "3.0.0",
        "info": {"title": "Petstore", "version": "1.0"},
        "paths": {
            "/pets/{petId}": {
                "get": {
                    "operationId": "getPetById",
                    "summary": "按 ID 查询宠物",
                    "parameters": [{"name": "petId", "in": "path", "required": True, "schema": {"type": "string"}}],
                }
            },
            "/orders/{orderId}": {
                "delete": {
                    "operationId": "deleteOrder",
                    "summary": "删除订单",
                    # 故意不声明 orderId 参数：属性缺失 → 模板引用未定义参数 → 校验拒绝
                }
            },
        },
    }
    result = await custom_tool_service.import_openapi_tools(
        custom_session,
        tenant_id=TENANT_ID,
        uid=UID,
        document=document,
        base_url="https://petstore.example.com",
        name_prefix="pets",
    )
    assert result["created"] == ["pets_get_pet_by_id"]
    skipped_labels = {item["operation"]: item["reason"] for item in result["skipped"]}
    assert "DELETE /orders/{orderId}" in skipped_labels

    tool = await custom_session.scalar(select(CustomTool).where(CustomTool.slug == "pets_get_pet_by_id"))
    assert tool.spec["path"] == "/pets/{{petId}}"
    assert "petId" in tool.args_schema["required"]
    # 门禁自证：导入产物能通过正式校验（等价于手工新建路径的约束）
    cleaned_spec = custom_tool_service.validate_custom_spec(tool.spec, tool.args_schema)
    assert cleaned_spec["path"] == "/pets/{{petId}}"


async def test_import_rejects_undeclared_path_placeholder(custom_session):
    document = {
        "openapi": "3.0.0",
        "info": {"title": "X", "version": "1"},
        "paths": {
            "/items/{itemId}": {
                "get": {
                    "operationId": "getItem",
                    "summary": "查询",
                    "parameters": [
                        # 声明的是 otherId，路径占位是 itemId → 转换后残留 {itemId} → 拒导
                        {"name": "otherId", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                }
            }
        },
    }
    result = await custom_tool_service.import_openapi_tools(
        custom_session,
        tenant_id=TENANT_ID,
        uid=UID,
        document=document,
        base_url="https://x.example.com",
        name_prefix="x",
    )
    assert result["created"] == []
    assert len(result["skipped"]) == 1


class _AsyncSessionContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *_args):
        return False


async def test_runtime_resolution_without_identity_fails_closed(custom_session, monkeypatch):
    """无 McpExecutionContext 且无 uid：跳过自定义工具（fail-closed），绝不回落默认租户。"""
    from yuxi.storage.postgres import manager as postgres_manager

    monkeypatch.setattr(
        postgres_manager.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(custom_session)
    )
    tools = await custom_tool_service.load_custom_tools_for_runtime(["anything"])
    assert tools == []


async def test_runtime_resolution_loads_ready_enabled_only(custom_session, monkeypatch):
    from yuxi.agents.mcp.execution import (
        McpExecutionContext,
        reset_mcp_execution_context,
        set_mcp_execution_context,
    )
    from yuxi.storage.postgres import manager as postgres_manager

    await _create_valid_tool(custom_session)
    await _create_valid_tool(custom_session, slug="broken_tool")
    await _create_valid_tool(custom_session, slug="required_tool")
    ready = await custom_session.scalar(select(CustomTool).where(CustomTool.slug == "fetch_gene_info"))
    ready.lifecycle_status, ready.enabled = "READY", True
    broken = await custom_session.scalar(select(CustomTool).where(CustomTool.slug == "broken_tool"))
    broken.lifecycle_status, broken.enabled = "FAILED", False
    required = await custom_session.scalar(select(CustomTool).where(CustomTool.slug == "required_tool"))
    required.lifecycle_status, required.enabled, required.dependency_mode = "DRAFT", False, "REQUIRED"
    await custom_session.commit()

    monkeypatch.setattr(
        postgres_manager.pg_manager, "get_async_session_context", lambda: _AsyncSessionContext(custom_session)
    )
    token = set_mcp_execution_context(McpExecutionContext(tenant_id=TENANT_ID, uid=UID))
    try:
        tools = await custom_tool_service.load_custom_tools_for_runtime(
            ["fetch_gene_info", "broken_tool", "missing_tool"]
        )
        assert [tool.name for tool in tools] == ["fetch_gene_info"]
        assert tools[0].metadata["custom_tool"] == "fetch_gene_info"

        # REQUIRED 语义：未就绪的自定义工具让整轮装配 fail-closed（与 MCP 同语义）
        with pytest.raises(RuntimeError, match="required_tool"):
            await custom_tool_service.load_custom_tools_for_runtime(["required_tool"])
    finally:
        reset_mcp_execution_context(token)
