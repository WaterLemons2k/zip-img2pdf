"""zip-img2pdf end-to-end validation matrix (pytest).

Setup:  uv sync --group dev
Run:    python -m pytest -v
"""

from __future__ import annotations

import contextlib
import glob
import io
import logging
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import warnings
import zipfile
from pathlib import Path
from typing import Callable, Generator, Iterator, cast

import pikepdf
import pytest
from PIL import Image

PROJ = Path(__file__).resolve().parent.parent

# Force the child tool's Python to emit UTF-8 regardless of the Windows locale
# codec (Chinese Windows defaults stderr to GBK, breaking UTF-8 consumers)
CLI_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8:replace"}


def run_cli(*args: str | Path, env: dict[str, str] | None = None) -> tuple[int, str]:
    """Invoke the CLI via the test interpreter, returning (returncode, stdout)."""
    proc = subprocess.run(
        [sys.executable, "-m", "zip_img2pdf", *args],
        cwd=PROJ,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=CLI_ENV if env is None else {**CLI_ENV, **env},
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def run_cli_inproc(
    *args: str | Path, env: dict[str, str] | None = None
) -> tuple[int, str]:
    """Run the CLI in-process via runpy, returning (returncode, stdout)."""
    old_argv = sys.argv
    old_environ = dict(os.environ)
    old_cwd = os.getcwd()
    root_logger = logging.getLogger()
    old_handlers = list(root_logger.handlers)
    old_level = root_logger.level
    out, err = io.StringIO(), io.StringIO()
    try:
        # The CLI's logging.basicConfig() is a no-op once root has handlers, so
        # drop them: this re-binds _TqdmLoggingHandler to the redirected stderr
        # on every call instead of the first call's discarded StringIO.
        root_logger.handlers = []
        if env is not None:
            os.environ.update(env)
        os.chdir(PROJ)
        sys.argv = [str(PROJ / "zip_img2pdf" / "cli.py"), *map(str, args)]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                # runpy.run_module re-executes the module even though it is already in
                # sys.modules (imported via zip_img2pdf/__init__.py); suppress the
                # resulting benign RuntimeWarning.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    runpy.run_module("zip_img2pdf.cli", run_name="__main__")
                rc = 0
            except SystemExit as exc:
                rc = exc.code if isinstance(exc.code, int) else 0
        return rc, out.getvalue() + err.getvalue()
    finally:
        # The CLI calls logging.addLevelName(logging.WARNING, "WARN"), a
        # process-global table mutation that would leak across in-process runs.
        logging.addLevelName(logging.WARNING, "WARNING")
        root_logger.handlers = old_handlers
        root_logger.setLevel(old_level)
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_environ)
        sys.argv = old_argv


@pytest.fixture(scope="session")
def work() -> Generator[Path, None, None]:
    workdir = Path(tempfile.mkdtemp(prefix="zp_test_"))
    yield workdir
    shutil.rmtree(workdir, ignore_errors=True)


@pytest.fixture(scope="session")
def basic_zip(work: Path) -> Path:
    """Shared mixed-format zip; sole source of truth for the basic matrix."""
    zp = work / "basic.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for name, ext, size in [
            ("img2.jpg", "JPEG", (300, 200)),
            ("img10.png", "PNG", (100, 100)),
            ("sub/img3.gif", "GIF", (50, 80)),
            ("b.bmp", "BMP", (120, 90)),
            ("c.webp", "WEBP", (70, 70)),
            ("d.tif", "TIFF", (90, 140)),
        ]:
            buf = io.BytesIO()
            Image.new("RGB", size, (10, 20, 30)).save(buf, format=ext)
            zf.writestr(name, buf.getvalue())
    return zp


def media_boxes(pdf_path: Path) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    with pikepdf.open(pdf_path) as pdf:
        for p in pdf.pages:
            box = [float(x) for x in p.MediaBox]  # pyright: ignore[reportGeneralTypeIssues, reportUnknownArgumentType, reportUnknownVariableType]
            out.append((round(box[2], 2), round(box[3], 2)))
    return out


def page_img_sizes(pdf_path: Path) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    with pikepdf.open(pdf_path) as pdf:
        for p in pdf.pages:
            xo = p["/Resources"]["/XObject"]
            for k in cast(Iterator[str], xo):
                out.append((int(xo[k]["/Width"]), int(xo[k]["/Height"])))
    return out


def test_basic(basic_zip: Path, work: Path) -> None:
    zp = basic_zip
    out = work / "basic.pdf"
    rc, text = run_cli_inproc(zp, "-o", out)
    assert rc == 0, f"exit code 0: {text}"
    assert out.is_file(), "only 1 PDF"
    assert out.exists() and len(media_boxes(out)) == 6, "6 pages"
    # default: each page matches its image's natural size (96 dpi -> 0.75 pt/px)
    assert set(media_boxes(out)) == {
        (225.0, 150.0),
        (75.0, 75.0),
        (37.5, 60.0),
        (90.0, 67.5),
        (52.5, 52.5),
        (67.5, 105.0),
    }, "pages match image natural sizes"


def test_natural_sort(work: Path) -> None:
    zp = work / "sort.zip"
    # Each image uses a unique size so page order can be asserted precisely
    sizes = {
        "1.jpg": (10, 10),
        "2.jpg": (20, 20),
        "10.jpg": (100, 100),
        "a1b2c.jpg": (30, 30),
        "a1b.jpg": (40, 40),
        "a2.jpg": (50, 50),
    }
    with zipfile.ZipFile(zp, "w") as zf:
        for n, s in sizes.items():
            buf = io.BytesIO()
            Image.new("RGB", s).save(buf, format="JPEG")
            zf.writestr(n, buf.getvalue())
    out = work / "sort.pdf"
    rc, text = run_cli_inproc(zp, "-o", out)
    assert rc == 0, f"no crash, exit code 0: {text}"
    got: list[tuple[int, int]] = page_img_sizes(out) if out.exists() else []
    # Expected order: 1, 2, 10, a1b2c, a1b, a2 (digit group first, natural order within the a group)
    expected = [(10, 10), (20, 20), (100, 100), (30, 30), (40, 40), (50, 50)]
    assert got == expected, f"page order fully correct: got={got}"


def test_zip_slip(work: Path) -> None:
    zp = work / "slip.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (60, 60)).save(buf, format="JPEG")
        zf.writestr("../evil.png", buf.getvalue())
    rc, text = run_cli_inproc(zp, "-o", work / "slip.pdf")
    assert rc == 1, f"rejected with exit 1: rc={rc}"
    assert "Unsafe path" in text, f"reports unsafe path: {text}"
    assert not (work / ".." / "evil.png").exists(), "no out-of-bounds file written"


def test_invalid_pagesize(basic_zip: Path, work: Path) -> None:
    zp = basic_zip
    out = work / "ps.pdf"
    rc, _ = run_cli_inproc(zp, "-o", out, "--pagesize", "bogus")
    assert rc == 2, f"exit code 2 (usage): rc={rc}"
    assert not out.exists(), "no PDF generated"


def test_metadata(basic_zip: Path, work: Path) -> None:
    zp = basic_zip
    out = work / "meta.pdf"
    rc, text = run_cli_inproc(
        zp,
        "-o",
        out,
        "--title",
        "Title",
        "--author",
        "Author",
        "--subject",
        "Subject",
        "--keywords",
        "k1",
        "--keywords",
        "k2",
        "--creator",
        "Creator",
    )
    assert rc == 0, f"exit code 0: {text}"
    with pikepdf.open(out) as pdf:
        d = pdf.docinfo
        import re

        prod = str(d["/Producer"])
        assert str(d.get("/Title") or "") == "Title", f"Title: {d.get('/Title')!s}"
        assert str(d.get("/Author") or "") == "Author", "Author"
        assert str(d.get("/Subject") or "") == "Subject", "Subject"
        assert str(d.get("/Keywords") or "") == "k1,k2", (
            f"Keywords: {d.get('/Keywords')!s}"
        )
        assert str(d.get("/Creator") or "") == "Creator", "Creator"
        assert bool(
            re.match(
                r"zip-img2pdf [\d.]+ \(img2pdf [\d.]+\)",
                prod,
            )
        ), f"Producer contains versions: {prod}"


def test_producer_override(basic_zip: Path, work: Path) -> None:
    zp = basic_zip
    out = work / "producer.pdf"
    rc, text = run_cli_inproc(zp, "-o", out, "--producer", "My Tool 9.9")
    assert rc == 0, f"exit code 0: {text}"
    with pikepdf.open(out) as pdf:
        assert str(pdf.docinfo.get("/Producer") or "") == "My Tool 9.9", (
            f"Producer overridden: {pdf.docinfo.get('/Producer')!s}"
        )


def test_default_name(work: Path) -> None:
    zp = work / "defname.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("a.jpg", buf.getvalue())
    rc, _ = run_cli_inproc(zp)
    assert rc == 0 and (work / "defname.pdf").exists(), "defname.pdf generated"
    with pikepdf.open(work / "defname.pdf") as pdf:
        assert str(pdf.docinfo.get("/Title") or "") == "defname", (
            f"default Title is the zip base name: {pdf.docinfo.get('/Title')!s}"
        )


def test_no_images(work: Path) -> None:
    zp = work / "txt.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("readme.txt", "hello")
    rc, text = run_cli_inproc(zp, "-o", work / "nope.pdf")
    assert rc == 1 and "no images found" in text, f"exits 1 with message: {text}"


def test_corrupt_image_atomic(work: Path) -> None:
    zp = work / "corrupt.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("good.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # fake JPEG
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("real.jpg", buf.getvalue())
    out = work / "corrupt_out.pdf"
    rc, text = run_cli_inproc(zp, "-o", out)
    assert rc == 1 and ("image" in text or "Error" in text), (
        f"exits 1 with image error message: {text}"
    )
    assert "good.jpg" in text, f"error names bad file good.jpg: {text}"
    assert not out.exists(), "no partial PDF written"
    assert not glob.glob(str(work / ".zip-img2pdf-*.tmp")), "no temp file residue"
    # failure with existing output should preserve it
    with open(out, "w", encoding="utf-8") as f:
        f.write("OLD")
    rc, _ = run_cli_inproc(zp, "-o", out)
    with open(out, encoding="utf-8") as f:
        assert out.exists() and f.read() == "OLD", "old output preserved"


def test_output_same_as_input(work: Path) -> None:
    zp = work / "same.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("a.jpg", buf.getvalue())
    rc, text = run_cli_inproc(zp, "-o", zp)
    assert rc == 2 and "cannot be the same as the input zip" in text, (
        f"rejected with exit 2: {text}"
    )
    assert zipfile.is_zipfile(zp), "zip intact"


def test_debug_flag(work: Path) -> None:
    zp = work / "debug.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("a.jpg", buf.getvalue())
    out = work / "debug.pdf"
    rc, text = run_cli_inproc(zp, "-o", out, "--debug")
    assert rc == 0, f"exit code 0: {text}"
    assert "DEBUG" in text, f"--debug emits DEBUG-level log records: {text}"
    assert "Converting a.jpg" in text, (
        f"--debug prints the file currently being processed: {text}"
    )


@pytest.mark.parametrize("fit", ["fill", "exact", "shrink", "enlarge"])
def test_fit_variants(work: Path, fit: str) -> None:
    zp = work / f"fit_{fit}.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("a.jpg", buf.getvalue())
    out = work / f"fit_{fit}.pdf"
    # fit only takes effect with an explicit --pagesize (default: natural size)
    rc, text = run_cli_inproc(zp, "-o", out, "--pagesize", "A4", "--fit", fit)
    assert rc == 0, f"exit code 0: {text}"
    assert out.exists() and len(media_boxes(out)) == 1, f"1 page for fit={fit}"


def test_input_not_found(work: Path) -> None:
    rc, text = run_cli_inproc(work / "nope.zip", "-o", work / "x.pdf")
    assert rc == 1 and "file not found" in text, f"exits 1 with message: {text}"


def test_input_is_dir(work: Path) -> None:
    rc, text = run_cli_inproc(work, "-o", work / "x.pdf")
    assert rc == 1 and "directory, not a file" in text, f"exits 1 with message: {text}"


def test_output_is_dir(work: Path) -> None:
    zp = work / "outdir.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("a.jpg", buf.getvalue())
    rc, text = run_cli_inproc(zp, "-o", work)
    assert rc == 1 and "output path is a directory" in text, (
        f"exits 1 with message: {text}"
    )


def test_output_dir_missing(work: Path) -> None:
    zp = work / "outmiss.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("a.jpg", buf.getvalue())
    rc, text = run_cli_inproc(zp, "-o", work / "nodir" / "x.pdf")
    assert rc == 1 and "output directory does not exist" in text, (
        f"exits 1 with message: {text}"
    )


def test_unicode_metadata(work: Path) -> None:
    zp = work / "unicode.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("a.jpg", buf.getvalue())
    out = work / "unicode.pdf"
    rc, text = run_cli_inproc(zp, "-o", out, "--title", "中文标题", "--author", "张三")
    assert rc == 0, f"exit code 0: {text}"
    with pikepdf.open(out) as pdf:
        d = pdf.docinfo
        assert str(d.get("/Title") or "") == "中文标题", f"Title: {d.get('/Title')!s}"
        assert str(d.get("/Author") or "") == "张三", f"Author: {d.get('/Author')!s}"


def test_fit_case_insensitive(work: Path) -> None:
    zp = work / "tiny.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("tiny.jpg", buf.getvalue())
    out = work / "fitcase.pdf"
    rc, text = run_cli_inproc(zp, "-o", out, "--pagesize", "A4", "--fit", "INTO")
    assert rc == 0, f"exit code 0: {text}"
    with pikepdf.open(out) as pdf:
        mb = pdf.pages[0].MediaBox
        box = [float(x) for x in mb]  # pyright: ignore[reportGeneralTypeIssues, reportUnknownArgumentType, reportUnknownVariableType]
        assert abs(box[2] - 595.28) < 1, "page is A4"


def test_password_zip(work: Path) -> None:
    import pyzipper  # pyright: ignore[reportMissingTypeStubs]

    zp = work / "pw.zip"
    zf = cast(
        zipfile.ZipFile,
        pyzipper.AESZipFile(  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            zp,
            "w",
            compression=pyzipper.ZIP_DEFLATED,  # pyright: ignore[reportUnknownMemberType]
            encryption=pyzipper.WZ_AES,  # pyright: ignore[reportUnknownMemberType]
        ),
    )
    with zf:
        zf.setpassword(b"secret")
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("a.jpg", buf.getvalue())
    out = work / "pw.pdf"
    rc, text = run_cli_inproc(zp, "-o", out)
    assert rc == 1 and not out.exists(), f"exits 1 and no PDF: rc={rc} {text[:120]}"


def test_garbage_image_named(work: Path) -> None:
    zp = work / "garbage.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr(
            "sub/ghost.jpg", "this is not an image at all"
        )  # magic bytes don't match
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("ok.jpg", buf.getvalue())
    out = work / "garbage.pdf"
    rc, text = run_cli_inproc(zp, "-o", out)
    assert rc == 1, f"exit 1: rc={rc}"
    assert "ghost.jpg" in text, f"error contains filename ghost.jpg: {text}"
    assert not out.exists(), "no PDF generated"


def test_truncated_image_named(work: Path) -> None:
    # The huge valid image (a_huge.jpg) converts fine under the default layout, but if
    # the trial conversion doesn't mirror layout_fun it would fail under a different
    # layout and be misjudged as broken -- regression locks in this fix
    zp = work / "trunc.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (20000, 1000)).save(buf, format="JPEG")
        zf.writestr("a_huge.jpg", buf.getvalue())
        buf = io.BytesIO()
        Image.new("RGB", (80, 60)).save(buf, format="JPEG")
        zf.writestr("z_cut.jpg", buf.getvalue()[:200])  # valid header + truncated body
    out = work / "trunc.pdf"
    rc, text = run_cli_inproc(zp, "-o", out)
    assert rc == 1, f"exit 1: rc={rc}"
    assert "z_cut.jpg" in text, f"error points to z_cut.jpg: {text}"
    assert "a_huge.jpg" not in text, f"huge valid image a_huge.jpg not blamed: {text}"
    assert not out.exists(), "no PDF generated"


def test_empty_image_file(work: Path) -> None:
    zp = work / "empty.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("empty.jpg", b"")
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("ok.jpg", buf.getvalue())
    out = work / "empty.pdf"
    rc, text = run_cli_inproc(zp, "-o", out)
    assert rc == 1, f"exit 1: rc={rc}"
    assert "empty.jpg" in text, f"error contains filename empty.jpg: {text}"
    assert not out.exists(), "no PDF generated"


def test_png_as_jpg(work: Path) -> None:
    zp = work / "pjpg.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (40, 40)).save(buf, format="PNG")
        zf.writestr("real.png.jpg", buf.getvalue())
    out = work / "pjpg.pdf"
    rc, text = run_cli_inproc(zp, "-o", out)
    assert rc == 0 and out.exists() and len(media_boxes(out)) == 1, (
        f"exits 0 with 1 page: {text}"
    )


def test_many_images(work: Path) -> None:
    zp = work / "many.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for i in range(30):
            size = (50, 50) if i % 2 == 0 else (300, 200)
            buf = io.BytesIO()
            Image.new("RGB", size).save(buf, format="JPEG")
            zf.writestr(f"img_{i:02d}.jpg", buf.getvalue())
    out = work / "many.pdf"
    rc, text = run_cli_inproc(zp, "-o", out)
    assert rc == 0, f"exit code 0: {text}"
    boxes: list[tuple[float, float]] = media_boxes(out) if out.exists() else []
    assert len(boxes) == 30, f"30 pages: pages={len(boxes)}"
    # default: pages match image natural sizes (50x50 -> 37.5x37.5, 300x200 -> 225x150)
    assert set(boxes) == {(37.5, 37.5), (225.0, 150.0)}, (
        f"pages match image natural sizes: boxes={set(boxes)}"
    )


def test_concurrent_output(work: Path) -> None:
    import time

    zp = work / "conc.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for i in range(5):
            buf = io.BytesIO()
            Image.new("RGB", (200 + i * 10, 150 + i * 10)).save(buf, format="JPEG")
            zf.writestr(f"p{i}.jpg", buf.getvalue())
    out = work / "conc.pdf"
    # launch two conversion processes writing the same target file
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "zip_img2pdf", zp, "-o", out],
            cwd=PROJ,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=CLI_ENV,
        )
        for _ in range(2)
    ]
    rcs: list[int] = []
    for p in procs:
        p.wait(timeout=180)
        p.communicate()  # drain pipes to avoid Windows pipe buffering issues
        rcs.append(p.returncode)
    # Two processes race to atomically replace the same output file. On Windows the
    # loser of the os.replace race can fail with access-denied, so tolerate one
    # failure (last-writer-wins); at least one must succeed and the result must be
    # a valid PDF.
    assert 0 in rcs, f"at least one concurrent process exits 0: rcs={rcs}"
    time.sleep(
        0.5
    )  # wait for the atomic replace (os.replace) to fully complete before opening
    boxes: list[tuple[float, float]] = media_boxes(out) if out.exists() else []
    assert len(boxes) == 5, f"PDF parseable with 5 pages: pages={len(boxes)}"
    assert not glob.glob(str(work / ".zip-img2pdf-*.tmp")), "no .tmp temp file residue"


def test_no_tempdir_leak(work: Path) -> None:
    # Isolate the tool's temp dir per worker: point TMP/TEMP at our own directory so
    # the scan is immune to other parallel workers' extraction dirs in the global temp.
    tmp = work / "leak_tmp"
    tmp.mkdir()
    leak_env: dict[str, str] = {"TMP": str(tmp), "TEMP": str(tmp)}

    def leftover() -> set[str]:
        return {n for n in os.listdir(tmp) if n.startswith("zip_img2pdf_")}

    good = work / "leak_ok.zip"
    with zipfile.ZipFile(good, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("a.jpg", buf.getvalue())
    bad = work / "leak_bad.zip"
    with open(bad, "w", encoding="utf-8") as f:
        f.write("not a zip")
    before = leftover()
    rc_good, _ = run_cli(good, "-o", work / "leak_ok.pdf", env=leak_env)
    assert rc_good == 0, f"successful conversion exits 0: rc={rc_good}"
    assert leftover() <= before, (
        f"no temp-dir residue after success: before={before} after={leftover()}"
    )
    rc_bad, _ = run_cli(bad, "-o", work / "leak_bad.pdf", env=leak_env)
    assert rc_bad == 1, f"bad zip exits 1: rc={rc_bad}"
    assert leftover() <= before, (
        f"no temp-dir residue after failure: before={before} after={leftover()}"
    )


def test_full_journey(work: Path) -> None:
    zp = work / "my_scans.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for name, ext, size in [
            ("scan 01.jpg", "JPEG", (800, 1200)),
            ("scan/page2.png", "PNG", (600, 900)),
            ("scan/page3.jpg", "JPEG", (700, 1000)),
        ]:
            buf = io.BytesIO()
            Image.new("RGB", size).save(buf, format=ext)
            zf.writestr(name, buf.getvalue())
    out = work / "journey.pdf"
    rc, text = run_cli_inproc(
        zp,
        "-o",
        out,
        "--title",
        "Quarterly Scans",
        "--author",
        "Zhang San",
        "--subject",
        "Office Archive",
        "--keywords",
        "scan",
        "archive",
        "--creator",
        "Office IT",
        "--pagesize",
        "A3",
        "--fit",
        "into",
    )
    assert rc == 0, f"exit code 0: {text}"
    assert out.exists(), "PDF generated"
    boxes = media_boxes(out)
    assert len(boxes) == 3, f"3 pages: pages={len(boxes)}"
    assert set(boxes) == {(841.89, 1190.55)}, (
        f"each page A3 portrait 841.89x1190.55: boxes={set(boxes)}"
    )
    with pikepdf.open(out) as pdf:
        d = pdf.docinfo
        assert str(d.get("/Title") or "") == "Quarterly Scans", (
            f"Title=Quarterly Scans: {d.get('/Title')!s}"
        )
        assert str(d.get("/Author") or "") == "Zhang San", (
            f"Author=Zhang San: {d.get('/Author')!s}"
        )
        assert str(d.get("/Subject") or "") == "Office Archive", (
            f"Subject=Office Archive: {d.get('/Subject')!s}"
        )
        assert str(d.get("/Keywords") or "") == "scan,archive", (
            f"Keywords=scan,archive: {d.get('/Keywords')!s}"
        )
        assert str(d.get("/Creator") or "") == "Office IT", (
            f"Creator=Office IT: {d.get('/Creator')!s}"
        )
        prod = str(d.get("/Producer") or "")
        assert (
            prod.startswith("zip-img2pdf ")
            and " (img2pdf " in prod
            and prod.endswith(")")
        ), (
            f"Producer starts with zip-img2pdf version, contains img2pdf version, ends with ): {prod}"
        )


def _malformed_trunc(work: Path) -> Path:
    zp = work / "m_trunc.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("a.jpg", buf.getvalue())
    with open(zp, "rb") as f:
        data = f.read()
    with open(zp, "wb") as f:
        f.write(data[: len(data) // 2])
    return zp


def _malformed_empty(work: Path) -> Path:
    zp = work / "m_empty.zip"
    with zipfile.ZipFile(zp, "w"):
        pass
    return zp


def _malformed_dirs(work: Path) -> Path:
    zp = work / "m_dirs.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("folder/", b"")
    return zp


def _malformed_long(work: Path) -> Path:
    zp = work / "m_long.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("x" * 300 + ".jpg", buf.getvalue())
    return zp


def _malformed_backslash(work: Path) -> Path:
    zp = work / "m_bs.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("sub\\evil.jpg", buf.getvalue())
    return zp


def _malformed_nest(work: Path) -> Path:
    zp = work / "m_nest.zip"
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("x.txt", "hi")
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("fake.jpg", inner.getvalue())
    return zp


def _malformed_text(work: Path) -> Path:
    zp = work / "m_txt.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("readme.txt", "hello")
        zf.writestr("docs/", b"")
    return zp


def _malformed_comment(work: Path) -> Path:
    zp = work / "m_cmt.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("a.jpg", buf.getvalue())
        zf.comment = b"zip comment with \x00 binary\xff bytes"
    return zp


def _malformed_illegal_nonimg(work: Path) -> Path:
    zp = work / "m_illegal_nonimg.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("a.jpg", buf.getvalue())
        # non-image member with characters illegal on Windows: must be skipped,
        # not fail the whole conversion (3.8 Path.resolve() would raise OSError)
        zf.writestr("readme<bad>.txt", "hi")
    return zp


# expected exit codes (probed empirically): malformed zips fail with 1, while
# harmless members (backslash name, binary zip comment) convert with 0
@pytest.mark.parametrize(
    "name,builder,expected_rc",
    [
        pytest.param("trunc", _malformed_trunc, 1, id="trunc"),
        pytest.param("empty", _malformed_empty, 1, id="empty"),
        pytest.param("dirs", _malformed_dirs, 1, id="dirs"),
        pytest.param("long", _malformed_long, 1, id="long"),
        pytest.param("backslash", _malformed_backslash, 0, id="backslash"),
        pytest.param("nest", _malformed_nest, 1, id="nest"),
        pytest.param("text", _malformed_text, 1, id="text"),
        pytest.param("comment", _malformed_comment, 0, id="comment"),
        pytest.param(
            "illegal_nonimg", _malformed_illegal_nonimg, 0, id="illegal_nonimg"
        ),
    ],
)
def test_malformed_input(
    work: Path, name: str, builder: Callable[[Path], Path], expected_rc: int
) -> None:
    zp = builder(work)
    out = work / f"m_out_{name}.pdf"
    rc, text = run_cli_inproc(zp, "-o", out)
    assert "Traceback" not in text, f"malformed {name}: no traceback: {text[:80]}"
    assert rc == expected_rc, (
        f"malformed {name}: rc={rc} expected {expected_rc}: {text[:80]}"
    )


def test_keyboard_interrupt_exit_130(work: Path) -> None:
    import zip_img2pdf.cli as mod  # pyright: ignore[reportMissingTypeStubs]

    zp = work / "ki.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("a.jpg", buf.getvalue())
    out = work / "ki.pdf"
    real_convert: object = mod.img2pdf.convert  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    def boom(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    mod.img2pdf.convert = boom  # pyright: ignore[reportUnknownMemberType]
    old_argv = sys.argv
    try:
        sys.argv = ["zip-img2pdf", str(zp), "-o", str(out)]
        try:
            mod.main()
        except SystemExit as exc:
            assert exc.code == 130, f"KeyboardInterrupt exits 130: {exc.code!s}"
        else:
            assert False, "KeyboardInterrupt exits 130: no SystemExit raised"
    finally:
        mod.img2pdf.convert = real_convert  # pyright: ignore[reportUnknownMemberType]
        sys.argv = old_argv


def test_utf8_output_strict(work: Path) -> None:
    zp = work / "中文扫描.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        buf = io.BytesIO()
        Image.new("RGB", (50, 50)).save(buf, format="JPEG")
        zf.writestr("图1.jpg", buf.getvalue())
    out = work / "utf8_out.pdf"
    proc = subprocess.run(
        [sys.executable, "-m", "zip_img2pdf", str(zp), "-o", str(out)],
        cwd=PROJ,
        capture_output=True,
        env=CLI_ENV,
        check=False,
    )
    strict = True
    text = ""
    try:
        text = proc.stdout.decode("utf-8", errors="strict") + proc.stderr.decode(
            "utf-8", errors="strict"
        )
    except UnicodeDecodeError as exc:
        strict = False
        text = str(exc)
    assert proc.returncode == 0, f"exit code 0: rc={proc.returncode}"
    assert strict, f"output decodes as strict UTF-8: {text[:120]}"
    assert out.exists(), "PDF generated"
    assert "Done" in text, f"output contains done marker: {text[:120]!r}"
