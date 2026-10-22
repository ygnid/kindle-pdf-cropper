"""Command-line interface for kindle-pdf-cropper."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import Config
from .pipeline import process, summarize


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kindle-crop",
        description=(
            "Auto-crop PDF margins / headers / footers for Kindle reading, "
            "optionally splitting every page into two landscape halves. "
            "Outputs PDF (text/vector preserved) or fixed-layout EPUB."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("input", metavar="INPUT.pdf", help="source PDF file")
    p.add_argument(
        "-o", "--output", metavar="PATH",
        help="output file (or stem). Default: <input stem>_cropped.pdf|epub",
    )
    p.add_argument(
        "--format", choices=["pdf", "epub", "both"], default="pdf",
        help="output format",
    )
    p.add_argument(
        "--split", action="store_true",
        help="split every page horizontally into two landscape pages with a "
        "small overlap (bigger text on Kindle)",
    )
    p.add_argument(
        "--split-overlap-pt", type=float, default=20.0,
        help="minimum overlap between the two halves, in points",
    )
    p.add_argument(
        "--split-overlap-ratio", type=float, default=0.03,
        help="overlap as a fraction of the cropped page height",
    )
    p.add_argument(
        "--padding", type=float, default=6.0, metavar="PT",
        help="extra padding kept around the content box, in points",
    )
    p.add_argument(
        "--no-header-footer", action="store_true",
        help="disable header/footer/page-number removal (only trim margins)",
    )
    p.add_argument(
        "--epub-mode", choices=["image", "svg", "auto"], default="auto",
        help="EPUB page strategy: bitmap pages, SVG text/vector pages, or auto",
    )
    p.add_argument(
        "--epub-dpi", type=int, default=200,
        help="raster DPI for EPUB bitmap pages",
    )
    p.add_argument(
        "--epub-image-format", choices=["jpeg", "png"], default="jpeg",
        help="bitmap format for EPUB pages",
    )
    p.add_argument(
        "--analysis-dpi", type=int, default=144,
        help="DPI used for margin/header/footer analysis",
    )
    p.add_argument(
        "--debug-dir", metavar="DIR",
        help="write per-page analysis overlays (PNG) into this directory",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        config = Config(
            margin_padding_pt=args.padding,
            split=args.split,
            split_overlap_ratio=args.split_overlap_ratio,
            split_overlap_min_pt=args.split_overlap_pt,
            header_footer_zone=0.0 if args.no_header_footer else 0.12,
            analysis_dpi=args.analysis_dpi,
            epub_dpi=args.epub_dpi,
            epub_image_format=args.epub_image_format,
            epub_mode=args.epub_mode,
            debug_dir=args.debug_dir,
        ).derived()
        formats = ("pdf", "epub") if args.format == "both" else (args.format,)

        if args.output:
            from pathlib import Path

            out = Path(args.output)
            if len(formats) == 1:
                out_dir, out_stem = out.parent, out.stem
            else:
                out_dir, out_stem = out.parent, out.stem
        else:
            out_dir, out_stem = None, None

        files, plans = process(
            args.input, config, formats=formats,
            out_dir=out_dir, out_stem=out_stem,
        )
        print(summarize(plans))
        for f in files:
            print(f"wrote {f}")
        return 0
    except Exception as exc:  # noqa: BLE001 -- CLI boundary
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
