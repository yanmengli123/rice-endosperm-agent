import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

COMMIT = "7ada7918b9e515604d3c0ae264d3a9af10bf6e54"
ROOT = Path(__file__).resolve().parents[1]
REPO = Path(os.getenv("BIOINFOMCP_SOURCE_DIR") or ROOT.parent / "BioinfoMCP").resolve()
TOOLS = [
    "bamCoverage", "bcftools", "bedtools_coverage", "bedtools_intersect", "bowtie2", "bwa",
    "computeGCBias", "correctGCBias", "cutadapt", "faToTwoBit", "fastp", "flye", "freebayes",
    "gatk_ApplyBQSR", "gatk_BaseRecalibrator", "gatk_HaplotypeCaller", "gatk_SelectVariants",
    "gunzip", "hisat2", "kallisto", "macs3_callpeak", "macs3_hmmratac", "mafft", "meme",
    "minimap2", "multiqc", "plotCorrelation", "qualimap", "quast", "salmon", "samtools",
    "seqtk", "spades", "star", "stringtie", "trim-galore", "trimmomatic",
]

BWA_REQUIREMENTS = b"fastmcp==2.12.4\n"
BWA_ENV_YAML = (
    "\nname: mcp-tool\nchannels:\n  - bioconda\n  - conda-forge\n"
    "dependencies:\n  - bwa\n  - pip\n"
).encode()


def git_bytes(path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(REPO), "show", f"{COMMIT}:{path}"],
        capture_output=True, check=True,
    ).stdout


head = subprocess.run(
    ["git", "-C", str(REPO), "rev-parse", "HEAD"],
    capture_output=True,
    check=True,
    text=True,
).stdout.strip()
if head != COMMIT:
    raise SystemExit(f"BioinfoMCP source must be checked out at {COMMIT}; got {head}")

manifest = {}
for key in TOOLS:
    d = f"mcp_{key}"
    entry = {"server_file": f"{key}_server.py"}
    for kind, path in [
        ("server", f"mcp-servers/{d}/app/{key}_server.py"),
        ("requirements", f"mcp-servers/{d}/requirements.txt"),
        ("env_yaml", f"mcp-servers/{d}/environment.yaml"),
    ]:
        try:
            data = git_bytes(path)
            entry[f"{kind}_sha256"] = hashlib.sha256(data).hexdigest()
        except subprocess.CalledProcessError:
            if key == "bwa" and kind == "requirements":
                entry[f"{kind}_sha256"] = hashlib.sha256(BWA_REQUIREMENTS).hexdigest()
                entry[f"{kind}_synthesized"] = True
            elif key == "bwa" and kind == "env_yaml":
                entry[f"{kind}_sha256"] = hashlib.sha256(BWA_ENV_YAML).hexdigest()
                entry[f"{kind}_synthesized"] = True
            else:
                raise
    server_source = git_bytes(f"mcp-servers/{d}/app/{key}_server.py").decode("utf-8")
    entry["tool_names"] = re.findall(
        r"@mcp\.tool\([^\n]*\)\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)",
        server_source,
    )
    entry["tool_count"] = len(entry["tool_names"])
    if entry["tool_count"] == 0:
        raise SystemExit(f"No @mcp.tool definitions found for {key}")
    manifest[key] = entry

out = {
    "bioinfomcp_commit": COMMIT,
    "bioinfomcp_tree": subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD^{tree}"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip(),
    "license_sha256": hashlib.sha256(git_bytes("LICENSE")).hexdigest(),
    "upstream_source": "https://github.com/florensiawidjaja/BioinfoMCP",
    "tools": dict(sorted(manifest.items())),
}
dst = ROOT / "docker/mcp/bioinfomcp-tools/manifest.json"
dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
missing = [k for k, v in manifest.items() if v.get("env_yaml_synthesized")]
print(
    "manifest written:",
    len(manifest),
    "servers /",
    sum(item["tool_count"] for item in manifest.values()),
    "MCP tools; synthesized env_yaml:",
    missing,
)
