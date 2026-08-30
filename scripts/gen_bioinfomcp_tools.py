"""Generate per-tool BioinfoMCP runtime assets from the pinned-commit manifest.

Outputs:
- docker/mcp/bioinfomcp-tools/<slug>/Dockerfile  (hash-pinned, mirrors the
  audited FastQC pattern: tuna-https apt mirror, SHA256-verified upstream files)
- docker/mcp/run-bioinfomcp-tool.sh              (one controlled launcher for all
  tools; slug must be in the embedded allowlist; identical security posture to
  run-bioinfomcp-fastqc.sh)
- docker-compose.yml                             (37 profile-gated build services)
- backend/package/yuxi/agents/mcp/bioinfomcp_catalog.py (builtin MCP entries)

Idempotent: re-running regenerates all outputs from manifest.json.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docker/mcp/bioinfomcp-tools/manifest.json"
COMPOSE = ROOT / "docker-compose.yml"
WRAPPER = ROOT / "docker/mcp/run-bioinfomcp-tool.sh"
CATALOG = ROOT / "backend/package/yuxi/agents/mcp/bioinfomcp_catalog.py"

COMMIT = "7ada7918b9e515604d3c0ae264d3a9af10bf6e54"
SHORT = COMMIT[:7]
RUNTIME_SCHEMA = "2"
MINIFORGE_BASE = (
    "condaforge/miniforge3@"
    "sha256:609d8012d8ad3ea46c8e531ce2de9e727031960067b3a1e412c6e2954ef551c9"
)

# 面向科研用户的简短用途说明；未列出的使用通用描述。
PURPOSE = {
    "bamCoverage": "将 BAM 转换为 deeptools 覆盖度信号文件",
    "bcftools": "处理 VCF/BCF 变异调用文件",
    "bedtools_coverage": "计算 BED 区间的覆盖度统计",
    "bedtools_intersect": "计算基因组区间交集",
    "bowtie2": "将短序列比对到参考基因组",
    "bwa": "将测序读段比对到参考基因组",
    "computeGCBias": "计算测序数据的 GC 偏倚",
    "correctGCBias": "校正测序数据的 GC 偏倚",
    "cutadapt": "去除测序接头的剪切工具",
    "faToTwoBit": "将 FASTA 转换为 2bit 压缩格式",
    "fastp": "FASTQ 快速质控与过滤",
    "flye": "长读段基因组从头组装",
    "freebayes": "基于单倍型的遗传变异检测",
    "gatk_ApplyBQSR": "GATK 应用碱基质量校正",
    "gatk_BaseRecalibrator": "GATK 碱基质量校正学习",
    "gatk_HaplotypeCaller": "GATK 单倍型遗传变异检测",
    "gatk_SelectVariants": "GATK 变异位点筛选",
    "gunzip": "解压 .gz 压缩文件",
    "hisat2": "将 RNA-seq 读段比对到参考基因组",
    "kallisto": "RNA-seq 转录本定量",
    "macs3_callpeak": "ChIP-seq 峰值调用",
    "macs3_hmmratac": "ATAC-seq 开放染色质区域调用",
    "mafft": "多序列比对",
    "meme": "motif 发现与分析",
    "minimap2": "长读段快速比对",
    "multiqc": "聚合多个质控工具的汇总报告",
    "plotCorrelation": "绘制样本间相关性图",
    "qualimap": "比对结果质量评估",
    "quast": "基因组组装质量评估",
    "salmon": "转录本定量",
    "samtools": "处理 SAM/BAM 比对文件",
    "seqtk": "FASTQ/FASTA 序列工具包",
    "spades": "短读段基因组从头组装",
    "star": "RNA-seq 高速比对",
    "stringtie": "转录本组装与定量",
    "trim-galore": "测序数据质量剪切",
    "trimmomatic": " Illumina 读段剪切",
}

# 容器上限不是资源预留；它防止异常参数或失控子进程拖垮 Yuxi 主服务。
# tools/list 不执行生信程序，因此管理页探测同样适用这些配置。
HEAVY_TOOLS = {
    "flye",
    "gatk_ApplyBQSR",
    "gatk_BaseRecalibrator",
    "gatk_HaplotypeCaller",
    "gatk_SelectVariants",
    "meme",
    "quast",
    "spades",
    "star",
}
MEDIUM_TOOLS = {
    "bamCoverage",
    "bcftools",
    "bowtie2",
    "bwa",
    "computeGCBias",
    "correctGCBias",
    "freebayes",
    "hisat2",
    "kallisto",
    "macs3_callpeak",
    "macs3_hmmratac",
    "minimap2",
    "plotCorrelation",
    "qualimap",
    "salmon",
}


def slug_of(tool_key: str) -> str:
    return "bioinfomcp-" + tool_key.lower().replace("_", "-")


def dockerfile_for(
    tool_key: str,
    info: dict,
    license_sha256: str,
    env_source_key: str,
) -> str:
    server = info["server_file"]
    slug = slug_of(tool_key)
    if info.get("env_yaml_synthesized"):
        env_fetch = (
            "RUN printf '\\nname: mcp-tool\\nchannels:\\n  - bioconda\\n"
            "  - conda-forge\\ndependencies:\\n  - bwa\\n  - pip\\n' > /tmp/environment.yaml\n"
        )
    else:
        env_fetch = f"""RUN python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \\
      "https://raw.githubusercontent.com/florensiawidjaja/BioinfoMCP/${{BIOINFOMCP_COMMIT}}/mcp-servers/mcp_{env_source_key}/environment.yaml" \\
      /tmp/environment.yaml
"""
    return f"""# 由 scripts/gen_bioinfomcp_tools.py 从 manifest.json 生成，勿手改。
# 上游固定提交 {SHORT}；Miniforge、环境、Python 服务与许可证均固定或校验。
FROM {MINIFORGE_BASE}

ARG BIOINFOMCP_COMMIT={COMMIT}
ARG REQUIREMENTS_SHA256={info["requirements_sha256"]}

ENV PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1 \\
    HOME=/tmp

WORKDIR /app
RUN python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \\
      "https://raw.githubusercontent.com/florensiawidjaja/BioinfoMCP/${{BIOINFOMCP_COMMIT}}/mcp-servers/mcp_bamCoverage/requirements.txt" \\
      /tmp/requirements.txt \\
    && printf '%s  %s\\n' "$REQUIREMENTS_SHA256" /tmp/requirements.txt | sha256sum --check --strict \\
    && conda install --name base --yes --channel conda-forge "python=3.12" \\
    && python -m pip install --no-cache-dir -r /tmp/requirements.txt \\
    && conda clean --all --yes \\
    && rm -rf /root/.cache /tmp/requirements.txt
ARG SERVER_SHA256={info["server_sha256"]}
ARG ENV_YAML_SHA256={info["env_yaml_sha256"]}
ARG LICENSE_SHA256={license_sha256}
{env_fetch}# 强制更新 base；上游文件声明 name=mcp-tool，原 Dockerfile 会把 CLI 装进未激活环境。
RUN printf '%s  %s\\n' "$ENV_YAML_SHA256" /tmp/environment.yaml | sha256sum --check --strict \\
    && for attempt in 1 2 3; do \\
         if conda env update --name base --file /tmp/environment.yaml; then break; fi; \\
         if [ "$attempt" -eq 3 ]; then exit 1; fi; \\
         sleep $((attempt * 5)); \\
       done \\
    && conda clean --all --yes \\
    && rm -rf /root/.cache /tmp/environment.yaml

RUN python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \\
      "https://raw.githubusercontent.com/florensiawidjaja/BioinfoMCP/${{BIOINFOMCP_COMMIT}}/mcp-servers/mcp_{tool_key}/app/{server}" \\
      /app/{server} \\
    && printf '%s  %s\\n' "$SERVER_SHA256" /app/{server} | sha256sum --check --strict \\
    && mkdir -p /usr/share/doc/bioinfomcp \\
    && python -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \\
      "https://raw.githubusercontent.com/florensiawidjaja/BioinfoMCP/${{BIOINFOMCP_COMMIT}}/LICENSE" \\
      /usr/share/doc/bioinfomcp/LICENSE \\
    && printf '%s  %s\\n' "$LICENSE_SHA256" /usr/share/doc/bioinfomcp/LICENSE | sha256sum --check --strict

COPY docker/mcp/bioinfomcp-tools/THIRD_PARTY_NOTICES.md \\
     /usr/share/doc/bioinfomcp/THIRD_PARTY_NOTICES.md

LABEL org.opencontainers.image.title="Yuxi BioinfoMCP {tool_key}" \\
      org.opencontainers.image.source="https://github.com/florensiawidjaja/BioinfoMCP" \\
      org.opencontainers.image.revision="${{BIOINFOMCP_COMMIT}}" \\
      org.opencontainers.image.licenses="MIT AND LicenseRef-See-Conda-Metadata" \\
      io.yuxi.bioinfomcp.slug="{slug}" \\
      io.yuxi.bioinfomcp.runtime-schema="{RUNTIME_SCHEMA}" \\
      io.yuxi.bioinfomcp.tool-count="{info['tool_count']}"

WORKDIR /home/gem/user-data
CMD ["python", "/app/{server}"]
"""


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    tools = manifest["tools"]
    requirement_hashes = {info["requirements_sha256"] for info in tools.values()}
    if len(requirement_hashes) != 1:
        raise RuntimeError("BioinfoMCP requirements.txt files are no longer identical")
    commit = manifest["bioinfomcp_commit"]
    license_sha256 = manifest["license_sha256"]
    short = commit[:7]
    env_source_by_hash: dict[str, str] = {}
    for key, info in sorted(tools.items()):
        env_source_by_hash.setdefault(info["env_yaml_sha256"], key)

    # 1) per-tool Dockerfiles
    for key, info in tools.items():
        slug = slug_of(key)
        out_dir = ROOT / "docker/mcp/bioinfomcp-tools" / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "Dockerfile").write_text(
            dockerfile_for(
                key,
                info,
                license_sha256,
                env_source_by_hash[info["env_yaml_sha256"]],
            ),
            encoding="utf-8",
            newline="\n",
        )

    # 2) compose build services（插到 bioinfomcp-fastqc-image 之后）
    compose = COMPOSE.read_text(encoding="utf-8")
    if "  bioinfomcp-bamcoverage-image:" not in compose:
        block = []
        for key in sorted(tools):
            slug = slug_of(key)
            block.append(f"""  {slug}-image:
    profiles: ["bioinfomcp"]
    build:
      context: .
      dockerfile: docker/mcp/bioinfomcp-tools/{slug}/Dockerfile
    image: yuxi-{slug}:{short}
    entrypoint: ["/bin/true"]
    restart: "no"
""")
        import re as _re
        m = _re.search(r'(  bioinfomcp-fastqc-image:\n(?:.*\n)*?    restart: "no"\n)', compose)
        assert m, "fastqc-image service block not found; compose insertion anchor missing"
        compose = compose[: m.end(1)] + "".join(block) + compose[m.end(1):]
        COMPOSE.write_text(compose, encoding="utf-8", newline="\n")
        print("compose: services added")

    # 3) 通用受控启动器
    slugs = sorted(slug_of(key) for key in tools)
    heavy_slugs = sorted(slug_of(key) for key in tools if key in HEAVY_TOOLS)
    medium_slugs = sorted(slug_of(key) for key in tools if key in MEDIUM_TOOLS)
    heavy_pattern = "|".join(heavy_slugs)
    medium_pattern = "|".join(medium_slugs)
    wrapper = f"""#!/usr/bin/env bash
set -euo pipefail

# BioinfoMCP 各工具的统一受控启动入口（安全姿态与 run-bioinfomcp-fastqc.sh 一致）。
# 数据库 MCP 配置只能选择本启动器 + 白名单 slug，不能改变挂载、网络、权限或资源限制。
readonly COMMIT_SHORT="{short}"
readonly COMMIT_FULL="{commit}"
readonly UID_VALUE="${{YUXI_MCP_EXECUTION_UID:-}}"
readonly THREAD_VALUE="${{YUXI_MCP_EXECUTION_THREAD_ID:-}}"
readonly SAFE_ID_PATTERN='^[A-Za-z0-9_-]+$'

readonly ALLOWED_SLUGS=(
{chr(10).join(f"  \"{s}\"" for s in slugs)}
)

fail() {{
  printf 'BioinfoMCP runtime error: %s\\n' "$1" >&2
  exit 1
}}

[[ $# -ge 1 ]] || fail "usage: yuxi-bioinfomcp-tool <slug>"
readonly SLUG="$1"
found=0
for allowed in "${{ALLOWED_SLUGS[@]}}"; do
  [[ "$SLUG" == "$allowed" ]] && found=1 && break
done
[[ "$found" == 1 ]] || fail "slug '$SLUG' is not in the BioinfoMCP allowlist"

case "$SLUG" in
  {heavy_pattern})
    readonly MEMORY_LIMIT="16g" CPU_LIMIT="8" PIDS_LIMIT="2048" MAX_RUNTIME_SECONDS="21600" TMP_SIZE="2g"
    ;;
  {medium_pattern})
    readonly MEMORY_LIMIT="8g" CPU_LIMIT="4" PIDS_LIMIT="1024" MAX_RUNTIME_SECONDS="7200" TMP_SIZE="1g"
    ;;
  *)
    readonly MEMORY_LIMIT="4g" CPU_LIMIT="2" PIDS_LIMIT="512" MAX_RUNTIME_SECONDS="1800" TMP_SIZE="512m"
    ;;
esac

readonly IMAGE="yuxi-$SLUG:$COMMIT_SHORT"
command -v docker >/dev/null 2>&1 || fail "docker CLI is unavailable in the Yuxi runtime image"
docker info >/dev/null 2>&1 || fail "Docker Engine is unavailable to the Yuxi runtime"
docker image inspect "$IMAGE" >/dev/null 2>&1 || fail \\
  "image '$IMAGE' is not installed; run: docker compose --profile bioinfomcp build $SLUG-image"
readonly IMAGE_ID="$(docker image inspect --format '{{{{.Id}}}}' "$IMAGE")"
readonly IMAGE_REVISION="$(
  docker image inspect \\
    --format '{{{{index .Config.Labels "org.opencontainers.image.revision"}}}}' "$IMAGE"
)"
[[ "$IMAGE_REVISION" == "$COMMIT_FULL" ]] || fail \\
  "image '$IMAGE' does not carry the verified BioinfoMCP revision label"
readonly IMAGE_SLUG="$(
  docker image inspect \\
    --format '{{{{index .Config.Labels "io.yuxi.bioinfomcp.slug"}}}}' "$IMAGE"
)"
[[ "$IMAGE_SLUG" == "$SLUG" ]] || fail \\
  "image '$IMAGE' does not match the requested BioinfoMCP server"

readonly IMAGE_RUNTIME_SCHEMA="$(
  docker image inspect \\
    --format '{{{{index .Config.Labels "io.yuxi.bioinfomcp.runtime-schema"}}}}' "$IMAGE"
)"
[[ "$IMAGE_RUNTIME_SCHEMA" == "{RUNTIME_SCHEMA}" ]] || fail \\
  "image '$IMAGE' does not carry runtime schema '{RUNTIME_SCHEMA}'"

readonly SELF_CONTAINER_ID="$(cat /etc/hostname)"
SAVES_HOST_SOURCE="$(
  docker inspect \\
    --format '{{{{range .Mounts}}}}{{{{if eq .Destination "/app/saves"}}}}{{{{.Source}}}}{{{{end}}}}{{{{end}}}}' \\
    "$SELF_CONTAINER_ID" 2>/dev/null || true
)"
[[ -n "$SAVES_HOST_SOURCE" ]] || fail "cannot resolve the host source for /app/saves"
[[ "$SAVES_HOST_SOURCE" == *$'\\n'* || "$SAVES_HOST_SOURCE" == *','* ]] && \\
  fail "the /app/saves host source contains unsupported characters"

mount_args=()
if [[ -n "$THREAD_VALUE" ]]; then
  [[ "$THREAD_VALUE" =~ $SAFE_ID_PATTERN ]] || fail "invalid execution thread identifier"
  [[ "$UID_VALUE" =~ $SAFE_ID_PATTERN ]] || fail "invalid execution user identifier"

  thread_local="/app/saves/threads/$THREAD_VALUE/user-data"
  workspace_local="/app/saves/threads/shared/$UID_VALUE/workspace"
  mkdir -p "$thread_local" "$workspace_local"

  thread_host="$SAVES_HOST_SOURCE/threads/$THREAD_VALUE/user-data"
  workspace_host="$SAVES_HOST_SOURCE/threads/shared/$UID_VALUE/workspace"
  mount_args+=(
    --mount "type=bind,source=$thread_host,target=/home/gem/user-data"
    --mount "type=bind,source=$workspace_host,target=/home/gem/user-data/workspace"
  )
else
  mount_args+=(--tmpfs "/home/gem/user-data:rw,nosuid,nodev,size=64m")
fi

runtime_dir="$(mktemp -d /tmp/yuxi-bioinfomcp.XXXXXX)"
cidfile="$runtime_dir/container.cid"
docker_pid=""
watchdog_pid=""

watch_container() {{
  local owner_pid="$1"
  local elapsed=0
  while ((elapsed < MAX_RUNTIME_SECONDS)); do
    if ! kill -0 "$owner_pid" >/dev/null 2>&1; then
      break
    fi
    if [[ -s "$cidfile" ]] && ! docker inspect "$(cat "$cidfile")" >/dev/null 2>&1; then
      return
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  if [[ -s "$cidfile" ]]; then
    docker kill "$(cat "$cidfile")" >/dev/null 2>&1 || true
  fi
}}

cleanup() {{
  if [[ -n "$watchdog_pid" ]]; then
    kill "$watchdog_pid" >/dev/null 2>&1 || true
  fi
  if [[ -s "$cidfile" ]]; then
    docker kill "$(cat "$cidfile")" >/dev/null 2>&1 || true
  fi
  if [[ -n "$docker_pid" ]]; then
    kill "$docker_pid" >/dev/null 2>&1 || true
  fi
  rm -rf "$runtime_dir"
}}
trap cleanup EXIT INT TERM HUP

watch_container "$$" &
watchdog_pid="$!"

set +e
docker run \\
  --rm \\
  --interactive \\
  --pull never \\
  --cidfile "$cidfile" \\
  --network none \\
  --read-only \\
  --cap-drop ALL \\
  --security-opt no-new-privileges:true \\
  --pids-limit "$PIDS_LIMIT" \\
  --memory "$MEMORY_LIMIT" \\
  --cpus "$CPU_LIMIT" \\
  --stop-timeout 5 \\
  --tmpfs "/tmp:rw,nosuid,nodev,noexec,size=$TMP_SIZE" \\
  --env HOME=/tmp \\
  --env TMPDIR=/tmp \\
  --env PYTHONDONTWRITEBYTECODE=1 \\
  --workdir /home/gem/user-data \\
  "${{mount_args[@]}}" \\
  "$IMAGE_ID" <&0 &
docker_pid="$!"
wait "$docker_pid"
status="$?"
docker_pid=""
kill "$watchdog_pid" >/dev/null 2>&1 || true
watchdog_pid=""
set -e

exit "$status"
"""
    WRAPPER.write_text(wrapper, encoding="utf-8", newline="\n")

    # 4) 后端目录模块
    entries = []
    for key in sorted(tools):
        slug = slug_of(key)
        purpose = PURPOSE.get(key, "隔离运行该生信分析工具")
        timeout = 21600 if key in HEAVY_TOOLS else 7200 if key in MEDIUM_TOOLS else 1800
        entries.append(f'    "{slug}": {{\n'
                       f'        "name": "BioinfoMCP · {key}",\n'
                       f'        "command": "/usr/local/bin/yuxi-bioinfomcp-tool",\n'
                       f'        "args": ["{slug}"],\n'
                       f'        "transport": "stdio",\n'
                       f'        "description": "隔离运行 {key}：{purpose}；"\n'
                       f'        "来源 florensiawidjaja/BioinfoMCP 固定提交 {short}",\n'
                       f'        "icon": "🧬",\n'
                       f'        "tags": ["内置", "BioinfoMCP"],\n'
                       f'        "timeout": {timeout},\n'
                       f'        "source_type": SOURCE_TYPE_BUILTIN,\n'
                       f'        "source_ref": (\n'
                       f'            "https://github.com/florensiawidjaja/BioinfoMCP@"\n'
                       f'            f"{commit}#mcp_{key}"\n'
                       f'        ),\n'
                       f'    }},')
    catalog = f'''"""BioinfoMCP 工具目录（生成文件，勿手改）。

由 scripts/gen_bioinfomcp_tools.py 从 docker/mcp/bioinfomcp-tools/manifest.json
生成：上游 florensiawidjaja/BioinfoMCP 固定提交 {short}，每工具一个隔离镜像、
一张 MCP 卡片。镜像构建：docker compose --profile bioinfomcp build <slug>-image。
"""

from typing import Any

from yuxi.agents.mcp.registry import SOURCE_TYPE_BUILTIN

BIOINFOMCP_COMMIT = "{commit}"
BIOINFOMCP_RUNTIME_SCHEMA = "{RUNTIME_SCHEMA}"

BIOINFOMCP_SERVERS: dict[str, dict[str, Any]] = {{
{chr(10).join(entries)}
}}

BIOINFOMCP_SLUGS = frozenset(BIOINFOMCP_SERVERS)
BIOINFOMCP_EXPECTED_TOOLS: dict[str, tuple[str, ...]] = {{
{chr(10).join(f'    "{slug_of(key)}": {tuple(info["tool_names"])!r},' for key, info in sorted(tools.items()))}
}}
'''
    CATALOG.write_text(catalog, encoding="utf-8", newline="\n")
    print(f"generated: {len(tools)} Dockerfiles + wrapper + catalog; compose services added")


if __name__ == "__main__":
    main()
