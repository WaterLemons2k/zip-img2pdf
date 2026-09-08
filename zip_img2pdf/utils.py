from __future__ import annotations

import logging
import os
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Iterable, Iterator, List, Protocol

from tqdm import tqdm

logger = logging.getLogger("zip-img2pdf")


class TqdmLoggingHandler(  # pyright: ignore[reportUnusedClass]
    logging.StreamHandler  # pyright: ignore[reportMissingTypeArgument]  # Generic in stubs, not subscriptable at runtime on Python 3.8
):
    """Log handler that prints records via tqdm.write() so they appear above the progress bar.

    tqdm.write() temporarily pauses and clears active progress bars, prints the
    message on the line above them, then restores and redraws the bars, so log
    output is never interleaved or overwritten by the bars' carriage returns.
    """

    def __init__(self) -> None:
        # The base's generic type parameter is Unknown (unparameterized on 3.8)
        super().__init__()  # pyright: ignore[reportUnknownMemberType]

    def emit(self, record: logging.LogRecord) -> None:
        try:
            # self.stream inherits the Unknown generic parameter from StreamHandler
            tqdm.write(self.format(record), file=self.stream)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            self.flush()
        except Exception:
            self.handleError(record)


class _ProgressBar(Protocol):
    """Minimal structural type for the tqdm bar we advance."""

    def update(self, n: float | None = 1) -> bool | None: ...


class ProgressList(List[str]):
    """A list whose iteration advances a progress bar once per item.

    img2pdf.convert() only expands its input when the first argument is a
    list/tuple and then iterates it lazily in its main loop, so wrapping the
    image list in this way yields one progress step per converted image (the bar's
    total is the number of images).

    An optional on_item callback is invoked with each item just before the bar
    advances, letting the caller observe the item currently being processed
    (e.g. for debug logging) without coupling this class to logging.
    """

    def __init__(
        self,
        items: Iterable[str],
        bar: _ProgressBar,
        on_item: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(items)
        self._bar: _ProgressBar = bar
        self._on_item: Callable[[str], None] | None = on_item

    def __iter__(self) -> Iterator[str]:
        for item in super().__iter__():
            if self._on_item is not None:
                self._on_item(item)
            self._bar.update(1)
            yield item


def pkg_version(name: str) -> str:
    """Return the installed version of a package, or 'unknown' if not installed."""
    try:
        ver = version(name)
    except PackageNotFoundError:
        ver = "unknown"

    return f"{name} {ver}"


def is_valid_pdf(path: str) -> bool:
    """Verify the output is indeed a valid PDF file."""
    if not Path(path).is_file():
        return False
    with open(path, "rb") as f:
        return f.read(5) == b"%PDF-" and f.seek(0, os.SEEK_END) > 0
