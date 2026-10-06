"""An actual stopped PostgreSQL data volume restored into a network-isolated instance."""

import json
import os
import subprocess
import time
import uuid
from types import SimpleNamespace

import pytest

from yuxi.literature import native_recovery as recovery

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("YUXI_NATIVE_RECOVERY_TEST") != "1", reason="Requires isolated Docker fixture opt-in"),
]


def test_native_postgres_restore_keeps_versions_trash_ledger_and_task_state(tmp_path):
    runtime = recovery.DockerRuntime()
    label = "postgres-recovery-fixture-" + uuid.uuid4().hex
    prefix = "pg-restore-" + uuid.uuid4().hex[:16]
    runtime.create_volume(label, {"yuxi.fixture": label})
    containers, volumes = [], [label]

    def start(volume):
        identifier = subprocess.check_output(
            [
                "docker",
                "run",
                "-d",
                "--network",
                "none",
                "--label",
                "yuxi.fixture=" + label,
                "--mount",
                f"type=volume,source={volume},target=/var/lib/postgresql/data",
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "-e",
                "POSTGRES_USER=fixture",
                "-e",
                "POSTGRES_DB=fixture",
                "postgres:17-alpine",
            ],
            text=True,
        ).strip()
        containers.append(identifier)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            # Initialization's temporary server accepts Unix-socket connections,
            # then shuts down. TCP is available only on the final server.
            probe = subprocess.run(
                ["docker", "exec", identifier, "pg_isready", "-h", "127.0.0.1", "-U", "fixture"], capture_output=True
            )
            if probe.returncode == 0:
                return identifier
            time.sleep(0.25)
        pytest.fail("Isolated fixture PostgreSQL did not start")

    def sql(identifier, statement):
        return subprocess.check_output(
            ["docker", "exec", identifier, "psql", "-U", "fixture", "-d", "fixture", "-At", "-c", statement], text=True
        )

    try:
        source = start(label)
        sql(
            source,
            "CREATE TABLE native_state(id BIGSERIAL PRIMARY KEY, kind TEXT, value JSONB); "
            "INSERT INTO native_state(kind,value) VALUES "
            '(\'frozen_revision\',\'{"pointer":"lar-original","history":["lar-old","lar-original"]}\'), '
            "('trash','{\"trashed\":true}'), ('ledger','{\"charged_once\":true}'), "
            '(\'task\',\'{"status":"RUNNING","fencing_token":17}\'), '
            "('index','{\"revision\":\"parse-original\"}');",
        )
        expected = sql(source, "SELECT id,kind,value FROM native_state ORDER BY id")
        subprocess.run(["docker", "stop", "-t", "10", source], check=True, capture_output=True)
        info = json.loads(subprocess.check_output(["docker", "inspect", source]))[0]
        services = [
            SimpleNamespace(
                id=source,
                attrs={
                    "Config": {"Labels": {"com.docker.compose.service": service}},
                    "Image": info["Image"],
                    "State": {"Running": False},
                    "Mounts": [
                        {"Type": "volume", "Name": label, "Destination": "/var/lib/postgresql/data", "RW": True}
                    ],
                },
            )
            for service in recovery.REQUIRED_SERVICES
        ]
        proxy = SimpleNamespace(inventory=lambda project=None, all=True: services if all else [], helper=runtime.helper)
        key = os.urandom(32)
        recovery.export_cold(
            proxy, project="fixture", state_root="/srv/fixture/docker/volumes", destination=tmp_path / "sealed", key=key
        )
        result = recovery.restore_isolated(runtime, directory=tmp_path / "sealed", key=key, prefix=prefix)
        assert result["started"] is False
        restored = result["mounts"][0]["volume"]
        volumes.append(restored)
        clone = start(restored)  # ONLY this fixture DB, no worker/API/network/public ports
        assert sql(clone, "SELECT id,kind,value FROM native_state ORDER BY id") == expected
        assert sql(clone, "INSERT INTO native_state(kind,value) VALUES ('new','{}') RETURNING id").startswith("6")
    finally:
        for identifier in containers:
            info = json.loads(subprocess.check_output(["docker", "inspect", identifier]))[0]
            assert info["Config"]["Labels"]["yuxi.fixture"] == label
            subprocess.run(["docker", "rm", "-f", identifier], check=True, capture_output=True)
        volumes = set(volumes + runtime.list_volumes(prefix + "-"))
        for name in volumes:
            info = json.loads(subprocess.check_output(["docker", "volume", "inspect", name]))[0]
            assert (info.get("Labels") or {}).get("yuxi.fixture") == label or (
                name.startswith(prefix + "-") and info["Labels"].get("yuxi.native-recovery")
            )
            subprocess.run(["docker", "volume", "rm", name], check=True, capture_output=True)
