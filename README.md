# zip-img2pdf

Losslessly convert the raster images in a zip archive into a single PDF file.

`zip-img2pdf` extracts the images from a zip archive and combines them into one
PDF using [img2pdf](https://pypi.org/project/img2pdf), which embeds images
losslessly without re-encoding.

## Installation

```bash
pip install zip-img2pdf
```

Requires Python 3.8 or newer.

## Usage

```bash
zip-img2pdf <input.zip>
```

This converts the images inside `input.zip` into `input.pdf` (the output is
named after the zip by default).

### Options

| Option | Description |
| --- | --- |
| `-o, --output` | Output PDF path (defaults to the zip's name) |
| `--title` | Sets the PDF title metadata |
| `--author` | Sets the PDF author metadata |
| `--subject` | Sets the PDF subject metadata |
| `--keywords` | Sets the PDF keywords (repeatable) |
| `--creator` | Sets the PDF creator metadata |
| `--producer` | Sets the PDF producer metadata |
| `--pagesize` | Uniform page size (e.g. `A4`, `A3`, `Letter`, `210mmx297mm`; append `^T` for landscape) |
| `--fit` | How to place images on the page: `into`, `fill`, `exact`, `shrink`, `enlarge` (default `shrink`) |
| `--debug` | Enable debug-level logging |

### Examples

```bash
zip-img2pdf scans.zip --title "My Scans" --author "Zhang San"
zip-img2pdf photos.zip -o album.pdf --pagesize A4 --fit into
```

### Supported image formats

`jpg`, `jpeg`, `png`, `bmp`, `gif`, `webp`, `tif`, `tiff`

Files are sorted naturally by name (`img2` comes before `img10`), and
subfolders are searched recursively.

## License

[MIT](LICENSE)
