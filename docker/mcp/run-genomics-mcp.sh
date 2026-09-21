#!/usr/bin/env bash
set -euo pipefail

readonly SLUG="${1:-}"
readonly UID_VALUE="${YUXI_MCP_EXECUTION_UID:-}"
readonly THREAD_VALUE="${YUXI_MCP_EXECUTION_THREAD_ID:-}"
readonly SAFE_ID_PATTERN='^[A-Za-z0-9_-]+$'

fail() {
  printf 'Genomics MCP runtime error: %s\n' "$1" >&2
  exit 1
}

case "$SLUG" in
  gene-authority)
    IMAGE="${YUXI_GENE_AUTHORITY_IMAGE:-yuxi-gene-authority:1.1.0}"
    REVISION="gene-authority-1.1.0+ncbi-datasets-18.37.0"
    ;;
  plant-genomics)
    IMAGE="${YUXI_PLANT_GENOMICS_IMAGE:-yuxi-plant-genomics:1.21.0}"
    REVISION="ddd223f641cebf82927e9b6ce68394f047ee6930"
    ;;
  gramene)
    IMAGE="${YUXI_GRAMENE_MCP_IMAGE:-yuxi-gramene-mcp:b42afce}"
    REVISION="b42afce19b96e14b0a3f2e47ce8208eea9fe1f60"
    ;;
  *) fail "slug '$SLUG' is not in the allowlist" ;;
esac
readonly IMAGE REVISION

command -v docker >/dev/null 2>&1 || fail "docker CLI is unavailable"
docker info >/dev/null 2>&1 || fail "Docker Engine is unavailable"
docker image inspect "$IMAGE" >/dev/null 2>&1 || fail \
  "image '$IMAGE' is not installed; build the genomics-mcp compose profile"
readonly IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE")"
readonly ACTUAL_REVISION="$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE")"
readonly ACTUAL_SLUG="$(docker image inspect --format '{{index .Config.Labels "io.yuxi.mcp.slug"}}' "$IMAGE")"
readonly RUNTIME_SCHEMA="$(docker image inspect --format '{{index .Config.Labels "io.yuxi.mcp.runtime-schema"}}' "$IMAGE")"
[[ "$ACTUAL_REVISION" == "$REVISION" ]] || fail "image revision label mismatch"
[[ "$ACTUAL_SLUG" == "$SLUG" ]] || fail "image slug label mismatch"
[[ "$RUNTIME_SCHEMA" == "1" ]] || fail "image runtime schema mismatch"

mount_args=(--tmpfs "/home/gem/user-data:rw,nosuid,nodev,size=64m")
if [[ "$SLUG" == "gene-authority" && -n "$THREAD_VALUE" ]]; then
  [[ "$THREAD_VALUE" =~ $SAFE_ID_PATTERN ]] || fail "invalid execution thread identifier"
  [[ "$UID_VALUE" =~ $SAFE_ID_PATTERN ]] || fail "invalid execution user identifier"
  readonly SELF_CONTAINER_ID="$(cat /etc/hostname)"
  SAVES_HOST_SOURCE="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/app/saves"}}{{.Source}}{{end}}{{end}}' "$SELF_CONTAINER_ID" 2>/dev/null || true)"
  [[ -n "$SAVES_HOST_SOURCE" ]] || fail "cannot resolve /app/saves host source"
  [[ "$SAVES_HOST_SOURCE" != *$'\n'* && "$SAVES_HOST_SOURCE" != *','* ]] || fail "unsupported saves path"
  thread_local="/app/saves/threads/$THREAD_VALUE/user-data"
  thread_host="$SAVES_HOST_SOURCE/threads/$THREAD_VALUE/user-data"
  mkdir -p "$thread_local"
  mount_args=(--mount "type=bind,source=$thread_host,target=/home/gem/user-data")
fi

env_args=(--env HOME=/tmp)
if [[ "$SLUG" == "gene-authority" ]]; then
  [[ -z "${NCBI_API_KEY:-}" ]] || env_args+=(--env NCBI_API_KEY)
  [[ -z "${YUXI_NCBI_EMAIL:-}" ]] || env_args+=(--env YUXI_NCBI_EMAIL)
elif [[ "$SLUG" == "plant-genomics" ]]; then
  [[ -z "${PLANT_GENOMICS_MCP_NCBI_EMAIL:-}" ]] || env_args+=(--env PLANT_GENOMICS_MCP_NCBI_EMAIL)
elif [[ "$SLUG" == "gramene" ]]; then
  [[ -z "${GRAMENE_API_BASE:-}" ]] || env_args+=(--env GRAMENE_API_BASE)
fi

runtime_dir="$(mktemp -d /tmp/yuxi-genomics-mcp.XXXXXX)"
cidfile="$runtime_dir/container.cid"
cleanup() {
  if [[ -s "$cidfile" ]]; then docker kill "$(cat "$cidfile")" >/dev/null 2>&1 || true; fi
  rm -rf "$runtime_dir"
}
trap cleanup EXIT INT TERM HUP

docker run \
  --rm \
  --interactive \
  --pull never \
  --cidfile "$cidfile" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --pids-limit 256 \
  --memory 2g \
  --cpus 2 \
  --stop-timeout 5 \
  --tmpfs "/tmp:rw,nosuid,nodev,noexec,size=256m" \
  "${mount_args[@]}" \
  "${env_args[@]}" \
  --workdir /home/gem/user-data \
  "$IMAGE_ID"
