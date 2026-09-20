# Vendored: ricekb_mcp.py

| Field | Value |
| --- | --- |
| Upstream | Rice Research Agent repository, `rice-kb-gateway/mcp/ricekb_mcp.py` |
| Gateway contract | `rice-source-envelope-v1.1`, gateway `2.2.0` |
| MCP server version | `1.1.0` |
| sha256 | `0be31f3f6d59a9a2ea1c8d9acda95b8e3876099ecd8388a01bfa222d84ce0aa6` |

`ricekb_mcp.py` is copied byte-for-byte from the upstream repository. Do not
edit it here; change it upstream, run its test suite
(`python -m unittest discover -s rice-kb-gateway/tests`), then re-copy and
update the hash above:

```powershell
Copy-Item 'D:\Rice‑Research Agent\rice-kb-gateway\mcp\ricekb_mcp.py' .\docker\mcp\ricekb\ricekb_mcp.py
Get-FileHash .\docker\mcp\ricekb\ricekb_mcp.py -Algorithm SHA256
```

`backend/test/unit/agents/test_ricekb_builtin.py` pins this hash as
`VENDORED_SHA256` and checks the installed `/usr/local/bin/ricekb-mcp` inside
the runtime image, so a stale or locally edited copy fails the unit suite.
Update the table above and that constant together.
