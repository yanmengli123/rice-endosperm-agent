"""导出 FastAPI OpenAPI 契约（单一真源工件）。

用法（容器内，cwd=/app）::

    docker compose exec api uv run --no-sync python test/tools/export_openapi.py [输出路径]

默认输出到 ``test/fixtures/agent_run_contract/openapi.json``，供跨端契约
检查与桌面端 CI 消费。OpenAPI 由 pydantic 模型推导，改模型后重新导出即可
与 APISIX 手写闭集 schema 做漂移比对（见 test_apisix_gateway_contract.py）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_OUTPUT = Path("test/fixtures/agent_run_contract/openapi.json")


def main() -> int:
    from server.main import app

    output = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    spec = app.openapi()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OpenAPI 已导出：{output}（{len(spec.get('paths', {}))} paths）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
