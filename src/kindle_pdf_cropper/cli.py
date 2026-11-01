"""Command-line interface for kindle-pdf-cropper."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import KINDLE_DEVICE_RATIOS, Config
from .pipeline import process, summarize


def _tui_available() -> bool:
    """True only for an interactive terminal (never in pipes/CI/tests)."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def _device_menu_items() -> list[tuple[str, object]]:
    """Menu entries: (label, value).  value is None, "manual" or a device key."""
    items: list[tuple[str, object]] = [
        ("No limit (keep the split page as-is)", None),
        ("Enter a ratio manually...", "manual"),
    ]
    for name in sorted(KINDLE_DEVICE_RATIOS):
        ratio = KINDLE_DEVICE_RATIOS[name]
        items.append((f"{name}  (max H/W {ratio:.3f})", name))
    return items


def _enable_ansi() -> None:
    """Enable ANSI escape processing on legacy Windows consoles."""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def _read_key() -> str:
    """Return 'up', 'down', 'enter' or 'escape' (Escape = cancel/no limit)."""
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "")
        if ch == "\x1b":
            seq = ""
            while msvcrt.kbhit() and len(seq) < 2:
                seq += msvcrt.getwch()
            return {"[A": "up", "[B": "down"}.get(seq, "escape")
        if ch in ("\r", "\n"):
            return "enter"
        if ch.lower() == "q":
            return "escape"
        return ""

    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ready, _, _ = select.select([sys.stdin], [], [], 0.05)
            if ready:
                seq = sys.stdin.read(2)
                return {"[A": "up", "[B": "down"}.get(seq, "escape")
            return "escape"
        if ch in ("\r", "\n"):
            return "enter"
        if ch.lower() == "q":
            return "escape"
        return ""
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_manual_ratio() -> float | None:
    while True:
        try:
            raw = input("Max height/width ratio (0-1, e.g. 0.75): ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if not raw:
            return None
        try:
            ratio = float(raw)
        except ValueError:
            print("Please enter a number, e.g. 0.75")
            continue
        if 0.0 < ratio <= 1.0:
            return ratio
        print("Ratio must be in (0, 1].")


def _prompt_split_device() -> tuple[str | None, float | None]:
    """Interactive up/down picker for the split-page aspect ratio.

    Returns ``(device_type, max_ratio)``; ``(None, None)`` means no limit.
    """
    items = _device_menu_items()
    selected = 0
    _enable_ansi()

    def render(first: bool) -> None:
        lines = [
            "Select a Kindle model for the split-page aspect ratio "
            "(up/down, Enter, q = no limit):"
        ]
        for i, (label, _) in enumerate(items):
            line = f"  {'>' if i == selected else ' '} {label}"
            if i == selected:
                line = f"\033[7m{line}\033[0m"
            lines.append(line + "\033[K")
        if not first:
            sys.stdout.write(f"\033[{len(lines)}A")
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()

    first = True
    while True:
        render(first)
        first = False
        key = _read_key()
        if key == "up":
            selected = (selected - 1) % len(items)
        elif key == "down":
            selected = (selected + 1) % len(items)
        elif key == "enter":
            break
        elif key == "escape":
            selected = 0  # no limit
            break

    sys.stdout.write("\n")
    sys.stdout.flush()
    value = items[selected][1]
    if value == "manual":
        return None, _read_manual_ratio()
    if value is None:
        return None, None
    return str(value), None


def _maybe_prompt_split_device(args: argparse.Namespace) -> None:
    """Show the device picker when `--split` has no aspect option yet."""
    if (
        not args.split
        or args.device_type is not None
        or args.max_aspect_ratio is not None
        or not _tui_available()
    ):
        return
    try:
        device, ratio = _prompt_split_device()
    except KeyboardInterrupt:
        return
    args.device_type = device
    if ratio is not None:
        args.max_aspect_ratio = ratio


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
        help="output file (or stem). Default: <input stem>_cropped.pdf|epub "
        "(with --split: <input stem>_split.pdf|epub)",
    )
    p.add_argument(
        "--format", choices=["pdf", "epub", "both"], default="pdf",
        help="output format",
    )
    p.add_argument(
        "--split", action="store_true",
        help="split every page horizontally into two landscape pages. "
        "By default the cut is placed automatically in the whitespace "
        "between two lines (no overlap, nothing is cut); set an overlap "
        "parameter below to use a middle-cut with overlap instead",
    )
    p.add_argument(
        "--split-overlap-pt", type=float, default=None, metavar="PT",
        help="enable overlap mode: minimum overlap between the two halves, "
        "in points (default: adaptive line-gap split without overlap)",
    )
    p.add_argument(
        "--split-overlap-ratio", type=float, default=None,
        help="enable overlap mode: overlap as a fraction of the cropped "
        "page height (default: adaptive line-gap split without overlap)",
    )
    aspect = p.add_mutually_exclusive_group()
    aspect.add_argument(
        "--max-aspect-ratio", type=float, default=None, metavar="H/W",
        help="with --split: maximum height/width ratio of each output page "
        "(e.g. 0.75 for a 3:4 Kindle). Taller pages get blank side margins",
    )
    aspect.add_argument(
        "--device-type", default=None, metavar="NAME",
        help="with --split: Kindle model whose landscape screen ratio is the "
        "maximum (e.g. kindle, paperwhite-5, paperwhite-12, oasis-2, "
        "scribe, colorsoft, dx)",
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
        "--no-watermark-removal", action="store_true",
        help="disable watermark / repeated-pattern removal on scanned pages",
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
    _maybe_prompt_split_device(args)
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
            split_max_aspect_ratio=args.max_aspect_ratio,
            device_type=args.device_type,
            header_footer_zone=0.0 if args.no_header_footer else 0.20,
            remove_watermark=not args.no_watermark_removal,
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
