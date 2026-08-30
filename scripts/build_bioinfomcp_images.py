"""Build and verify BioinfoMCP runtime images from the pinned manifest.

Examples:
    python scripts/build_bioinfomcp_images.py --all --jobs 2
    python scripts/build_bioinfomcp_images.py samtools bowtie2 star

Existing images are skipped only when both the pinned upstream revision and
the per-server slug label match. A failed tool does not prevent the remaining
tools from building; the process exits non-zero with a final failure summary.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docker/mcp/bioinfomcp-tools/manifest.json"
RUNTIME_SCHEMA = "2"


def slug_of(tool: str) -> str:
    return "bioinfomcp-" + tool.lower().replace("_", "-")


def image_name(slug: str, commit: str) -> str:
    return f"yuxi-{slug}:{commit[:7]}"


def image_is_verified(slug: str, commit: str) -> bool:
    image = image_name(slug, commit)
    result = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}|'
            '{{index .Config.Labels "io.yuxi.bioinfomcp.slug"}}|'
            '{{index .Config.Labels "io.yuxi.bioinfomcp.runtime-schema"}}',
            image,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == f"{commit}|{slug}|{RUNTIME_SCHEMA}"


def build_one(
    tool: str,
    commit: str,
    force: bool,
    environment_lock: threading.Lock,
) -> tuple[str, bool, str]:
    slug = slug_of(tool)
    if not force and image_is_verified(slug, commit):
        return tool, True, "already verified"
    with environment_lock:
        free_gib = shutil.disk_usage(ROOT).free / 1024**3
        if free_gib < 4:
            return tool, False, f"only {free_gib:.1f} GiB free before build"
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--profile",
                "bioinfomcp",
                "build",
                f"{slug}-image",
            ],
            cwd=ROOT,
        )
        if result.returncode != 0:
            return tool, False, f"build exited with {result.returncode}"
        if not image_is_verified(slug, commit):
            return tool, False, "image labels failed verification"
    return tool, True, "built and verified"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tools", nargs="*", help="Manifest tool keys, for example samtools or star")
    parser.add_argument("--all", action="store_true", help="Build all 37 non-FastQC images")
    parser.add_argument("--jobs", type=int, default=1, choices=range(1, 5))
    parser.add_argument("--force", action="store_true", help="Rebuild verified images")
    args = parser.parse_args()

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    known = list(manifest["tools"])
    selected = known if args.all else args.tools
    if not selected:
        parser.error("select one or more tools, or pass --all")
    unknown = sorted(set(selected) - set(known))
    if unknown:
        parser.error(f"unknown tools: {', '.join(unknown)}")

    free_gib = shutil.disk_usage(ROOT).free / 1024**3
    if free_gib < 8:
        parser.error(f"only {free_gib:.1f} GiB free; at least 8 GiB is required")

    commit = manifest["bioinfomcp_commit"]
    results: list[tuple[str, bool, str]] = []
    environment_locks: dict[str, threading.Lock] = {}
    for tool in selected:
        environment_locks.setdefault(manifest["tools"][tool]["env_yaml_sha256"], threading.Lock())

    # Populate the shared Python/FastMCP base layer before parallel workers start.
    # Otherwise a clean host downloads and solves that identical layer once per worker.
    remaining = list(dict.fromkeys(selected))
    if args.jobs > 1 and remaining:
        tool = remaining.pop(0)
        result = build_one(
            tool,
            commit,
            args.force,
            environment_locks[manifest["tools"][tool]["env_yaml_sha256"]],
        )
        results.append(result)
        state = "OK" if result[1] else "FAILED"
        print(f"[{state}] {result[0]}: {result[2]}", flush=True)

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {
            pool.submit(
                build_one,
                tool,
                commit,
                args.force,
                environment_locks[manifest["tools"][tool]["env_yaml_sha256"]],
            ): tool
            for tool in remaining
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            state = "OK" if result[1] else "FAILED"
            print(f"[{state}] {result[0]}: {result[2]}", flush=True)

    failed = [tool for tool, ok, _ in results if not ok]
    print(f"BioinfoMCP images: {len(results) - len(failed)}/{len(results)} verified")
    if failed:
        print("Failed:", ", ".join(sorted(failed)))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
