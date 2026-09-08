#!/usr/bin/env python3
"""
zip_img2pdf.cli - Convert a zip archive containing image files into a PDF file.

Dependencies: img2pdf, tqdm (managed via pyproject.toml + uv).

Usage:
    uv run zip-img2pdf <input.zip>
    uv run zip-img2pdf <input.zip> -o out.pdf --title "Document Title" --author "Author"

Notes:
    - Files are sorted naturally by name (img2 comes before img10); subfolders are searched recursively.
    - Supported image formats: jpg, jpeg, png, bmp, gif, webp, tif, tiff.
    - img2pdf embeds images losslessly without re-encoding; only one PDF file is produced.
    - By default each page matches its image's natural size; --pagesize sets a uniform
      page size (e.g. A4/A3/Letter/210mmx297mm, append ^T for landscape) and --fit
      controls how images are placed on the page.
      Note: when only a single dimension is given, the other is derived from the image
      aspect ratio, so pages may not be equal in size.
    - PDF metadata can be written via --title/--author/--subject/--keywords/--creator.
    - The Producer metadata defaults to "zip-img2pdf <version> (img2pdf <version>)" and can be overridden with --producer.
    - The PDF title defaults to the zip file's base name (without extension)
      unless --title is given.
    - The output file is named after the zip by default (extension .pdf).
    - Log messages are printed above the progress bars via tqdm.write(), so they are
      never overwritten by the bars' redraws.
"""

from __future__ import annotations

import logging
import os
from argparse import ArgumentParser
from pathlib import Path
from shutil import rmtree
from sys import exit
from tempfile import mkdtemp, mkstemp
from typing import Callable, cast
from zipfile import BadZipFile, ZipFile

import img2pdf  # pyright: ignore[reportMissingTypeStubs]
from tqdm import tqdm

from .archive import collect_images, safe_extract
from .images import find_bad_image, find_unreadable_image
from .utils import (
    ProgressList,
    TqdmLoggingHandler,
    is_valid_pdf,
    logger,
    pkg_version,
)


def build_parser():
    parser = ArgumentParser(
        description="Convert images from a zip archive into a single PDF"
    )
    parser.add_argument("input_zip", help="Path to the input zip file")
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Path to the output PDF file (defaults to the zip's name)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG level logging (default INFO)",
    )
    meta = parser.add_argument_group("PDF metadata")
    meta.add_argument("--title", help="Sets the title metadata value")
    meta.add_argument("--author", help="Sets the author metadata value")
    meta.add_argument("--creator", help="Sets the creator metadata value")
    meta.add_argument(
        "--producer",
        default=f"{pkg_version('zip-img2pdf')} ({pkg_version('img2pdf')})",
        help=f"Sets the producer metadata value (default: {pkg_version('zip-img2pdf')} ({pkg_version('img2pdf')}))",
    )
    meta.add_argument("--subject", help="Sets the subject metadata value")
    meta.add_argument(
        "--keywords",
        nargs="+",
        action="append",
        help="Sets the keywords metadata value (can be given multiple times)",
    )
    layout = parser.add_argument_group("Page layout")
    layout.add_argument(
        "--pagesize",
        default=None,
        type=img2pdf.parse_pagesize_rectarg,  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        help="Uniform page size, e.g. A4/A3/Letter/210mmx297mm; append ^T for landscape "
        "(default: each page matches its image's natural size)",
    )
    layout.add_argument(
        "--fit",
        default="shrink",
        type=str.lower,
        choices=["into", "fill", "exact", "shrink", "enlarge"],
        help="How to fit images on the page (only takes effect with --pagesize): "
        "into(scaled to fit the page)/"
        "fill(fill and crop)/exact(forced stretch)/"
        "shrink(only shrink; small images keep original size and are centered, default)/"
        "enlarge(only enlarge)",
    )
    return parser


def main():
    args = build_parser().parse_args()
    logging.addLevelName(logging.WARNING, "WARN")
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)5s %(name)s: %(message)s",
        handlers=[TqdmLoggingHandler()],
    )

    if not Path(args.input_zip).is_file():
        if Path(args.input_zip).is_dir():
            logger.error(f"input path is a directory, not a file: {args.input_zip}")
        else:
            logger.error(f"file not found: {args.input_zip}")
        exit(1)

    output_pdf = args.output
    if output_pdf is None:
        output_pdf = str(Path(args.input_zip).with_suffix(".pdf"))

    if Path(output_pdf).resolve() == Path(args.input_zip).resolve():
        logger.error("output file cannot be the same as the input zip.")
        exit(2)

    if Path(output_pdf).is_dir():
        logger.error(f"output path is a directory, not a file: {output_pdf}")
        exit(1)
    out_dir = str(Path(output_pdf).resolve().parent)
    if not Path(out_dir).is_dir():
        logger.error(f"output directory does not exist: {out_dir}")
        exit(1)

    # Only pass user-specified metadata to img2pdf; the title defaults to the
    # zip's base name (without extension) so the PDF always carries a title
    if args.title is None:
        args.title = Path(args.input_zip).stem

    metadata_kwargs: dict[str, str | list[str]] = {}
    for key in ("title", "author", "subject", "keywords", "creator", "producer"):
        value = getattr(args, key)
        if key == "keywords" and value is not None:
            value = [kw for group in value for kw in group]
        if value is not None:
            metadata_kwargs[key] = value

    # Use a uniform page size and place images according to --fit
    layout_fun = cast(
        Callable[..., object],
        img2pdf.get_layout_fun(  # pyright: ignore[reportUnknownMemberType]
            pagesize=args.pagesize,
            fit=img2pdf.FitMode[args.fit],
        ),
    )

    images: list[str] = []
    tmpdir = mkdtemp(prefix="zip_img2pdf_")
    try:
        logger.info(f"Extracting {args.input_zip}")
        with ZipFile(args.input_zip) as zf:
            safe_extract(zf, tmpdir)

        images = collect_images(tmpdir)
        if not images:
            logger.error("no images found in the zip archive.")
            exit(1)

        bad_image = find_bad_image(images)
        if bad_image is not None:
            logger.error(
                f"image file is corrupt or not a valid image: {Path(bad_image).relative_to(tmpdir)}"
            )
            exit(1)

        logger.info(f"Found {len(images)} images, converting...")

        # Atomic write: create a unique temp file in the output directory (concurrency-safe,
        # same-disk atomic replace), then swap in only after validation; on failure keep the
        # old output and leave no residue
        out_dir = str(Path(output_pdf).resolve().parent)
        fd, tmp_out = mkstemp(dir=out_dir, prefix=".zip-img2pdf-", suffix=".tmp")
        os.close(fd)
        try:
            with open(tmp_out, "wb") as f, tqdm(
                total=len(images), desc="Converting", unit="img", disable=None
            ) as bar:
                # disable=None: hide the progress bar when not on a TTY (pipes/redirection/test capture)
                img2pdf.convert(  # pyright: ignore[reportUnknownMemberType]
                    ProgressList(
                        images,
                        bar,
                        on_item=lambda img: logger.debug(
                            f"Converting {Path(img).relative_to(tmpdir)}"
                        ),
                    ),
                    outputstream=f,
                    layout_fun=layout_fun,
                    **metadata_kwargs,
                )
            # mkstemp defaults to 0600; fix permissions before replacing so the result stays readable by others on POSIX
            current_umask = os.umask(0)
            os.umask(current_umask)
            os.chmod(tmp_out, 0o666 & ~current_umask)
            if not is_valid_pdf(tmp_out):
                raise RuntimeError("Generated PDF failed validation")
            os.replace(tmp_out, output_pdf)
        finally:
            if Path(tmp_out).exists():
                os.remove(tmp_out)

        logger.info(f"Done: {output_pdf}")
    except BadZipFile:
        logger.error(f"{args.input_zip} is not a valid zip file.")
        exit(1)
    except img2pdf.ImageOpenError as exc:
        bad = find_unreadable_image(images, layout_fun, metadata_kwargs)
        if bad is not None:
            logger.error(
                f"image unreadable or unsupported format: {Path(bad).relative_to(tmpdir)} ({exc})"
            )
        else:
            logger.error(f"image unreadable or unsupported format: {exc}")
        exit(1)
    except KeyboardInterrupt:
        logger.error("cancelled.")
        exit(130)
    except Exception as exc:
        logger.error(f"{exc}")
        exit(1)
    finally:
        rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
