"""Run upstream MCP with a narrow accommodation for synthetic proxy DNS.

This installation's transparent outbound proxy resolves the official NCBI
E-utilities host into RFC 2544 benchmarking addresses (198.18.0.0/15).
Upstream correctly blocks non-public IPs to prevent SSRF.  Permit only this
exact HTTPS host when *all* DNS answers are in that synthetic range.  All
other hosts and addresses keep upstream's original guard, including redirects
and record-supplied URLs.  Do not use the upstream global private-egress flag.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from data_aggregator_mcp import egress
from data_aggregator_mcp.errors import ValidationError

_SYNTHETIC_PROXY_RANGE = ipaddress.ip_network("198.18.0.0/15")
_APPROVED_HOST = "eutils.ncbi.nlm.nih.gov"
_original_assert_public_url = egress.assert_public_url


async def _assert_public_or_ncbi_proxy(url: str, *, what: str) -> None:
    try:
        await _original_assert_public_url(url, what=what)
    except ValidationError:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != _APPROVED_HOST
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise
        loop = asyncio.get_running_loop()
        try:
            addresses = await loop.getaddrinfo(_APPROVED_HOST, 443, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            raise
        if not addresses or any(
            ipaddress.ip_address(address[4][0]) not in _SYNTHETIC_PROXY_RANGE for address in addresses
        ):
            raise


egress.assert_public_url = _assert_public_or_ncbi_proxy

if __name__ == "__main__":
    from data_aggregator_mcp.server import main

    main()
