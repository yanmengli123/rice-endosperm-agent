"""Rewrite urllib-download RUN blocks in the six failing BioinfoMCP Dockerfiles
to local COPY + sha256 verification (upstream files staged from the pinned
commit in the local clone). Generic line-based rewrite handles per-tool
variations in the RUN bodies."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ["hisat2", "quast", "star", "stringtie", "trim-galore", "trimmomatic"]

DOWNLOAD_RE = re.compile(
    r"RUN python -c 'import sys, urllib\.request; urllib\.request\.urlretrieve\(sys\.argv\[1\], sys\.argv\[2\]\)' \\\n"
    r"      \"(?P<url>https://raw\.githubusercontent\.com/[^\"]+)\" \\\n"
    r"      (?P<dest>\S+)(?:\\\n(?P<rest>.*?))?\n",
    re.DOTALL,
)


def filename_of(url: str, dest: str) -> str:
    if url.endswith("/LICENSE"):
        return "LICENSE"
    return dest.rsplit("/", 1)[-1]


def rewrite(df: Path) -> bool:
    t = df.read_text(encoding="utf-8")
    if "raw.githubusercontent.com" not in t:
        return False

    def replace_block(match: re.Match) -> str:
        body_first = match.group(0)
        url = match.group("url")
        dest = match.group("dest")
        rest = match.group("rest")
        name = filename_of(url, dest)
        tool_dir = df.parent.name
        copy_line = f"COPY docker/mcp/bioinfomcp-tools/{tool_dir}/upstream/{name} {dest}\n"
        if rest is None:
            return copy_line
        # RUN 内剩余命令链（以 && 开头的行序列）→ 首行去掉 &&
        rest_lines = [line for line in rest.split("\n") if line.strip()]
        rebuilt = ["RUN " + rest_lines[0].lstrip()[3:]]  # 去掉首个 "&& "
        rebuilt += [line for line in rest_lines[1:]]
        return copy_line + "\n".join(rebuilt) + "\n"

    new_t = DOWNLOAD_RE.sub(replace_block, t)
    assert "raw.githubusercontent.com" not in new_t, f"unhandled download remains in {df.name}"
    df.write_text(new_t, encoding="utf-8", newline="\n")
    return True


def main() -> None:
    staged = 0
    for key in TOOLS:
        slug = f"bioinfomcp-{key.lower()}"
        df = ROOT / "docker/mcp/bioinfomcp-tools" / slug / "Dockerfile"
        if rewrite(df):
            staged += 1
            print(f"rewritten: {slug}")
        else:
            print(f"skip (no remote downloads): {slug}")
    print(f"done: {staged} rewritten")


if __name__ == "__main__":
    main()
