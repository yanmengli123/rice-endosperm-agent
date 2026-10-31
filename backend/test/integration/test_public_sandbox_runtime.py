"""Explicit provisioner smoke with unique disposable scopes; never enumerates other sandboxes."""

import os
from uuid import uuid4

import pytest


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    pass


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_databases():
    pass


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    pass


def test_public_container_lifetime_keeps_project_files_and_other_runs(monkeypatch):
    if os.getenv("YUXI_V08_SANDBOX_SMOKE") != "explicit-disposable-sandbox-scopes":
        pytest.skip("Requires explicit disposable provisioner smoke authorization")
    from yuxi.agents.backends.sandbox import provider as module
    from yuxi.agents.backends.sandbox.backend import ProvisionerSandboxBackend
    from yuxi.utils.paths import VIRTUAL_PATH_OUTPUTS

    nonce = "v08_smoke_" + uuid4().hex
    monkeypatch.setattr(module, "load_user_agent_env", lambda uid: {})
    provider = module.ProvisionerSandboxProvider()
    monkeypatch.setattr(module, "_sandbox_provider", provider)
    runs = [nonce + str(index) for index in range(3)]

    def backend(run_id):
        token = module.public_sandbox_run.set(run_id)
        instance = ProvisionerSandboxBackend(nonce, uid=nonce, file_thread_id=nonce, skills_thread_id=nonce)
        return token, instance

    try:
        token, first = backend(runs[0])
        try:
            result = first.execute(
                f"printf first > /tmp/v08-lifetime; printf shared > {VIRTUAL_PATH_OUTPUTS}/sentinel.txt"
            )
            assert result.exit_code == 0
        finally:
            module.public_sandbox_run.reset(token)
        token, second = backend(runs[1])
        try:
            result = second.execute(f"test ! -e /tmp/v08-lifetime && cat {VIRTUAL_PATH_OUTPUTS}/sentinel.txt")
            assert result.exit_code == 0 and "shared" in result.output
            assert first.id != second.id
            provider.release_run(runs[0])
            assert provider._client.discover(first.id) is None
            assert second.execute("printf alive").exit_code == 0
        finally:
            module.public_sandbox_run.reset(token)
        token, third = backend(runs[2])
        try:
            result = third.execute(f"test ! -e /tmp/v08-lifetime && test -f {VIRTUAL_PATH_OUTPUTS}/sentinel.txt")
            assert result.exit_code == 0
        finally:
            module.public_sandbox_run.reset(token)
    finally:
        for run_id in runs:
            module.destroy_public_run(
                run_id=run_id, uid=nonce, thread_id=nonce, file_thread_id=nonce, skills_thread_id=nonce
            )
    assert provider._connections == {}
