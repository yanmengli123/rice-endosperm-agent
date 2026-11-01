"""网页工具运行时接线：注册元数据、供应商分派、失败关闭与凭据感知装配。

管理端 HTTP 测试（test_v08_operations_http）已 mock search_web 验证配置面；
本文件补齐 agent 工具注册 → 服务分派 → 供应商适配这条真实执行链的单元边界。
"""

from __future__ import annotations

import pytest

from yuxi.agents.toolkits.buildin import web_tools as web_tool_module
from yuxi.agents.toolkits.registry import get_all_extra_metadata
from yuxi.services.web_tools.errors import InvalidInputError
from yuxi.services.web_tools.schemas import FetchInput, SearchInput
from yuxi.services.web_tools import service as web_service


def _configuration(*, search="deepseek", fetch=None, keys=None):
    return {"search": search, "fetch": fetch, "keys": keys or {}, "stored": {},
            "search_selection": search, "fetch_selection": fetch}


def _patch_configuration(monkeypatch, config):
    async def fake_read_configuration(db=None):
        return config

    monkeypatch.setattr(web_service, "read_configuration", fake_read_configuration)


async def _fake_post(payloads):
    async def post(provider, operation, key, payload):
        payloads.append((provider, operation, key, payload))
        return {"results": [{"title": "水稻基因图位克隆研究", "url": "https://example.org/rice-gene",
                             "summary": "相关摘要", "content": "完整正文片段",
                             "rerank_score": 2.1, "date": "2026-10-30"}]}
    return post


def test_web_tools_registered_as_buildin_with_schemas():
    """两个网页工具进入工具注册表，携带 buildin 元数据与输入 schema。"""
    extras = get_all_extra_metadata()
    for name in ("web_search", "web_fetch"):
        assert name in extras, f"{name} 未注册进工具元数据表"
        assert extras[name].category == "buildin"
    assert extras["web_search"].display_name == "网络搜索"


async def test_search_web_dispatches_to_configured_provider(monkeypatch):
    """按管理员配置分派供应商，结果走统一归一化且默认不携带正文。"""
    _patch_configuration(monkeypatch, _configuration(keys={"deepseek": "synthetic-key"}))
    seen = []
    monkeypatch.setattr(web_service, "_post", await _fake_post(seen))
    result = await web_service.search_web(SearchInput(query="水稻 图位克隆"))
    assert result["provider"] == "deepseek"
    assert result["results"][0]["title"] == "水稻基因图位克隆研究"
    assert result["results"][0]["score_max"] == 3
    assert result["results"][0]["raw_content"] == ""
    provider, operation, key, payload = seen[0]
    assert (provider, operation, key) == ("deepseek", "search", "synthetic-key")
    assert payload == {"query": "水稻 图位克隆", "count": 10, "need_content": False}


async def test_fetch_rejects_provider_without_fetch_support(monkeypatch):
    """目录未声明正文能力的供应商在分派前被拒绝，不发起请求。"""
    _patch_configuration(monkeypatch, _configuration())
    with pytest.raises(InvalidInputError, match="不支持此网页工具"):
        await web_service.fetch_web(FetchInput(url="https://example.org/page"),
                                    provider="doubao", api_key="synthetic-key")


async def test_unconfigured_tools_fail_closed_with_neutral_error(monkeypatch):
    """未配置凭据时服务层抛中性错误；工具层按注册表约定转成给模型的提示串。"""
    _patch_configuration(monkeypatch, _configuration(search=None, fetch=None, keys={}))
    with pytest.raises(InvalidInputError) as service_error:
        await web_service.search_web(SearchInput(query="anything"))
    assert service_error.value.code == "web_credentials_missing"
    outcome = await web_tool_module.web_search.ainvoke({"query": "anything"})
    assert isinstance(outcome, str) and "网页工具未配置" in outcome


async def test_available_web_tools_follow_credentials(monkeypatch):
    """装配集合只包含既有凭据支撑的操作，未配置的操作不进入本轮能力。"""
    _patch_configuration(monkeypatch, _configuration(search="deepseek", fetch=None, keys={"deepseek": "k"}))
    assert await web_service.available_web_tools() == {"web_search"}
    _patch_configuration(monkeypatch, _configuration(search="tavily", fetch="tavily", keys={"tavily": "k"}))
    assert await web_service.available_web_tools() == {"web_search", "web_fetch"}
