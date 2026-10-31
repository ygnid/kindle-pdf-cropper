"""Configuration for the cropping pipeline.

Every knob that affects how a page is analyzed or rendered lives here so the
CLI and the library API stay in sync.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Config:
    # -- margin cropping ---------------------------------------------------
    #: Extra padding (PDF points) kept around the detected content box so
    #: antialiasing / glyph overhangs are never clipped.
    margin_padding_pt: float = 6.0
    #: Fraction of page height used as the "header/footer zone" that is
    #: eligible for repeated-text / page-number / running-strip detection.
    header_footer_zone: float = 0.20
    #: Minimum whitespace gap (relative to median line height) that must
    #: separate a header/footer band from the body before we crop it away.
    hf_gap_factor: float = 0.6
    #: A text line counts as "repeated" if it appears on at least this share
    #: of the pages (running headers / footers).
    repeat_min_share: float = 0.30
    #: Never crop away more than this share of the page height in total.
    #: Header + footer + their surrounding blank bands can legitimately add
    #: up to ~1/3 of a sparse page, so keep this generous.
    max_trim_share: float = 0.35
    #: Remove watermarks / repeated background patterns on scanned pages
    #: (detected as pixels that are consistently darker than paper across
    #: many pages, while real text pixels vary).
    remove_watermark: bool = True
    #: Minimum fraction of a page *dimension* (width and height) that must
    #: remain after cropping; below that the page is kept as-is (protects
    #: against degenerate crops on nearly empty pages).
    min_content_share: float = 0.15

    # -- page split (landscape for Kindle) --------------------------------
    #: Enabled via --split. Each page is cut horizontally into two pages.
    #:
    #: Two split strategies:
    #:  * adaptive (default, when no overlap parameter is given): the cut
    #:    line is placed in the whitespace *between two lines* (text bboxes
    #:    for text PDFs, blank ink bands for scanned PDFs), so no line is
    #:    ever cut and no overlap is needed.
    #:  * overlap (when any overlap parameter is set): the page is cut at
    #:    the vertical middle and the two halves overlap by
    #:    max(ratio * height, min_pt).
    split: bool = False
    #: Overlap between the two halves, as a fraction of the *cropped* height.
    split_overlap_ratio: float | None = None
    #: ...and at least this many points (so a normal text line survives).
    split_overlap_min_pt: float | None = None

    # -- rendering ----------------------------------------------------------
    #: DPI used for content analysis (raster projections).
    analysis_dpi: int = 144
    #: DPI used for EPUB page images.
    epub_dpi: int = 200
    #: Cap on the longest EPUB image side (pixels) to keep file sizes sane.
    epub_max_side: int = 4096
    #: EPUB image format: "jpeg" or "png".
    epub_image_format: str = "jpeg"
    #: JPEG quality for EPUB images.
    epub_jpeg_quality: int = 88
    #: EPUB page mode: "image" (bitmap pages), "svg" (text/vector pages) or
    #: "auto" (svg for text pages, image for scanned pages).
    epub_mode: str = "auto"

    #: Write per-page analysis overlays (PNG) into this directory when set.
    debug_dir: str | None = None

    #: Smallest connected run of ink (analysis pixels) treated as content.
    min_run_ratio: float = 0.0015

    def derived(self) -> "Config":
        """Return a copy with derived fields validated/normalized."""
        import copy

        c = copy.copy(self)
        c.analysis_dpi = max(72, min(400, c.analysis_dpi))
        c.epub_dpi = max(72, min(600, c.epub_dpi))
        if c.epub_mode not in ("image", "svg", "auto"):
            raise ValueError("epub_mode must be one of: image, svg, auto")
        if c.epub_image_format not in ("jpeg", "png"):
            raise ValueError("epub_image_format must be jpeg or png")
        if c.split_overlap_ratio is not None and not 0.0 < c.split_overlap_ratio < 0.25:
            raise ValueError("split_overlap_ratio must be in (0, 0.25)")
        if c.split_overlap_min_pt is not None and c.split_overlap_min_pt < 1.0:
            raise ValueError("split_overlap_min_pt must be >= 1 pt")
        return c
