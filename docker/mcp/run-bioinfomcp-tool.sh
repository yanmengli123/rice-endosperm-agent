#!/usr/bin/env bash
set -euo pipefail

# BioinfoMCP 各工具的统一受控启动入口（安全姿态与 run-bioinfomcp-fastqc.sh 一致）。
# 数据库 MCP 配置只能选择本启动器 + 白名单 slug，不能改变挂载、网络、权限或资源限制。
readonly COMMIT_SHORT="7ada791"
readonly COMMIT_FULL="7ada7918b9e515604d3c0ae264d3a9af10bf6e54"
readonly UID_VALUE="${YUXI_MCP_EXECUTION_UID:-}"
readonly THREAD_VALUE="${YUXI_MCP_EXECUTION_THREAD_ID:-}"
readonly SAFE_ID_PATTERN='^[A-Za-z0-9_-]+$'

readonly ALLOWED_SLUGS=(
  "bioinfomcp-bamcoverage"
  "bioinfomcp-bcftools"
  "bioinfomcp-bedtools-coverage"
  "bioinfomcp-bedtools-intersect"
  "bioinfomcp-bowtie2"
  "bioinfomcp-bwa"
  "bioinfomcp-computegcbias"
  "bioinfomcp-correctgcbias"
  "bioinfomcp-cutadapt"
  "bioinfomcp-fastp"
  "bioinfomcp-fatotwobit"
  "bioinfomcp-flye"
  "bioinfomcp-freebayes"
  "bioinfomcp-gatk-applybqsr"
  "bioinfomcp-gatk-baserecalibrator"
  "bioinfomcp-gatk-haplotypecaller"
  "bioinfomcp-gatk-selectvariants"
  "bioinfomcp-gunzip"
  "bioinfomcp-hisat2"
  "bioinfomcp-kallisto"
  "bioinfomcp-macs3-callpeak"
  "bioinfomcp-macs3-hmmratac"
  "bioinfomcp-mafft"
  "bioinfomcp-meme"
  "bioinfomcp-minimap2"
  "bioinfomcp-multiqc"
  "bioinfomcp-plotcorrelation"
  "bioinfomcp-qualimap"
  "bioinfomcp-quast"
  "bioinfomcp-salmon"
  "bioinfomcp-samtools"
  "bioinfomcp-seqtk"
  "bioinfomcp-spades"
  "bioinfomcp-star"
  "bioinfomcp-stringtie"
  "bioinfomcp-trim-galore"
  "bioinfomcp-trimmomatic"
)

fail() {
  printf 'BioinfoMCP runtime error: %s\n' "$1" >&2
  exit 1
}

[[ $# -ge 1 ]] || fail "usage: yuxi-bioinfomcp-tool <slug>"
readonly SLUG="$1"
found=0
for allowed in "${ALLOWED_SLUGS[@]}"; do
  [[ "$SLUG" == "$allowed" ]] && found=1 && break
done
[[ "$found" == 1 ]] || fail "slug '$SLUG' is not in the BioinfoMCP allowlist"

case "$SLUG" in
  bioinfomcp-flye|bioinfomcp-gatk-applybqsr|bioinfomcp-gatk-baserecalibrator|bioinfomcp-gatk-haplotypecaller|bioinfomcp-gatk-selectvariants|bioinfomcp-meme|bioinfomcp-quast|bioinfomcp-spades|bioinfomcp-star)
    readonly MEMORY_LIMIT="16g" CPU_LIMIT="8" PIDS_LIMIT="2048" MAX_RUNTIME_SECONDS="21600" TMP_SIZE="2g"
    ;;
  bioinfomcp-bamcoverage|bioinfomcp-bcftools|bioinfomcp-bowtie2|bioinfomcp-bwa|bioinfomcp-computegcbias|bioinfomcp-correctgcbias|bioinfomcp-freebayes|bioinfomcp-hisat2|bioinfomcp-kallisto|bioinfomcp-macs3-callpeak|bioinfomcp-macs3-hmmratac|bioinfomcp-minimap2|bioinfomcp-plotcorrelation|bioinfomcp-qualimap|bioinfomcp-salmon)
    readonly MEMORY_LIMIT="8g" CPU_LIMIT="4" PIDS_LIMIT="1024" MAX_RUNTIME_SECONDS="7200" TMP_SIZE="1g"
    ;;
  *)
    readonly MEMORY_LIMIT="4g" CPU_LIMIT="2" PIDS_LIMIT="512" MAX_RUNTIME_SECONDS="1800" TMP_SIZE="512m"
    ;;
esac

readonly IMAGE="yuxi-$SLUG:$COMMIT_SHORT"
command -v docker >/dev/null 2>&1 || fail "docker CLI is unavailable in the Yuxi runtime image"
docker info >/dev/null 2>&1 || fail "Docker Engine is unavailable to the Yuxi runtime"
docker image inspect "$IMAGE" >/dev/null 2>&1 || fail \
  "image '$IMAGE' is not installed; run: docker compose --profile bioinfomcp build $SLUG-image"
readonly IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE")"
readonly IMAGE_REVISION="$(
  docker image inspect \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE"
)"
[[ "$IMAGE_REVISION" == "$COMMIT_FULL" ]] || fail \
  "image '$IMAGE' does not carry the verified BioinfoMCP revision label"
readonly IMAGE_SLUG="$(
  docker image inspect \
    --format '{{index .Config.Labels "io.yuxi.bioinfomcp.slug"}}' "$IMAGE"
)"
[[ "$IMAGE_SLUG" == "$SLUG" ]] || fail \
  "image '$IMAGE' does not match the requested BioinfoMCP server"

readonly IMAGE_RUNTIME_SCHEMA="$(
  docker image inspect \
    --format '{{index .Config.Labels "io.yuxi.bioinfomcp.runtime-schema"}}' "$IMAGE"
)"
[[ "$IMAGE_RUNTIME_SCHEMA" == "2" ]] || fail \
  "image '$IMAGE' does not carry runtime schema '2'"

readonly SELF_CONTAINER_ID="$(cat /etc/hostname)"
SAVES_HOST_SOURCE="$(
  docker inspect \
    --format '{{range .Mounts}}{{if eq .Destination "/app/saves"}}{{.Source}}{{end}}{{end}}' \
    "$SELF_CONTAINER_ID" 2>/dev/null || true
)"
[[ -n "$SAVES_HOST_SOURCE" ]] || fail "cannot resolve the host source for /app/saves"
[[ "$SAVES_HOST_SOURCE" == *$'\n'* || "$SAVES_HOST_SOURCE" == *','* ]] && \
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

watch_container() {
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
}

cleanup() {
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
}
trap cleanup EXIT INT TERM HUP

watch_container "$$" &
watchdog_pid="$!"

set +e
docker run \
  --rm \
  --interactive \
  --pull never \
  --cidfile "$cidfile" \
  --network none \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --pids-limit "$PIDS_LIMIT" \
  --memory "$MEMORY_LIMIT" \
  --cpus "$CPU_LIMIT" \
  --stop-timeout 5 \
  --tmpfs "/tmp:rw,nosuid,nodev,noexec,size=$TMP_SIZE" \
  --env HOME=/tmp \
  --env TMPDIR=/tmp \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --workdir /home/gem/user-data \
  "${mount_args[@]}" \
  "$IMAGE_ID" <&0 &
docker_pid="$!"
wait "$docker_pid"
status="$?"
docker_pid=""
kill "$watchdog_pid" >/dev/null 2>&1 || true
watchdog_pid=""
set -e

exit "$status"
