"""High-level pipeline tying analysis and output writers together."""

from __future__ import annotations

import logging
from pathlib import Path

import pymupdf as fitz

from .analysis import analyze_document, normalize_work_doc, write_debug_overlays
from .config import Config
from .model import PageFeatures, PagePlan
from .output_epub import write_epub
from .output_pdf import write_pdf

log = logging.getLogger(__name__)

DEFAULT_FORMATS = ("pdf",)


def process(
    input_path: str | Path,
    config: Config,
    formats: tuple[str, ...] = DEFAULT_FORMATS,
    out_dir: str | Path | None = None,
    out_stem: str | None = None,
) -> tuple[list[Path], list[PagePlan]]:
    """Crop `input_path` and write the requested formats.

    Returns ``(output_files, plans)``.
    """
    input_path = Path(input_path)
    config = config.derived()
    out_dir = Path(out_dir) if out_dir else input_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    default_suffix = "_split" if config.split else "_cropped"
    stem = out_stem or (input_path.stem + default_suffix)

    src = fitz.open(str(input_path))
    try:
        if len(src) == 0:
            raise ValueError(f"{input_path}: PDF contains no pages")
        meta = src.metadata
        toc = src.get_toc()
        log.info("normalizing %d page(s)", len(src))
        work, rotations = normalize_work_doc(src)
        try:
            plans, feats = analyze_document(work, rotations, config)
            if config.debug_dir:
                write_debug_overlays(work, feats, plans, config.debug_dir, config)

            outputs: list[Path] = []
            for fmt in formats:
                if fmt == "pdf":
                    out = out_dir / f"{stem}.pdf"
                    write_pdf(work, plans, out, meta, toc)
                elif fmt == "epub":
                    title = (meta or {}).get("title") or input_path.stem
                    out = out_dir / f"{stem}.epub"
                    write_epub(
                        work,
                        plans,
                        out,
                        config,
                        title=title,
                        src_meta=meta,
                        src_toc=toc,
                    )
                else:
                    raise ValueError(f"unknown output format: {fmt}")
                outputs.append(out)
            return outputs, plans
        finally:
            work.close()
    finally:
        src.close()


def summarize(plans: list[PagePlan], feats: list[PageFeatures] | None = None) -> str:
    """One-line human summary of the analysis result."""
    n = len(plans)
    if n == 0:
        return "no pages"
    trim_share = []
    for p in plans:
        area = p.width * p.height
        crop = p.crop
        keep = (crop.width * crop.height) / area if area else 1.0
        trim_share.append(1.0 - keep)
    hf_removed = sum(
        1 for p in plans if p.removed_top is not None or p.removed_bottom is not None
    )
    total_pages = sum(len(p.segments) for p in plans)
    return (
        f"{n} source page(s) -> {total_pages} output page(s), "
        f"avg area trimmed {sum(trim_share) / max(n, 1) * 100:.1f}%, "
        f"header/footer removed on {hf_removed} page(s)"
    )
