import httpx
import pytest

from yuxi.knowledge.connector_secrets import public_connector_info, submitted_feishu_params
from yuxi.knowledge.implementations.feishu import FeishuKB, UpstreamError
from yuxi.utils.secret_crypto import SECRET_UNCHANGED_MARKER, _master_key


@pytest.fixture
def feishu(monkeypatch):
    monkeypatch.setenv("YUXI_SECRET_MASTER_KEY", "feishu-test-key-at-least-thirty-two-characters-long")
    _master_key.cache_clear()
    kb = object.__new__(FeishuKB)
    kb.databases_meta = {
        "kb_f": {"metadata": submitted_feishu_params({"feishu_user_token": "private-token", "feishu_space_id": "123"})}
    }
    yield kb
    _master_key.cache_clear()


def test_credentials_are_sealed_before_persistence_and_redacted_from_responses(feishu):
    params = feishu.databases_meta["kb_f"]["metadata"]
    assert params["feishu_user_token"].startswith("enc.v1:")
    public = public_connector_info({"kb_type": "feishu", "metadata": params, "additional_params": params})
    assert public["metadata"]["feishu_user_token"] == SECRET_UNCHANGED_MARKER
    assert "feishu_secret_id" not in public["additional_params"]
    assert params["feishu_user_token"] != SECRET_UNCHANGED_MARKER
    updated = submitted_feishu_params({"feishu_user_token": SECRET_UNCHANGED_MARKER}, params)
    assert updated == params
    with pytest.raises(ValueError, match="加密"):
        submitted_feishu_params(params)
    assert feishu._connection("kb_f")["feishu_user_token"] == "private-token"


async def test_search_returns_titles_and_cannot_widen_the_space(feishu, monkeypatch):
    async def request(_client, connection, method, path, **kwargs):
        assert connection["feishu_user_token"] == "private-token"
        assert kwargs["json"] == {"query": "Wx", "space_id": "123"}
        return {
            "items": [{"space_id": "123", "obj_type": 8, "node_id": "node1", "title": "Wx methods"}],
            "has_more": False,
        }

    monkeypatch.setattr(feishu, "_request", request)
    results = await feishu.aquery("Wx", "kb_f")
    assert results[0]["metadata"]["content_role"] == "document_title"
    assert results[0]["content"] == "Wx methods"

    async def bad_space(*_args, **_kwargs):
        return {"items": [{"space_id": "999", "obj_type": 8, "node_id": "node2", "title": "Other space"}]}

    monkeypatch.setattr(feishu, "_request", bad_space)
    with pytest.raises(ValueError, match="超出"):
        await feishu.aquery("Wx", "kb_f")


async def test_search_rejects_repeated_pagination(feishu, monkeypatch):
    async def request(*_args, **_kwargs):
        return {"items": [], "has_more": True, "page_token": "same"}

    monkeypatch.setattr(feishu, "_request", request)
    with pytest.raises(ValueError, match="重复"):
        await feishu.aquery("Wx", "kb_f")


async def test_document_reads_recheck_node_and_return_line_windows(feishu, monkeypatch):
    calls = []

    async def request(_client, _connection, _method, path, **kwargs):
        calls.append(path)
        if path.endswith("get_node"):
            assert kwargs["params"]["token"] == "node1"
            return {"node": {"space_id": "123", "obj_type": "docx", "node_type": "origin", "obj_token": "doc1"}}
        return {"content": "one\nWx methods\nthree"}

    monkeypatch.setattr(feishu, "_request", request)
    opened = await feishu.open_file_content("kb_f", "node1", offset=1, limit=1)
    assert opened["start_line"] == 2 and opened["next_offset"] == 2
    found = await feishu.find_file_content("kb_f", "node1", ["Wx"])
    assert found["windows"][0]["matched_lines"] == [2]
    assert len(calls) == 4


@pytest.mark.parametrize(
    "node",
    [
        {"space_id": "999", "obj_type": "docx", "obj_token": "doc1"},
        {"space_id": "123", "obj_type": "docx", "obj_token": "doc1", "node_type": "shortcut"},
        {"space_id": "123", "obj_type": "sheet", "obj_token": "doc1"},
    ],
)
async def test_foreign_nodes_shortcuts_and_non_documents_are_rejected(feishu, monkeypatch, node):
    async def request(*_args, **_kwargs):
        return {"node": node}

    monkeypatch.setattr(feishu, "_request", request)
    with pytest.raises(ValueError):
        await feishu.open_file_content("kb_f", "node1")
    with pytest.raises(ValueError, match="ID 无效"):
        await feishu.open_file_content("kb_f", "../node")


async def test_http_errors_never_include_tokens_or_provider_message(feishu):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer private-token"
        return httpx.Response(200, json={"code": 999, "msg": "private-token should never leak"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UpstreamError) as error:
            await feishu._request(client, feishu._connection("kb_f"), "POST", "/wiki/v2/nodes/search")
    assert "private-token" not in str(error.value)
