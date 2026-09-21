"""审计日志服务单测：代理链下的真实来源 IP 解析。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from yuxi.services.operation_log_service import _client_ip

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _request(headers: dict[str, str] | None = None, host: str = "10.0.0.9") -> SimpleNamespace:
    return SimpleNamespace(headers=headers or {}, client=SimpleNamespace(host=host))


async def test_client_ip_prefers_first_forwarded_hop():
    request = _request({"x-forwarded-for": "203.0.113.7, 172.21.0.3"}, host="172.21.0.3")
    assert _client_ip(request) == "203.0.113.7"


async def test_client_ip_falls_back_to_direct_host_without_proxy_header():
    assert _client_ip(_request(host="192.168.1.4")) == "192.168.1.4"
    assert _client_ip(_request({"x-forwarded-for": "  "}, host="192.168.1.4")) == "192.168.1.4"


async def test_client_ip_handles_missing_request_or_client():
    assert _client_ip(None) is None
    assert _client_ip(SimpleNamespace(headers={}, client=None)) is None
