"""Provision pinned PDF OCR weights during image build, never during ingestion."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import time
from urllib.request import urlopen

from yuxi.knowledge.pdf_evidence.region_ocr import OCR_MODELS, model_directory


def provision(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, metadata in OCR_MODELS.items():
        destination = root / name
        if destination.is_file():
            with destination.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() == metadata["sha256"]:
                    continue
        temporary = destination.with_suffix(".download")
        for attempt in range(3):
            try:
                digest = hashlib.sha256()
                size = 0
                with urlopen(metadata["url"], timeout=60) as response, temporary.open("wb") as stream:
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > 128 * 1024 * 1024:
                            raise ValueError(f"OCR_MODEL_SIZE_EXCEEDED: {name}")
                        digest.update(chunk)
                        stream.write(chunk)
                if digest.hexdigest() != metadata["sha256"]:
                    raise ValueError(f"OCR_MODEL_CHECKSUM_MISMATCH: {name}")
                os.replace(temporary, destination)
                destination.chmod(0o444)
                print(f"Provisioned {name}: SHA256 verified")
                break
            except Exception:
                temporary.unlink(missing_ok=True)
                if attempt == 2:
                    raise
                time.sleep(2**attempt)


if __name__ == "__main__":
    provision(model_directory())
