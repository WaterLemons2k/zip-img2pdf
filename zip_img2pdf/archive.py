from __future__ import annotations

import os
import re
from pathlib import Path
from zipfile import ZipFile

# Supported image extensions (lowercase)
IMAGE_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".gif",
    ".webp",
    ".tif",
    ".tiff",
}


def natural_key(name: str) -> list[tuple[int, int | str]]:
    """Natural sort key: puts img2 before img10 (type-safe, handles mixed names)."""
    return [
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in re.split(r"(\d+)", name)
    ]


def safe_extract(zf: ZipFile, dest: str):
    """Safely extract: only writes image members (zip-bomb hardening); for those,
    prevents zip-slip path traversal and rejects absolute paths and .. escapes.
    Non-image members are skipped entirely, whatever their names."""
    dest_root = Path(dest).resolve()
    for member in zf.infolist():
        name = member.filename
        if Path(name).suffix.lower() not in IMAGE_EXTS:
            continue
        if "\x00" in name:
            raise RuntimeError(f"Unsafe path rejected: {name}")

        try:
            target = (Path(dest) / name).resolve()
        except OSError as exc:
            raise RuntimeError(f"Failed to extract {name}: {exc}") from exc
        # reject anything escaping dest (absolute paths reset the join, so they are caught too)
        if not (target == dest_root or dest_root in target.parents):
            raise RuntimeError(f"Unsafe path rejected: {name}")
        try:
            zf.extract(member, dest)
        except OSError as exc:
            raise RuntimeError(f"Failed to extract {name}: {exc}") from exc


def collect_images(root: str) -> list[str]:
    """Recursively collect all image file paths, sorted in natural order."""
    images: list[str] = []
    for dirpath, _, filenames in os.walk(root):
        for fname in filenames:
            if Path(fname).suffix.lower() in IMAGE_EXTS:
                images.append(str(Path(dirpath) / fname))
    images.sort(key=lambda p: (natural_key(Path(p).name), str(Path(p).parent)))
    return images
