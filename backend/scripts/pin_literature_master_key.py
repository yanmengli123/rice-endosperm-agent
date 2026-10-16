"""Preserve the current credential key in a separate operator-managed file. Never rotate or print it."""

import argparse
import hmac
import os
from pathlib import Path

from dotenv import dotenv_values


def pin(*, source_env: Path, target: Path, dev_key: Path):
    values = dotenv_values(source_env)
    raw = os.getenv("YUXI_SECRET_MASTER_KEY") or values.get("YUXI_SECRET_MASTER_KEY")
    configured_file = os.getenv("YUXI_SECRET_MASTER_KEY_FILE") or values.get("YUXI_SECRET_MASTER_KEY_FILE")
    if not raw:
        source = Path(configured_file) if configured_file else dev_key
        raw = source.read_text(encoding="utf-8").strip()  # Existing only; never generate a replacement.
    if not isinstance(raw, str) or len(raw.strip()) < 32:
        raise ValueError("Existing credential master key unavailable")
    raw = raw.strip()
    if not target.is_absolute() or not target.parent.is_dir():
        raise ValueError("An existing external secret directory is required")
    if target.exists():
        if not hmac.compare_digest(target.read_text(encoding="utf-8").strip(), raw):
            raise ValueError("Refusing to overwrite a different credential key")
    else:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    print("Existing credential key preserved in external secret file; no rotation performed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-env", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--dev-key", type=Path, default=Path("/app/saves/.yuxi-dev-secret-master-key"))
    args = parser.parse_args()
    try:
        pin(source_env=args.source_env, target=args.target, dev_key=args.dev_key)
    except Exception as error:
        raise SystemExit(f"Credential key preservation failed ({type(error).__name__}); no key logged") from None
