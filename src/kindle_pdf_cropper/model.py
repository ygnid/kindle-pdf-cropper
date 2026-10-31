"""Core data model shared by analysis and the output writers."""

from __future__ import annotations

from dataclasses import dataclass, field

import pymupdf as fitz


@dataclass
class LineRecord:
    """One text line extracted from a page (in page coordinates)."""

    bbox: fitz.Rect
    text: str
    size: float
    #: letters/digits only, whitespace collapsed -- used for "running header"
    #: repeat detection and page-number heuristics
    norm_text: str
    #: vertical centre of the line as a fraction of page height (0..1)
    vfrac: float


@dataclass
class PageFeatures:
    """Everything the analysis pass needs to remember about one page."""

    index: int
    width: float  # pt
    height: float  # pt
    pt_per_px: float
    #: down-sampled binary ink mask (block of `block` analysis pixels -> 1 cell)
    ink: "np.ndarray"  # bool 2-D, H x W in block units
    block: int  # analysis pixels per ink cell
    row_sums: "np.ndarray"  # int, one entry per ink row (blocks)
    col_sums: "np.ndarray"  # int, one entry per ink col (blocks)
    pix_rows: "np.ndarray"  # int, per-pixel row ink counts (analysis px)
    lines: list[LineRecord] = field(default_factory=list)
    text_chars: int = 0
    image_area: float = 0.0
    image_bboxes: list[fitz.Rect] = field(default_factory=list)
    is_scanned: bool = False
    had_rotation: bool = False
    blank: bool = False
    #: full-page dark image (book cover, photo plate): keep the page whole
    photo_page: bool = False

    # filled by pass 2 -------------------------------------------------------
    crop: fitz.Rect | None = None
    removed_top: float | None = None  # pt: bottom edge of removed header band
    removed_bottom: float | None = None  # pt: top edge of removed footer band
    # internal analysis scratch (not part of the public model)
    _repeat_top: "set[str]" = None
    _repeat_bottom: "set[str]" = None
    _rep_top: bool = False
    _rep_bottom: bool = False
    _line_gap_pt: float | None = None
    _band_top_end: float | None = None
    _band_bot_start: float | None = None


@dataclass
class Segment:
    """One output page derived from one source page."""

    clip: fitz.Rect  # source-page rectangle (pt) shown on this output page
    width: float
    height: float


@dataclass
class PagePlan:
    """Final per-page plan handed to the writers."""

    index: int
    width: float
    height: float
    is_scanned: bool
    crop: fitz.Rect
    segments: list[Segment]
    removed_top: float | None = None
    removed_bottom: float | None = None
