from __future__ import annotations

from io import BytesIO
from typing import Callable

import img2pdf  # pyright: ignore[reportMissingTypeStubs]


def _file_starts_with(path: str, magic: bytes, offset: int = 0) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(len(magic)) == magic
    except OSError:
        return False


def _has_valid_magic(path: str) -> bool:
    """Check magic bytes by file content (not extension) to spot corrupt or disguised image files.

    Content matching any supported image format counts as valid, mirroring img2pdf's
    content-based sniffing so real images with mismatched extensions are not rejected.
    """
    if _file_starts_with(path, b"\xff\xd8\xff"):  # JPEG
        return True
    if _file_starts_with(path, b"\x89PNG\r\n\x1a\n"):  # PNG
        return True
    if _file_starts_with(path, b"GIF8"):  # GIF
        return True
    if _file_starts_with(path, b"BM"):  # BMP
        return True
    if _file_starts_with(path, b"II*\x00") or _file_starts_with(
        path, b"MM\x00*"
    ):  # TIFF
        return True
    return _file_starts_with(path, b"RIFF") and _file_starts_with(
        path, b"WEBP", offset=8
    )  # WebP


def find_bad_image(images: list[str]) -> str | None:
    """Return the first image whose magic bytes do not match, or None if all are fine."""
    for img in images:
        if not _has_valid_magic(img):
            return img
    return None


def find_unreadable_image(
    images: list[str],
    layout_fun: Callable[..., object],
    metadata_kwargs: dict[str, str | list[str]],
) -> str | None:
    """Locate the first image that img2pdf cannot read (e.g. truncated file), or None if all readable.

    Only called when img2pdf conversion raises: each image is tried individually to
    pinpoint the bad file so the error can name it. The trial conversion must mirror
    the real layout_fun and metadata kwargs, otherwise an oversized valid image could
    fail under the default layout and be misjudged as broken. Returns None when all
    are readable, letting the caller log the original exception as a fallback.
    """
    for img in images:
        try:
            with BytesIO() as buf:
                img2pdf.convert(  # pyright: ignore[reportUnknownMemberType]
                    [img], outputstream=buf, layout_fun=layout_fun, **metadata_kwargs
                )
        except Exception:
            return img
    return None
