# AGENTS.md

## Setup and verification

- Python >= 3.10, src-layout package. Tests import `kindle_pdf_cropper` by name, so install first:
  `pip install -e .[dev]`
- Full suite: `python -m pytest -q` (fast; all PDFs are generated synthetically, no network/services).
- One test: `python -m pytest tests/test_split.py::test_adaptive_split_no_overlap_and_between_lines -q`
- No ruff/mypy/black/pre-commit config exists; pytest is the only verification gate.
- Reusable fixtures live in `tests/conftest.py`: text, scan, rotated scan, tight scan, watermark scan. Use them for analysis regressions instead of inventing new PDFs. `tests/make_samples.py` can regenerate `examples/` samples (its `__main__` supports `text` and `scan` only).

## Architecture

- Flow: `cli.py` flags -> `Config` -> `pipeline.process` -> `analysis.analyze_document` -> `output_pdf.write_pdf` / `output_epub.write_epub`.
- `config.py` is the single source of truth for knobs. `Config.derived()` validates and clamps values; call it before use. Add new CLI options by adding a `Config` field and mapping it in `cli.main`, not by hardcoding behavior in `analysis.py`.
- `analysis.py` is raster-first: every page is rendered grayscale -> ink mask -> row/column projections, with text extraction supplying page-number/running-header signals. Two passes: per-page features, then document-level watermark/repetition detection, then final crop/split plans.
- `model.py` defines `PageFeatures`, `PagePlan`, `Segment`. Output writers only consume `PagePlan.segments`; add a format by writing an `output_*.py` and wiring it into `pipeline.process` plus the CLI `--format` choices.

## Critical gotchas

- Always call `normalize_work_doc(src)` before analysis. It bakes `/Rotate` into page content so text bboxes, raster pixels, and clip rects all share displayed-orientation coordinates. Never mix rects from `src` with rects from the normalized `work` doc.
- `normalize_work_doc` returns a copy. Read metadata/TOC from the original `src` (as `pipeline.process` does), and close both documents.
- Split default is adaptive: the cut goes into whitespace between lines (text bboxes first, else per-pixel blank bands), with no overlap. Overlap mode activates only when `split_overlap_ratio` or `split_overlap_min_pt` is set. Tests cover both modes; preserve the distinction.
- PDF output creates new pages sized to the crop and uses `Page.show_pdf_page`, so text/vector content is preserved without rasterizing (not merely a CropBox change). EPUB is EPUB3 fixed-layout: `auto` means SVG for text pages and bitmaps for scans. Do not introduce reflow.
- Light watermarks are removed by the strong-ink threshold itself; the cross-page repeated-pattern mask only cleans the per-pixel split profile. The block ink mask is intentionally left intact so repeated running headers/footers stay detectable as bands. Border-connected thin blobs and isolated specks are dropped separately in `analysis.py` (`_remove_border_blobs`, `_binary_open`).
- `batch_crop.py` is a standalone local helper that shells out to `kindle-crop.exe`; it is not part of the installed package.
- `examples/` contains local gitignored PDFs/EPUBs; do not commit them. `err.txt` and `out.txt` are untracked scratch files.

## Release

- CI (`.github/workflows/publish.yml`) runs pytest on pushes/PRs to `main`; pushing a `v*` tag builds and publishes to PyPI via trusted publishing.
- Version lives in both `pyproject.toml` and `src/kindle_pdf_cropper/__init__.py`; update both when bumping.
