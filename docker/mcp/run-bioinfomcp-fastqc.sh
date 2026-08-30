#!/usr/bin/env bash
set -euo pipefail

# Controlled launcher for the BioinfoMCP FastQC stdio server. Database MCP
# configuration can select this launcher, but cannot alter its container mounts,
# network policy, privileges, or resource limits.
readonly DEFAULT_IMAGE="yuxi-bioinfomcp-fastqc:7ada7918"
readonly IMAGE="${YUXI_BIOINFOMCP_FASTQC_IMAGE:-$DEFAULT_IMAGE}"
readonly UID_VALUE="${YUXI_MCP_EXECUTION_UID:-}"
readonly THREAD_VALUE="${YUXI_MCP_EXECUTION_THREAD_ID:-}"
readonly SAFE_ID_PATTERN='^[A-Za-z0-9_-]+$'

fail() {
  printf 'BioinfoMCP FastQC runtime error: %s\n' "$1" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || fail "docker CLI is unavailable in the Yuxi runtime image"
docker info >/dev/null 2>&1 || fail "Docker Engine is unavailable to the Yuxi runtime"
docker image inspect "$IMAGE" >/dev/null 2>&1 || fail \
  "image '$IMAGE' is not installed; run: docker compose --profile bioinfomcp build bioinfomcp-fastqc-image"
readonly IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$IMAGE")"
readonly IMAGE_REVISION="$(
  docker image inspect \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$IMAGE"
)"
[[ "$IMAGE_REVISION" == "7ada7918b9e515604d3c0ae264d3a9af10bf6e54" ]] || fail \
  "image '$IMAGE' does not carry the verified BioinfoMCP revision label"
readonly IMAGE_SLUG="$(
  docker image inspect \
    --format '{{index .Config.Labels "io.yuxi.bioinfomcp.slug"}}' \
    "$IMAGE"
)"
[[ "$IMAGE_SLUG" == "bioinfomcp-fastqc" ]] || fail \
  "image '$IMAGE' is not labelled for BioinfoMCP FastQC"
readonly IMAGE_RUNTIME_SCHEMA="$(
  docker image inspect \
    --format '{{index .Config.Labels "io.yuxi.bioinfomcp.runtime-schema"}}' \
    "$IMAGE"
)"
[[ "$IMAGE_RUNTIME_SCHEMA" == "2" ]] || fail \
  "image '$IMAGE' does not carry runtime schema '2'"

readonly SELF_CONTAINER_ID="$(cat /etc/hostname)"
SAVES_HOST_SOURCE="$(
  docker inspect \
    --format '{{range .Mounts}}{{if eq .Destination "/app/saves"}}{{.Source}}{{end}}{{end}}' \
    "$SELF_CONTAINER_ID" 2>/dev/null || true
)"

if [[ -z "$SAVES_HOST_SOURCE" ]]; then
  fail "cannot resolve the host source for /app/saves"
fi
if [[ "$SAVES_HOST_SOURCE" == *$'\n'* || "$SAVES_HOST_SOURCE" == *','* ]]; then
  fail "the /app/saves host source contains unsupported characters"
fi

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
  # The admin connection test only runs tools/list and must not see user files.
  mount_args+=(--tmpfs "/home/gem/user-data:rw,nosuid,nodev,size=64m")
fi

runtime_dir="$(mktemp -d /tmp/yuxi-bioinfomcp-fastqc.XXXXXX)"
cidfile="$runtime_dir/container.cid"
docker_pid=""
watchdog_pid=""

watch_container() {
  local owner_pid="$1"
  local elapsed=0
  while ((elapsed < 330)); do
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
  --pids-limit 512 \
  --memory 4g \
  --cpus 2 \
  --stop-timeout 5 \
  --tmpfs "/tmp:rw,nosuid,nodev,noexec,size=256m" \
  --env HOME=/tmp \
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
