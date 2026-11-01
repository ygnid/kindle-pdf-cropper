"""Configuration for the cropping pipeline.

Every knob that affects how a page is analyzed or rendered lives here so the
CLI and the library API stay in sync.
"""

from __future__ import annotations

from dataclasses import dataclass


#: Kindle e-reader screen aspect ratios as *portrait width / portrait height*.
#: A Kindle in landscape has height/width = portrait_width / portrait_height,
#: so this is also the maximum height/width ratio for a split page that must
#: fit on one screen.  Resolutions are the native panel resolutions published
#: by Amazon (e.g. Paperwhite 5 1236x1648, Scribe 1860x2480).
KINDLE_DEVICE_RATIOS: dict[str, float] = {
    "kindle": 1072 / 1448,          # base Kindle 11th/12th gen (2022/2024)
    "kindle-10": 600 / 800,         # base Kindle 10th gen (2019)
    "kindle-11": 1072 / 1448,
    "paperwhite-1": 758 / 1024,
    "paperwhite-2": 758 / 1024,
    "paperwhite-3": 1072 / 1448,
    "paperwhite-4": 1072 / 1448,
    "paperwhite-5": 1236 / 1648,    # 11th gen (2021)
    "paperwhite-12": 1264 / 1680,   # 12th gen (2024)
    "voyage": 1072 / 1448,
    "oasis-1": 1072 / 1448,
    "oasis-2": 1264 / 1680,
    "oasis-3": 1264 / 1680,
    "scribe": 1860 / 2480,
    "colorsoft": 1264 / 1680,
    "dx": 824 / 1200,
}

#: Family names map to the *most restrictive* ratio in that family, so the
#: result fits every generation even when the exact model is unknown.
_KINDLE_DEVICE_ALIASES: dict[str, str] = {
    "paperwhite": "paperwhite-2",
    "kpw": "paperwhite-2",
    "pw": "paperwhite-2",
    "paperwhite-11": "paperwhite-5",
    "paperwhite-11th-gen": "paperwhite-5",
    "paperwhite-12th-gen": "paperwhite-12",
    "oasis": "oasis-1",
    "oasis-2nd-gen": "oasis-2",
    "oasis-3rd-gen": "oasis-3",
    "scribe-2022": "scribe",
    "scribe-2024": "scribe",
    "kindle-2022": "kindle",
    "kindle-2024": "kindle",
    "kindle-12": "kindle",
    "kindle-12th-gen": "kindle",
    "kindle-11th-gen": "kindle-11",
}


#: Special `device_type` values that explicitly mean "no ratio limit".
_NO_DEVICE_VALUES = {"none", "no", "off", "unlimited", "native", "-"}


def kindle_device_ratio(name: str) -> float | None:
    """Look up a Kindle model name and return its landscape height/width ratio."""
    key = name.strip().lower().replace("_", "-").replace(" ", "-")
    if key.startswith("amazon-"):
        key = key[len("amazon-"):]
    key = _KINDLE_DEVICE_ALIASES.get(key, key)
    if key.startswith("kindle-") and key not in KINDLE_DEVICE_RATIOS:
        key = key[len("kindle-"):]
        # resolve family aliases such as "kindle-paperwhite" / "kindle-oasis"
        key = _KINDLE_DEVICE_ALIASES.get(key, key)
    return KINDLE_DEVICE_RATIOS.get(key)


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
    #: Maximum height/width ratio for split output pages.  Kindle screens in
    #: landscape have height/width = portrait_width / portrait_height (e.g.
    #: 0.75 for a 3:4 panel); pages that are taller are padded with blank
    #: space on the left and right instead of needing two screenfuls.
    split_max_aspect_ratio: float | None = None
    #: Kindle model name; derives `split_max_aspect_ratio` from its screen.
    device_type: str | None = None

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
        if c.device_type is not None:
            if not c.split:
                raise ValueError("device_type requires split=True")
            if c.device_type.strip().lower() in _NO_DEVICE_VALUES:
                c.device_type = None
            else:
                ratio = kindle_device_ratio(c.device_type)
                if ratio is None:
                    available = ", ".join(sorted(KINDLE_DEVICE_RATIOS))
                    raise ValueError(
                        f"unknown device_type {c.device_type!r}; "
                        f"choose from: {available}"
                    )
                if (
                    c.split_max_aspect_ratio is not None
                    and abs(c.split_max_aspect_ratio - ratio) > 1e-12
                ):
                    raise ValueError(
                        "device_type and split_max_aspect_ratio "
                        "are mutually exclusive"
                    )
                c.split_max_aspect_ratio = ratio
        if c.split_max_aspect_ratio is not None:
            if not c.split:
                raise ValueError("split_max_aspect_ratio requires split=True")
            if not 0.0 < c.split_max_aspect_ratio <= 1.0:
                raise ValueError(
                    "split_max_aspect_ratio must be in (0, 1] "
                    "(page height / page width)"
                )
        return c
