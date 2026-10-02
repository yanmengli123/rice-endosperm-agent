"""文献域管线 e2e：种子 → 阅读渲染（离线 paperparse）→ L1/L2 编译 → 任务投影与资产代理。

覆盖 ADR-0010 决议 10/15（自动登记后的显式重渲染）与编译手动端点。
种子经 ``test/e2e/literature_seed.py`` 直写 DB/MinIO，须在 api 容器内运行
（``bash backend/test/run_tests.sh e2e``）；宿主机运行时按导入失败跳过。
鉴权使用种子签发的一次性 API Key（yxkey_ Bearer），不依赖 .env 口令。
"""

from __future__ import annotations

import asyncio
import re
import time

import pytest

try:
    import literature_seed
except ImportError:  # 宿主机无容器内依赖
    literature_seed = None

pytestmark = pytest.mark.e2e

_POLL_INTERVAL = 3.0
_POLL_TIMEOUT = 420.0
# 模型供应商不可达类错误（编译是 LLM 操作；离线环境跳过编译断言而非误报代码缺陷）
_MODEL_UNAVAILABLE_RE = re.compile(r"Error calling model|timed out|Timeout|connect", re.IGNORECASE)


@pytest.fixture
async def lit_seed():
    if literature_seed is None:
        pytest.skip("literature_seed 不可导入：须在 api 容器内运行（run_tests.sh e2e）")
    result = await literature_seed.run_seed()
    assert result.get("ok"), {k: v for k, v in result.items() if k != "api_key"}
    yield result
    cleaned = await literature_seed.run_seed(cleanup=True)
    assert cleaned.get("ok"), cleaned


async def _wait_task(
    client,
    headers,
    *,
    type_: str,
    ref: str,
    expect: str = "SUCCEEDED",
    llm_ok_to_skip: bool = False,
    timeout: float = _POLL_TIMEOUT,
) -> dict:
    """轮询 /api/literature/tasks 至指定任务到达期望终态（FAILED/DEAD 即失败）。"""
    deadline = time.monotonic() + timeout
    last: dict | None = None
    snapshot: list = []
    while time.monotonic() < deadline:
        resp = await client.get("/api/literature/tasks", headers=headers)
        assert resp.status_code == 200, resp.text
        snapshot = resp.json().get("tasks", [])
        for task in snapshot:
            if task.get("type") == type_ and task.get("ref") == ref:
                last = task
                if task["status"] == expect:
                    return task
                if task["status"] in ("FAILED", "DEAD"):
                    error = str(task.get("error") or "")
                    if llm_ok_to_skip and _MODEL_UNAVAILABLE_RE.search(error):
                        pytest.skip(f"模型供应商不可达，跳过 LLM 编译断言: {error}")
                    pytest.fail(f"任务终态失败: {type_}/{ref} status={task['status']} error={error}")
        await asyncio.sleep(_POLL_INTERVAL)
    pytest.fail(
        f"任务轮询超时({timeout}s): {type_}/{ref} last={last} "
        f"snapshot={[{'type': t.get('type'), 'ref': t.get('ref'), 'status': t.get('status')} for t in snapshot]}"
    )


async def test_render_compile_pipeline(e2e_client, lit_seed):
    headers = {"Authorization": f"Bearer {lit_seed['api_key']}"}
    rid = lit_seed["rid"]
    revision_id = lit_seed["revision_id"]

    # 登记可见（种子侧等价于 W2 登记桥接的产物）
    resp = await e2e_client.get("/api/literature/papers", headers=headers)
    assert resp.status_code == 200, resp.text
    assert any(item["rid"] == rid for item in resp.json()["items"])

    # 阅读渲染（显式触发，决议 10；离线 paperparse 子进程）
    resp = await e2e_client.post(f"/api/literature/papers/{rid}/render", headers=headers)
    assert resp.status_code == 202, resp.text
    await _wait_task(e2e_client, headers, type_="reader_render", ref=revision_id)

    resp = await e2e_client.get(f"/api/literature/papers/{rid}", headers=headers)
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["render"]["status"] == "SUCCEEDED"
    assert (detail["render"]["markdown_uri"] or "").startswith("kbasset://")

    # 全局任务中心并入（W5）：文献任务对 superadmin 可见（种子 Key 归属 superadmin）
    resp = await e2e_client.get("/api/tasks", headers=headers, params={"limit": 100})
    assert resp.status_code == 200, resp.text
    assert any(t.get("type") == "literature_reader_render" for t in resp.json().get("tasks", []))

    # L1/L2 编译（手动端点；force=true 仅接管超过租约的历史孤儿，活跃任务保持幂等）
    resp = await e2e_client.post(
        f"/api/literature/papers/{rid}/compile", headers=headers, params={"level": "L1", "force": "true"}
    )
    assert resp.status_code == 202, resp.text
    # 模型不可达时 3 次尝试各约 45-300s 超时 + 重排，放宽窗口让其到达终态（skip 判定依赖终态）
    await _wait_task(
        e2e_client,
        headers,
        type_="compile",
        ref=f"{lit_seed['paper_id']}:L1",
        llm_ok_to_skip=True,
        timeout=900.0,
    )

    resp = await e2e_client.get(f"/api/literature/papers/{rid}", headers=headers)
    assert resp.status_code == 200, resp.text
    l1_jobs = [c for c in resp.json()["compile_jobs"] if c["level"] == "L1"]
    assert l1_jobs and l1_jobs[0]["status"] == "SUCCEEDED" and l1_jobs[0]["note_uri"]

    # kbasset 资产代理（租户校验 + 命名空间白名单 → 实体内容可读）
    resp = await e2e_client.get("/api/literature/assets", headers=headers, params={"uri": l1_jobs[0]["note_uri"]})
    assert resp.status_code == 200, resp.text
    assert len(resp.content) > 100
