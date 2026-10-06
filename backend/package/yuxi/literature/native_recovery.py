"""Operator-only encrypted cold snapshots; restore ONLY into fresh Docker volumes.

Preserves native database, object/version, queue and index bytes. This is not an
HTTP import, never runs SQL from an archive and never starts restored workers.
The deployment's code/images, .env and encryption master key are external.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import tarfile
import tempfile
import uuid
from types import SimpleNamespace

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

REQUIRED_SERVICES = {"api", "worker", "postgres", "redis", "minio", "milvus", "etcd", "graph"}
MAX_SNAPSHOT_BYTES = 100 * 1024**3


class DockerRuntime:
    """Use the deployment's existing Docker CLI, not an optional Python SDK."""

    def inventory(self, project=None, *, all=True):
        command = ["docker", "ps", "-aq" if all else "-q"]
        if project:
            command.extend(["--filter", "label=com.docker.compose.project=" + project])
        ids = subprocess.check_output(command, text=True).split()
        if not ids:
            return []
        return [
            SimpleNamespace(id=row["Id"], attrs=row)
            for row in json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True))
        ]

    def list_volumes(self, name):
        return subprocess.check_output(["docker", "volume", "ls", "-q", "--filter", "name=" + name], text=True).split()

    def create_volume(self, name, labels):
        command = ["docker", "volume", "create"]
        for key, value in labels.items():
            command.extend(["--label", key + "=" + value])
        subprocess.check_output([*command, name])
        return SimpleNamespace(attrs=json.loads(subprocess.check_output(["docker", "volume", "inspect", name]))[0])

    def helper(self, image, source, kind, restore):
        if "," in source:
            raise ValueError("挂载路径包含不支持的逗号")
        if kind == "volume" and source not in self.list_volumes(source):
            raise ValueError("状态卷不存在；禁止隐式新建源卷")
        destination = "/restore/data" if restore else "/data"
        mount = f"type={kind},source={source},target={destination}" + ("" if restore else ",readonly")
        identifier = subprocess.check_output(
            [
                "docker",
                "create",
                "--network",
                "none",
                *([] if restore else ["--read-only"]),
                "--cap-drop",
                "ALL",
                "--mount",
                mount,
                image,
                "sleep",
                "3600",
            ],
            text=True,
        ).strip()
        try:
            subprocess.check_output(["docker", "start", identifier])
        except subprocess.CalledProcessError:
            DockerHelper(identifier).remove(force=True)
            raise
        return DockerHelper(identifier)


class DockerHelper:
    def __init__(self, identifier):
        self.id = identifier

    def get_archive(self, path):
        def chunks():
            with tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(
                    ["docker", "cp", "--archive", self.id + ":" + path, "-"], stdout=subprocess.PIPE, stderr=errors
                )
                try:
                    while chunk := process.stdout.read(1024 * 1024):
                        yield chunk
                    if process.wait(timeout=1800):
                        raise RuntimeError("Docker 状态归档读取失败")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)
                    process.stdout.close()

        return chunks(), {}

    def put_archive(self, path, data):
        subprocess.run(
            ["docker", "cp", "--archive", "-", self.id + ":" + path],
            input=data,
            check=True,
            capture_output=True,
            timeout=1800,
        )
        return True

    def remove(self, force=False):
        subprocess.run(["docker", "rm", "-f", self.id], check=True, capture_output=True, timeout=60)


def cold_plan(client, project: str, state_root: str) -> dict:
    """Read Docker inventory without printing container env or credentials."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", project):
        raise ValueError("Compose project 标识无效")
    containers = client.inventory(project)
    services, mounts = {}, {}
    root = state_root.replace("\\", "/").rstrip("/")
    if not (root.startswith("/") or re.match(r"^[A-Za-z]:/", root)) or root.count("/") < 2:
        raise ValueError("必须显式指定部署的 docker/volumes 绝对目录")
    for container in containers:
        info = container.attrs
        service = info["Config"]["Labels"].get("com.docker.compose.service")
        if service in services:
            raise ValueError("每个服务只能有一个停止的实例")
        if info["State"]["Running"] or info["State"].get("Paused") or info["State"].get("Restarting"):
            raise ValueError("原生冷备需要所有 Compose 服务已停止；不会自动停机")
        services[service] = {"image": info["Image"], "container_id": container.id}
        for mount in info.get("Mounts", []):
            source = mount.get("Name") if mount["Type"] == "volume" else mount["Source"]
            source_path = str(source).replace("\\", "/")
            if not mount.get("RW") or mount["Type"] not in {"bind", "volume"}:
                continue
            # Code/config/secrets are NOT silently bundled with the native state.
            if mount["Type"] == "bind" and not source_path.lower().startswith(root.lower() + "/"):
                destination = mount["Destination"]
                if service in REQUIRED_SERVICES - {"api", "worker"} or (
                    service in {"api", "worker"}
                    and any(
                        destination == path or destination.startswith(path + "/")
                        for path in ("/app/saves", "/app/models")
                    )
                ):
                    raise ValueError("核心状态绑定位于声明目录之外；拒绝遗漏外部状态")
                continue
            key = (mount["Type"], source)
            mounts.setdefault(key, {"type": key[0], "source": source, "consumers": []})["consumers"].append(
                {"service": service, "destination": mount["Destination"]}
            )
    if not REQUIRED_SERVICES <= services.keys() or not mounts:
        raise ValueError("缺少核心服务或状态挂载；拒绝不完整冷备")
    sources = [(m["type"], str(m["source"]).replace("\\", "/").rstrip("/").lower()) for m in mounts.values()]
    for container in client.inventory(all=False):
        for mount in container.attrs.get("Mounts", []):
            source = mount.get("Name") if mount["Type"] == "volume" else mount.get("Source", "")
            path = str(source).replace("\\", "/").rstrip("/").lower()
            shared = any(
                mount["Type"] == kind
                and (
                    path == captured
                    or (kind == "bind" and (path.startswith(captured + "/") or captured.startswith(path + "/")))
                )
                for kind, captured in sources
            )
            if mount.get("RW") and shared:
                raise ValueError("有其他运行容器写入共享状态；拒绝非一致性快照")
    for service in REQUIRED_SERVICES:
        if not any(c["service"] == service for m in mounts.values() for c in m["consumers"]):
            raise ValueError(f"核心服务 {service} 没有可快照状态挂载")
    return {
        "schema": "yuxi-native-cold-v1",
        "project": project,
        "services": services,
        "mounts": sorted(mounts.values(), key=lambda m: (m["type"], m["source"])),
    }


def validate_tar(path: Path) -> None:
    """No devices/FIFOs, path escapes or links outside this individual volume."""
    with tarfile.open(path, "r:") as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if (
                not name.parts
                or name.is_absolute()
                or ".." in name.parts
                or name.parts[0] != "data"
                or "\\" in member.name
            ):
                raise ValueError("状态归档成员越界")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError("状态归档包含不可恢复的特殊文件")
            if member.issym() or member.islnk():
                target = member.linkname
                resolved = posixpath.normpath(posixpath.join(str(name.parent), target) if member.issym() else target)
                if target.startswith("/") or "\\" in target or not (resolved == "data" or resolved.startswith("data/")):
                    raise ValueError("外部 tablespace/链接不在快照内，拒绝不完整恢复")


def _helper(client, image, source, mount_type, *, restore=False):
    return client.helper(image, source, mount_type, restore)


def export_cold(
    client, *, project: str, state_root: str, destination: Path, key: bytes, helper_image="python:3.13-slim"
) -> dict:
    if len(key) != 32:
        raise ValueError("备份密钥必须为 32 个随机字节")
    plan = cold_plan(client, project, state_root)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    total = 0
    for idx, mount in enumerate(plan["mounts"]):
        helper = _helper(client, helper_image, mount["source"], mount["type"])
        try:
            chunks, _ = helper.get_archive("/data")
            nonce = os.urandom(12)
            filename = f"state-{idx:04d}.enc"
            cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
            cipher.authenticate_additional_data(filename.encode())
            digest, size = hashlib.sha256(), 0
            with (destination / filename).open("xb") as output:
                for chunk in chunks:
                    size += len(chunk)
                    total += len(chunk)
                    if total > MAX_SNAPSHOT_BYTES:
                        raise ValueError("冷备超过 100 GiB 单批预算")
                    digest.update(chunk)
                    output.write(cipher.update(chunk))
                output.write(cipher.finalize())
            mount.update(file=filename, nonce=nonce.hex(), tag=cipher.tag.hex(), size=size, sha256=digest.hexdigest())
        finally:
            helper.remove(force=True)  # only the temporary helper, no user volumes
    # A concurrent service start invalidates delivery, even if copy completed.
    after = cold_plan(client, project, state_root)
    if (
        plan["services"] != after["services"]
        or [{k: m[k] for k in ("type", "source", "consumers")} for m in plan["mounts"]] != after["mounts"]
    ):
        raise ValueError("冷备期间部署状态变化；未封存成功清单")
    nonce = os.urandom(12)
    (destination / "manifest.enc").write_bytes(
        nonce + AESGCM(key).encrypt(nonce, json.dumps(plan, sort_keys=True).encode(), b"yuxi-native-cold-v1")
    )
    return {"mounts": len(plan["mounts"]), "bytes": total, "sealed": True}


def decrypt_snapshot(directory: Path, key: bytes, scratch: Path) -> dict:
    if len(key) != 32:
        raise ValueError("备份密钥必须为 32 个随机字节")
    manifest = directory / "manifest.enc"
    if manifest.is_symlink() or manifest.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("恢复清单超过预算")
    blob = manifest.read_bytes()
    plan = json.loads(AESGCM(key).decrypt(blob[:12], blob[12:], b"yuxi-native-cold-v1"))
    if (
        plan.get("schema") != "yuxi-native-cold-v1"
        or not 0 < len(plan["mounts"]) <= 1000
        or not REQUIRED_SERVICES <= plan.get("services", {}).keys()
    ):
        raise ValueError("恢复清单契约无效")
    total = 0
    for idx, mount in enumerate(plan["mounts"]):
        expected = f"state-{idx:04d}.enc"
        source = directory / expected
        if (
            mount["file"] != expected
            or source.is_symlink()
            or not isinstance(mount["size"], int)
            or mount["size"] <= 0
            or source.stat().st_size != mount["size"]
        ):
            raise ValueError("状态文件身份或大小不符")
        total += mount["size"]
        if total > MAX_SNAPSHOT_BYTES:
            raise ValueError("恢复超过预算")
        cipher = Cipher(
            algorithms.AES(key), modes.GCM(bytes.fromhex(mount["nonce"]), bytes.fromhex(mount["tag"]))
        ).decryptor()
        cipher.authenticate_additional_data(expected.encode())
        digest = hashlib.sha256()
        target = scratch / f"state-{idx:04d}.tar"
        with source.open("rb") as encrypted, target.open("xb") as output:
            while chunk := encrypted.read(1024 * 1024):
                plain = cipher.update(chunk)
                digest.update(plain)
                output.write(plain)
            output.write(cipher.finalize())
        if digest.hexdigest() != mount["sha256"]:
            raise ValueError("状态摘要不符")
        validate_tar(target)
    return plan


def restore_isolated(client, *, directory: Path, key: bytes, prefix: str, helper_image="python:3.13-slim") -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,40}", prefix):
        raise ValueError("隔离恢复前缀无效")
    operation = uuid.uuid4().hex
    restored = []
    with tempfile.TemporaryDirectory(prefix="yuxi-native-restore-") as temporary:
        plan = decrypt_snapshot(directory, key, Path(temporary))  # verify ALL before any Docker writes
        for idx, mount in enumerate(plan["mounts"]):
            name = f"{prefix}-{operation[:12]}-{idx:04d}"
            if client.list_volumes(name):
                raise ValueError("恢复卷已存在；拒绝覆盖")
            volume = client.create_volume(name, {"yuxi.native-recovery": operation})
            if volume.attrs.get("Labels", {}).get("yuxi.native-recovery") != operation:
                raise ValueError("恢复卷归属冲突")
            helper = _helper(client, helper_image, name, "volume", restore=True)
            try:
                with (Path(temporary) / f"state-{idx:04d}.tar").open("rb") as archive:
                    subprocess.run(
                        ["docker", "cp", "--archive", "-", helper.id + ":/restore"],
                        stdin=archive,
                        check=True,
                        capture_output=True,
                        timeout=1800,
                    )
            finally:
                helper.remove(force=True)
            restored.append({"volume": name, "consumers": mount["consumers"]})
    return {
        "schema": plan["schema"],
        "services": plan["services"],
        "mounts": restored,
        "started": False,
        "warning": "未启动任何服务；先断开外网并校验配置/主密钥/版本，不自动重放恢复的任务。",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "backup", "verify", "restore"))
    parser.add_argument("--project", default="yuxi")
    parser.add_argument("--state-root")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--prefix", default="yuxi-recovery")
    args = parser.parse_args()
    client = DockerRuntime() if args.mode != "verify" else None
    if args.mode == "preflight":
        result = cold_plan(client, args.project, args.state_root or "")
        print(json.dumps({"ready": True, "mounts": len(result["mounts"])}))
        return
    if args.directory is None or args.key_file is None:
        parser.error("directory/key-file 必填；密钥内容不接受命令行参数")
    key = args.key_file.read_bytes()
    if args.mode == "verify":
        with tempfile.TemporaryDirectory(prefix="yuxi-native-verify-") as temporary:
            result = decrypt_snapshot(args.directory, key, Path(temporary))
            print(json.dumps({"verified": True, "mounts": len(result["mounts"])}))
    elif args.mode == "backup":
        print(
            json.dumps(
                export_cold(
                    client, project=args.project, state_root=args.state_root or "", destination=args.directory, key=key
                )
            )
        )
    else:
        print(json.dumps(restore_isolated(client, directory=args.directory, key=key, prefix=args.prefix)))


if __name__ == "__main__":
    main()
