"""Real Docker encrypted-byte roundtrip on isolated, labeled fixture volumes ONLY."""

import io
import os
import tarfile
import uuid
from types import SimpleNamespace

import json
import subprocess
from cryptography.exceptions import InvalidTag
import pytest

from yuxi.literature import native_recovery as recovery

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("YUXI_NATIVE_RECOVERY_TEST") != "1", reason="Requires isolated Docker fixture opt-in"),
]


def test_encrypted_native_state_isolated_restore_preserves_bytes_and_modes(tmp_path):
    client = recovery.DockerRuntime()
    identity = "recovery-fixture-" + uuid.uuid4().hex
    restore_prefix = "fixture-restore-" + uuid.uuid4().hex[:16]
    client.create_volume(identity, {"yuxi.fixture": identity})
    restored, helper = [], None
    payloads = {
        "postgresql/version-history": b"native IDs and frozen pointers",
        "minio/source.pdf": b"%PDF-bytes",
        "milvus/vector-index": b"\x00\xff\x01",
        "redis/queue": b"dormant task",
        "etcd/revision": b"index state",
        "neo4j/graph": b"edges",
        "saves/diary": "心得".encode(),
    }
    try:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for name, content in payloads.items():
                item = tarfile.TarInfo(name)
                item.size, item.mode = len(content), 0o640
                tar.addfile(item, io.BytesIO(content))
        helper = recovery._helper(client, "python:3.13-slim", identity, "volume", restore=True)
        assert helper.put_archive("/restore/data", archive.getvalue())
        helper.remove(force=True)
        helper = None
        # Synthetic stopped service inventory; real copying/crypto/Docker restoration.
        services = [
            SimpleNamespace(
                id=s,
                attrs={
                    "Config": {"Labels": {"com.docker.compose.service": s}},
                    "Image": "fixture",
                    "State": {"Running": False},
                    "Mounts": [{"Type": "volume", "Name": identity, "Destination": "/data", "RW": True}],
                },
            )
            for s in recovery.REQUIRED_SERVICES
        ]
        proxy = SimpleNamespace(inventory=lambda project=None, all=True: services if all else [], helper=client.helper)
        key = os.urandom(32)
        result = recovery.export_cold(
            proxy, project="fixture", state_root="/srv/fixture/docker/volumes", destination=tmp_path / "sealed", key=key
        )
        assert result["sealed"] and result["mounts"] == 1
        data = recovery.restore_isolated(client, directory=tmp_path / "sealed", key=key, prefix=restore_prefix)
        assert not data["started"]
        restored = [item["volume"] for item in data["mounts"]]
        helper = recovery._helper(client, "python:3.13-slim", restored[0], "volume", restore=True)
        chunks, _ = helper.get_archive("/restore/data")
        with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as tar:
            for name, content in payloads.items():
                item = tar.getmember("data/" + name)
                assert tar.extractfile(item).read() == content
                assert item.mode == 0o640
        # Corruption must be detected before creating any new destination volume.
        encrypted = tmp_path / "sealed/state-0000.enc"
        content = encrypted.read_bytes()
        encrypted.write_bytes(bytes([content[0] ^ 1]) + content[1:])
        with pytest.raises(InvalidTag):
            recovery.restore_isolated(client, directory=tmp_path / "sealed", key=key, prefix="fixture-corrupt")
        assert not client.list_volumes("fixture-corrupt-")
    finally:
        if helper:
            helper.remove(force=True)
        # An interrupted restore leaves fresh volumes for operator diagnosis;
        # fixture cleanup also covers that failure case, not production prefixes.
        if not restored:
            restored = client.list_volumes(restore_prefix + "-")
        for name in [identity, *restored]:
            volume = json.loads(subprocess.check_output(["docker", "volume", "inspect", name]))[0]
            labels = volume.get("Labels") or {}
            assert labels.get("yuxi.fixture") == identity or labels.get("yuxi.native-recovery")
            subprocess.run(["docker", "volume", "rm", name], check=True, capture_output=True)
