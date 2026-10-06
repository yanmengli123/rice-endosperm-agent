"""Cold-plan refusal and authenticated archive validation without Docker IO."""

import json
import os
import tarfile
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from yuxi.literature import native_recovery as recovery

pytestmark = pytest.mark.unit


def inventory(*, running=False, foreign=False):
    items = [
        SimpleNamespace(
            id=service,
            attrs={
                "Config": {"Labels": {"com.docker.compose.service": service}},
                "Image": "sha256:fixture",
                "State": {"Running": running},
                "Mounts": [
                    {"Type": "bind", "Source": "/srv/yuxi/docker/volumes/state", "Destination": "/data", "RW": True}
                ],
            },
        )
        for service in recovery.REQUIRED_SERVICES
    ]
    foreign_container = SimpleNamespace(attrs={"Mounts": items[0].attrs["Mounts"]})
    return SimpleNamespace(
        inventory=lambda project=None, all=True: items if all else ([foreign_container] if foreign else [])
    )


def test_cold_snapshot_requires_all_services_stopped_and_no_shared_writer():
    with pytest.raises(ValueError, match="已停止"):
        recovery.cold_plan(inventory(running=True), "yuxi", "/srv/yuxi/docker/volumes")
    with pytest.raises(ValueError, match="其他运行容器"):
        recovery.cold_plan(inventory(foreign=True), "yuxi", "/srv/yuxi/docker/volumes")
    nested = inventory(foreign=True)
    nested.inventory(all=False)[0].attrs["Mounts"] = [{"Type": "bind", "Source": "/srv/yuxi/docker", "RW": True}]
    with pytest.raises(ValueError, match="其他运行容器"):
        recovery.cold_plan(nested, "yuxi", "/srv/yuxi/docker/volumes")
    plan = recovery.cold_plan(inventory(), "yuxi", "/srv/yuxi/docker/volumes")
    assert len(plan["mounts"]) == 1 and len(plan["mounts"][0]["consumers"]) == 8
    with pytest.raises(ValueError, match="绝对目录"):
        recovery.cold_plan(inventory(), "yuxi", "relative/docker/volumes")
    external = inventory()
    core = next(item for item in external.inventory() if item.id == "postgres")
    core.attrs["Mounts"].append({"Type": "bind", "Source": "/external/wal", "Destination": "/wal", "RW": True})
    with pytest.raises(ValueError, match="外部状态"):
        recovery.cold_plan(external, "yuxi", "/srv/yuxi/docker/volumes")


@pytest.mark.parametrize(
    "name,link", [("data/../../escape", ""), ("data/link", "/etc/passwd"), ("data/link", "../../outside")]
)
def test_native_archive_rejects_path_and_external_link(tmp_path, name, link):
    path = tmp_path / "bad.tar"
    with tarfile.open(path, "w") as archive:
        item = tarfile.TarInfo(name)
        if link:
            item.type, item.linkname = tarfile.SYMTYPE, link
        archive.addfile(item)
    with pytest.raises(ValueError):
        recovery.validate_tar(path)


def test_native_manifest_authentication_before_any_restore_mutation(tmp_path):
    key, nonce = os.urandom(32), os.urandom(12)
    (tmp_path / "manifest.enc").write_bytes(
        nonce
        + AESGCM(key).encrypt(
            nonce, json.dumps({"schema": "yuxi-native-cold-v1", "mounts": []}).encode(), b"yuxi-native-cold-v1"
        )
    )
    with pytest.raises(InvalidTag):
        recovery.restore_isolated(None, directory=tmp_path, key=os.urandom(32), prefix="lab-restore")
    with pytest.raises(ValueError):
        recovery.restore_isolated(None, directory=tmp_path, key=key, prefix="../production")
    with pytest.raises(ValueError, match="契约"):
        recovery.restore_isolated(None, directory=tmp_path, key=key, prefix="lab-restore")
    with pytest.raises(ValueError, match="32"):
        recovery.decrypt_snapshot(tmp_path, os.urandom(16), tmp_path)
