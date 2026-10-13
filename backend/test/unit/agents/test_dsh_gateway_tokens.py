"""dsh LLM 网关 run 级令牌的单元测试（签发/校验/防篡改/过期/Bearer 提取）。"""

import time

import pytest

from yuxi.agents.buildin.dsh.gateway_tokens import (
    GatewayTokenError,
    bearer_token_from_authorization,
    mint_gateway_token,
    verify_gateway_token,
)


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    # 单元测试不依赖部署环境的主密钥配置，直接注入测试密钥（≥32 字符）。
    monkeypatch.setenv("YUXI_SECRET_MASTER_KEY", "unit-test-master-key-0123456789abcdef")
    yield


def test_mint_and_verify_roundtrip():
    token = mint_gateway_token("run-123", execution_id="execution-1", ttl_seconds=600)
    assert token.startswith("yxdsh_")
    claims = verify_gateway_token(token)
    assert claims["run_id"] == "run-123"
    assert claims["execution_id"] == "execution-1"
    assert claims["exp"] > int(time.time())


def test_tampered_token_rejected():
    token = mint_gateway_token("run-123", execution_id="execution-1", ttl_seconds=600)
    payload, _, signature = token.rpartition(".")
    with pytest.raises(GatewayTokenError):
        verify_gateway_token(f"{payload}.deadbeef")


def test_expired_token_rejected(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    token = mint_gateway_token("run-123", execution_id="execution-1", ttl_seconds=1)
    monkeypatch.setattr(time, "time", lambda: now + 1)
    with pytest.raises(GatewayTokenError, match="过期"):
        verify_gateway_token(token)


def test_non_gateway_token_rejected():
    with pytest.raises(GatewayTokenError):
        verify_gateway_token("yxkey_something")
    with pytest.raises(GatewayTokenError):
        verify_gateway_token(None)


def test_bearer_extraction_only_accepts_gateway_prefix():
    assert bearer_token_from_authorization("Bearer yxdsh_abc.def").startswith("yxdsh_")
    assert bearer_token_from_authorization("Bearer yxkey_real") is None
    assert bearer_token_from_authorization(None) is None
    assert bearer_token_from_authorization("Basic yxdsh_x") is None
