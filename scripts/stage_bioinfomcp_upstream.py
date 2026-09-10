"""Stage pinned upstream files locally and switch failing tool builds to COPY+verify.

raw.githubusercontent.com is intermittently unreachable from the build network.
The pinned commit objects already exist in the local BioinfoMCP clone, so stage
server.py / requirements.txt / environment.yaml / LICENSE from `git show` into
docker/mcp/bioinfomcp-tools/<slug>/upstream/ and rewrite the six failing
Dockerfiles to COPY + sha256sum --check (hashes unchanged, provenance identical).
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent / "BioinfoMCP"
COMMIT = "7ada7918b9e515604d3c0ae264d3a9af10bf6e54"
TOOLS = ["hisat2", "quast", "star", "stringtie", "trim-galore", "trimmomatic"]


def git_bytes(path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(REPO), "show", f"{COMMIT}:{path}"],
        capture_output=True, check=True,
    ).stdout


def patch_dockerfile(df: Path, tool_key: str, server_file: str) -> None:
    t = df.read_text(encoding="utf-8")
    upstream = f"docker/mcp/bioinfomcp-tools/bioinfomcp-{tool_key.lower()}/upstream"

    # 1) requirements 下载 → COPY + 校验（保留 conda python 锁定与 pip 安装）
    old_req = f"""RUN python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \\
      "https://raw.githubusercontent.com/florensiawidjaja/BioinfoMCP/${{BIOINFOMCP_COMMIT}}/mcp-servers/mcp_{tool_key}/requirements.txt" \\
      /tmp/requirements.txt \\
    && printf '%s  %s\\n' "$REQUIREMENTS_SHA256" /tmp/requirements.txt | sha256sum --check --strict \\
"""
    new_req = f"""COPY {upstream}/requirements.txt /tmp/requirements.txt
RUN printf '%s  %s\\n' "$REQUIREMENTS_SHA256" /tmp/requirements.txt | sha256sum --check --strict \\
"""
    assert old_req in t, f"requirements block not found in {df.name}"
    t = t.replace(old_req, new_req)

    # 2) environment.yaml 下载 → COPY
    old_env = f"""RUN python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \\
      "https://raw.githubusercontent.com/florensiawidjaja/BioinfoMCP/${{BIOINFOMCP_COMMIT}}/mcp-servers/mcp_{tool_key}/environment.yaml" \\
      /tmp/environment.yaml
"""
    new_env = f"""COPY {upstream}/environment.yaml /tmp/environment.yaml
"""
    assert old_env in t, f"environment block not found in {df.name}"
    t = t.replace(old_env, new_env)

    # 3) server + LICENSE 下载 → COPY + 校验
    old_server = f"""RUN python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \\
      "https://raw.githubusercontent.com/florensiawidjaja/BioinfoMCP/${{BIOINFOMCP_COMMIT}}/mcp-servers/mcp_{tool_key}/app/{server_file}" \\
      /app/{server_file} \\
    && printf '%s  %s\\n' "$SERVER_SHA256" /app/{server_file} | sha256sum --check --strict \\
    && mkdir -p /usr/share/doc/bioinfomcp \\
    && python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \\
      "https://raw.githubusercontent.com/florensiawidjaja/BioinfoMCP/${{BIOINFOMCP_COMMIT}}/LICENSE" \\
      /usr/share/doc/bioinfomcp/LICENSE \\
    && printf '%s  %s\\n' "$LICENSE_SHA256" /usr/share/doc/bioinfomcp/LICENSE | sha256sum --check --strict"""
    new_server = f"""COPY {upstream}/{server_file} /app/{server_file}
COPY {upstream}/LICENSE /usr/share/doc/bioinfomcp/LICENSE
RUN printf '%s  %s\\n' "$SERVER_SHA256" /app/{server_file} | sha256sum --check --strict \\
    && printf '%s  %s\\n' "$LICENSE_SHA256" /usr/share/doc/bioinfomcp/LICENSE | sha256sum --check --strict"""
    assert old_server in t, f"server block not found in {df.name}"
    t = t.replace(old_server, new_server)

    df.write_text(t, encoding="utf-8", newline="\n")


def main() -> None:
    license_bytes = git_bytes("LICENSE")
    for key in TOOLS:
        slug = f"bioinfomcp-{key.lower()}"
        upstream_dir = ROOT / "docker/mcp/bioinfomcp-tools" / slug / "upstream"
        upstream_dir.mkdir(parents=True, exist_ok=True)
        server_file = f"{key}_server.py"
        (upstream_dir / server_file).write_bytes(
            git_bytes(f"mcp-servers/mcp_{key}/app/{server_file}")
        )
        (upstream_dir / "requirements.txt").write_bytes(
            git_bytes(f"mcp-servers/mcp_{key}/requirements.txt")
        )
        (upstream_dir / "environment.yaml").write_bytes(
            git_bytes(f"mcp-servers/mcp_{key}/environment.yaml")
        )
        (upstream_dir / "LICENSE").write_bytes(license_bytes)
        patch_dockerfile(ROOT / "docker/mcp/bioinfomcp-tools" / slug / "Dockerfile", key, server_file)
        print(f"staged + patched: {slug}")


if __name__ == "__main__":
    main()
