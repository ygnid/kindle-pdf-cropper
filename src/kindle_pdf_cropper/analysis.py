"""Page content analysis: adaptive margin / header / footer detection.

The analysis is deliberately *raster-first*: every page is rendered to a
grayscale bitmap and turned into a binary "ink" mask, so text pages, scanned
pages and mixed pages all go through the same projection machinery.  Text
extraction is used as a second, higher-level signal for running headers and
page numbers (which a naive ink projection would happily keep).

Coordinate notes
----------------
The caller normalizes the document first (`normalize_work_doc`) which bakes
each page's /Rotate into the content (`Page.remove_rotation`).  After that,
page coordinates are unrotated *and* equal the displayed orientation, so all
bboxes/rects produced here may be used directly for clipping.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

import numpy as np
import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFilter

from .config import Config
from .model import LineRecord, PageFeatures, PagePlan, Segment

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def normalize_work_doc(src: fitz.Document) -> tuple[fitz.Document, list[int]]:
    """Return a copy of `src` with every page's rotation baked into its
    content, plus the list of original rotations.

    The pages are copied verbatim (so /Rotate and CropBox survive) and then
    ``Page.remove_rotation`` turns each displayed orientation into unrotated
    coordinates.  Afterwards ``get_text`` / ``get_pixmap`` agree on a single
    coordinate space that equals the displayed page.
    """
    work = fitz.open()
    rotations: list[int] = []
    for pno in range(len(src)):
        rotations.append(src[pno].rotation)
    work.insert_pdf(src, from_page=0, to_page=len(src) - 1)
    for pno in range(len(work)):
        work[pno].remove_rotation()
    return work, rotations


def _threshold(gray: np.ndarray) -> int:
    """Robust document binarization threshold.

    Finds the two dominant histogram modes -- the page background and the
    darkest content mode -- and returns their midpoint.  This is far more
    stable than plain Otsu for documents where a small but very dark cluster
    (anti-aliased text cores) would otherwise dominate the between-class
    variance.
    """
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 128
    bg = int(np.argmax(hist))
    dark_hist = hist[:bg]
    if dark_hist.sum() < 0.005 * total:
        # no real dark content (blank / photo-only page): keep a mid default
        return 128
    dark = int(np.argmax(dark_hist))
    t = int((bg + dark) / 2)
    return max(30, min(245, t))


_ROMAN = set("IVXLCDM")


def _is_page_number(line: LineRecord) -> bool:
    """Best-effort detection of page-number-like lines (e.g. ``- 12 -``,
    ``42``, ``Page 12``, ``— iii —``)."""
    if len(line.norm_text) > 10 or not line.norm_text:
        return False
    t = line.text.strip()
    if not t:
        return False
    # strip typical decorations: dashes, dots, bullets, whitespace, parens
    core = re.sub(r"[\s\-–—−·•.()\[\]{}<>'\"`~,，。、/\\|]", "", t)
    if not core:
        return False
    if core.isdigit():
        return True
    if all(ch in _ROMAN for ch in core):
        return True
    # "Page 12" / "第 12 页" / "p. 12" style
    m = re.fullmatch(
        r"(page|pg|p|第)?\s*(\d{1,4})\s*(页)?", core, re.IGNORECASE
    )
    return bool(m and m.group(2))


def _row_groups(active: np.ndarray, merge_gap: int) -> list[tuple[int, int]]:
    """Group consecutive active rows, merging gaps <= merge_gap (inclusive)."""
    groups: list[tuple[int, int]] = []
    start: int | None = None
    last = -10**9
    for i, a in enumerate(active):
        if a:
            if start is None or i - last > merge_gap + 1:
                if start is not None:
                    groups.append((start, last))
                start = i
            last = i
    if start is not None:
        groups.append((start, last))
    return groups


# --------------------------------------------------------------------------
# pass 1: per-page feature extraction
# --------------------------------------------------------------------------


def _binarize(gray: np.ndarray) -> np.ndarray:
    """Grayscale page -> binary ink mask (same rules as pass 1)."""
    t = _threshold(gray)
    ink = gray < t
    if ink.mean() > 0.6:  # dark-background page (negative scan): flip
        ink = ~ink
    return ink


def _page_features(
    page: fitz.Page, index: int, had_rotation: bool, config: Config
) -> PageFeatures:
    width, height = page.rect.width, page.rect.height
    # Cap the analysis raster for unusually large pages (posters, A0 scans).
    max_side_px = 2600
    dpi = min(
        config.analysis_dpi,
        max(72, int(max_side_px * 72.0 / max(width, height))),
    )
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
    gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width
    )
    # 3x3 median filter kills isolated sensor speckles in scans.
    gray = np.asarray(
        Image.fromarray(gray, mode="L").filter(ImageFilter.MedianFilter(3)),
        dtype=np.uint8,
    )

    ink = _binarize(gray)

    pix_rows = ink.sum(axis=1).astype(np.int32)

    h, w = ink.shape
    block = max(1, min(8, min(h, w) // 4))
    hb, wb = h // block, w // block
    if hb < 1 or wb < 1:
        block = 1
        hb, wb = h, w
    inkb = ink[: hb * block, : wb * block].reshape(
        hb, block, wb, block
    ).any(axis=(1, 3))

    row_sums = inkb.sum(axis=1).astype(np.int32)
    col_sums = inkb.sum(axis=0).astype(np.int32)
    pt_per_px = width / pix.width

    # --- text / image metadata -------------------------------------------
    lines: list[LineRecord] = []
    text_chars = 0
    try:
        data = page.get_text("dict")
    except Exception:
        data = {"blocks": []}
    for b in data.get("blocks", []):
        if b.get("type") != 0:
            continue
        for line in b.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            bbox: fitz.Rect | None = None
            texts: list[str] = []
            size = 0.0
            for s in spans:
                r = fitz.Rect(s["bbox"])
                bbox = r if bbox is None else (bbox | r)
                texts.append(s.get("text", ""))
                size = max(size, s.get("size", 0.0))
            text = "".join(texts)
            if not text.strip():
                continue
            norm = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", text)
            if not norm:
                continue
            text_chars += len(norm)
            lines.append(
                LineRecord(
                    bbox=bbox,
                    text=text,
                    size=size,
                    norm_text=norm,
                    vfrac=((bbox.y0 + bbox.y1) / 2.0) / max(height, 1e-9),
                )
            )
    try:
        imgs = page.get_image_info()
    except Exception:
        imgs = []
    image_area = 0.0
    image_bboxes: list[fitz.Rect] = []
    for im in imgs:
        r = fitz.Rect(im["bbox"])
        image_bboxes.append(r)
        image_area += abs(r.width) * abs(r.height)

    page_area = max(width * height, 1e-9)
    is_scanned = image_area >= 0.5 * page_area and text_chars < 300

    active_rows = row_sums >= max(2, int(config.min_run_ratio * wb))
    blank = not bool(active_rows.any()) and text_chars == 0

    return PageFeatures(
        index=index,
        width=width,
        height=height,
        pt_per_px=pt_per_px,
        ink=inkb,
        block=block,
        row_sums=row_sums,
        col_sums=col_sums,
        pix_rows=pix_rows,
        lines=lines,
        text_chars=text_chars,
        image_area=image_area,
        image_bboxes=image_bboxes,
        is_scanned=is_scanned,
        had_rotation=had_rotation,
        blank=blank,
    )


# --------------------------------------------------------------------------
# pass 2: per-page crop decision
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# watermark removal (scanned pages)
# --------------------------------------------------------------------------


def _compute_watermark_mask(
    work: fitz.Document,
    feats_list: list[PageFeatures],
    config: Config,
    sample_n: int = 40,
) -> "np.ndarray | None":
    """Build a pixel-level watermark mask from a spread page sample.

    A watermark / running header / page furniture pixel is *consistently*
    darker than paper on (almost) every page, while a text pixel is dark only
    on the few pages where that exact glyph position is occupied -- so its
    median gray across pages stays at the paper background value.

    Returns a boolean mask (True = watermark pixel) or None when the document
    is too small or has no obvious repeated pattern.
    """
    n = len(feats_list)
    if n < 4:
        return None
    idxs = np.linspace(0, n - 1, min(sample_n, n)).astype(int)
    dpi = config.analysis_dpi
    grays: list[np.ndarray] = []
    hmax = wmax = 0
    for i in idxs:
        pix = work[int(i)].get_pixmap(
            dpi=dpi, colorspace=fitz.csGRAY, alpha=False
        )
        g = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width
        )
        grays.append(g)
        hmax = max(hmax, g.shape[0])
        wmax = max(wmax, g.shape[1])
    stack = np.zeros((len(grays), hmax, wmax), dtype=np.uint8)
    for si, g in enumerate(grays):
        stack[si, : g.shape[0], : g.shape[1]] = g
    med = np.median(stack, axis=0)
    bg = float(np.median(med))  # paper background
    mask = med < (bg - 8.0)
    # no obvious watermark -> nearly-empty mask
    if mask.mean() < 0.002:
        return None
    return mask


def _apply_watermark_removal(
    work: fitz.Document,
    feats_list: list[PageFeatures],
    wm_pix: "np.ndarray",
    config: Config,
) -> None:
    """Subtract watermark pixels from each *scanned* page's ink data.

    A page whose content would shrink below half its original ink is left
    untouched (guard against documents where the repeated pattern is real
    content, e.g. a repeated table).
    """
    dpi = config.analysis_dpi
    for feats in feats_list:
        if not feats.is_scanned:
            continue
        page = work[feats.index]
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
        gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width
        )
        gray = np.asarray(
            Image.fromarray(gray, mode="L").filter(
                ImageFilter.MedianFilter(3)
            ),
            dtype=np.uint8,
        )
        ink = _binarize(gray)
        h, w = ink.shape
        # pages may render at slightly different pixel sizes; pad the global
        # watermark mask to the page size
        wm = np.zeros(ink.shape, dtype=bool)
        hh, ww = min(h, wm_pix.shape[0]), min(w, wm_pix.shape[1])
        wm[:hh, :ww] = wm_pix[:hh, :ww]
        content = ink & ~wm
        # A page whose content would shrink below a third of its original
        # ink is left untouched (guard against documents where the repeated
        # pattern is real content, e.g. a repeated table).
        if content.sum() < 0.35 * ink.sum():
            continue  # suspicious: keep the page as-is
        block = feats.block
        hb, wb = h // block, w // block
        if hb < 1 or wb < 1:
            continue
        inkb = content[: hb * block, : wb * block].reshape(
            hb, block, wb, block
        ).any(axis=(1, 3))
        feats.ink = inkb
        feats.row_sums = inkb.sum(axis=1).astype(np.int32)
        feats.col_sums = inkb.sum(axis=0).astype(np.int32)
        feats.pix_rows = content.sum(axis=1).astype(np.int32)


def _top_header_band(
    feats: PageFeatures,
    zone: float,
    zone_blk: int,
    small_band: int,
) -> "tuple[int, int] | None":
    """First line-like small ink band inside the top zone.

    Prefers bands of at least 2 blocks in height (a text line) so that a
    tiny repeated ornament sitting above the real running header does not
    steal the header role."""
    active = feats.row_sums >= max(2, int(0.0015 * feats.ink.shape[1]))
    fine = _row_groups(active, 1)
    line_like = None
    first_small = None
    for g in fine:
        if g[0] > zone_blk:
            break
        h = g[1] - g[0] + 1
        if h > small_band:
            continue
        if first_small is None:
            first_small = g
        if h >= 2 and line_like is None:
            line_like = g
    return line_like or first_small


def _top_band_signature(
    feats: PageFeatures, zone: float
) -> bytes | None:
    """16x8 normalized signature of a page's running-header candidate band,
    aligned to the band itself so position and height differences do not
    matter."""
    hb, wb = feats.ink.shape
    zone_blk = max(1, int(zone * hb))
    small_band = max(6, int(0.05 * hb))
    g = _top_header_band(feats, zone, zone_blk, small_band)
    if g is None:
        return None
    r0 = max(0, g[0] - 1)
    r1 = min(hb - 1, g[1] + 1)
    region = feats.ink[r0 : r1 + 1, :]
    rows_idx = np.linspace(0, region.shape[0] - 1, 16).astype(int)
    cols_idx = np.linspace(0, region.shape[1] - 1, 8).astype(int)
    return region[np.ix_(rows_idx, cols_idx)].tobytes()


def _repeated_strip_signatures(
    feats_list: list[PageFeatures], zone: float
) -> set[int]:
    """Return the set of page indices whose top band is a *running header*.

    Two signals, either suffices:

    1. clustering: the same band signature appears on >= max(3, 1%) pages;
    2. adjacency: the signature is nearly identical (<= 25% differing bits)
       on consecutive pages -- chapter/section headers repeat on every page
       of the section, so even a 2-page section is caught.
    """
    from collections import Counter

    n = len(feats_list)
    if n == 0:
        return set()
    sigs: dict[int, bytes | None] = {}
    for feats in feats_list:
        sigs[feats.index] = _top_band_signature(feats, zone)
    repeated: set[int] = set()
    # --- 1) clustering ------------------------------------------------
    min_count = max(3, math.ceil(0.01 * n))
    counts = Counter(sigs.values())
    for idx, s in sigs.items():
        if s is not None and counts[s] >= min_count:
            repeated.add(idx)
    # --- 2) adjacency ---------------------------------------------------
    order = sorted(sigs)
    for a, b in zip(order, order[1:]):
        sa, sb = sigs[a], sigs[b]
        if sa is None or sb is None:
            continue
        differing = sum(x != y for x, y in zip(sa, sb))
        if differing <= 0.25 * len(sa):
            repeated.add(a)
            repeated.add(b)
    return repeated


def _detect_band(
    feats: PageFeatures,
    zone_sign: int,
    zone: float,
    repeat_keys: set[str],
    gap_factor: float,
    rep_top: bool = False,
) -> tuple[float | None, float | None]:
    """Detect a header (sign=-1) or footer (sign=+1) band.

    Returns ``(removed_start_pt, removed_end_pt)`` -- the vertical extent of
    the band that should be cropped away -- or ``(None, None)`` when nothing
    qualifies.

    Geometric signal (works for scans): the topmost / bottommost ink group is
    treated as a header / footer band when it is *small* and either

    * ``rep_top`` -- the page's top strip matches a strip repeated across
      many pages (cross-page running-header detection, robust against
      tight layouts where the header sits exactly one line-gap above the
      body), or
    * it is separated from the body by a gap clearly wider than the median
      line gap, or
    * it is a small, low-coverage (centered) band very close to the page
      edge (fallback for tight layouts without repetition).

    A *footer* additionally requires a separation gap wider than the median
    line gap (so the last body line on a short page is never eaten).

    Textual signal: repeated running header/footer text lines and
    page-number-like lines (born-digital PDFs).
    """
    if feats.blank:
        return None, None
    height = feats.height
    hb = feats.ink.shape[0]
    wb = feats.ink.shape[1]
    pt_per_px = feats.pt_per_px
    zone_blk = max(1, int(zone * hb))

    # --- geometric signal ------------------------------------------------
    active = feats.row_sums >= max(2, int(0.0015 * wb))
    any_ink = feats.row_sums > 0
    fine = _row_groups(active, 1)
    gaps = (
        [fine[i + 1][0] - fine[i][1] - 1 for i in range(len(fine) - 1)]
        if len(fine) > 1
        else []
    )
    line_gap = float(np.median(gaps)) if gaps else 1.0
    block_pt = feats.block * pt_per_px
    ref_gap_blk = (
        (feats._line_gap_pt or (line_gap * block_pt)) / block_pt
        if block_pt
        else line_gap
    )
    # typical *line* height: median height of line-like groups only (a giant
    # body block must not inflate it, or everything would look "small")
    line_hs = [g[1] - g[0] + 1 for g in fine if (g[1] - g[0] + 1) <= max(8, int(0.03 * hb))]
    line_h = float(np.median(line_hs)) if line_hs else 1.0
    small_band = max(4, int(0.06 * hb), int(2.5 * line_h))
    geom: float | None = None
    if fine:
        if zone_sign < 0:
            g = _top_header_band(feats, zone, zone_blk, small_band)
            if g is not None:
                sep_gap = fine[1][0] - g[1] - 1 if len(fine) > 1 else 0
                band = feats.ink[g[0] : g[1] + 1, :]
                coverage = (band.sum(axis=0) > 0).mean()
                centered = coverage <= 0.5 and g[0] <= int(0.13 * hb)
                if rep_top or sep_gap > ref_gap_blk + 0.5 or centered:
                    end = g[1]
                    grow_cap = max(2, int(0.015 * hb))
                    grown = 0
                    while (
                        end + 1 < hb and any_ink[end + 1] and grown < grow_cap
                    ):
                        end += 1
                        grown += 1
                    geom = (end + 1) * feats.block * pt_per_px
        else:
            g = fine[-1]
            # footer must be a *small, narrow* band (page number etc.),
            # not a full-width paragraph that happens to end the page
            small_footer = max(3, int(0.025 * hb), int(1.5 * line_h))
            if (
                g[1] >= hb - zone_blk - 1
                and (g[1] - g[0] + 1) <= small_footer
            ):
                band = feats.ink[g[0] : g[1] + 1, :]
                coverage = (band.sum(axis=0) > 0).mean()
                prev_gap = g[0] - fine[-2][1] - 1 if len(fine) > 1 else 0
                if prev_gap > ref_gap_blk + 0.5 and coverage <= 0.7:
                    start = g[0]
                    grow_cap = max(2, int(0.015 * hb))
                    grown = 0
                    while (
                        start - 1 >= 0 and any_ink[start - 1] and grown < grow_cap
                    ):
                        start -= 1
                        grown += 1
                    geom = start * feats.block * pt_per_px

    # --- textual signal: repeated running header / footer, or page number --
    text_band: tuple[float, float] | None = None
    if feats.lines and not feats.had_rotation:
        cands = [
            ln
            for ln in feats.lines
            if (zone_sign < 0 and ln.vfrac < zone)
            or (zone_sign > 0 and ln.vfrac > 1.0 - zone)
        ]
        if cands:
            sizes = [ln.size for ln in feats.lines if ln.size > 0]
            median_line_h = (
                float(np.median(sizes)) * 1.25 if sizes else height * 0.02
            )
            gap_min_pt = gap_factor * median_line_h
            chosen = None
            for ln in cands:
                is_repeat = ln.norm_text in repeat_keys
                if is_repeat or _is_page_number(ln):
                    if chosen is None:
                        chosen = ln
                    elif zone_sign < 0 and ln.bbox.y1 > chosen.bbox.y1:
                        chosen = ln
                    elif zone_sign > 0 and ln.bbox.y0 < chosen.bbox.y0:
                        chosen = ln
            if chosen is not None:
                if zone_sign < 0:
                    body = [ln for ln in feats.lines if ln.vfrac >= zone]
                    body_top = min((ln.bbox.y0 for ln in body), default=None)
                    if body_top is not None and body_top - chosen.bbox.y1 >= gap_min_pt:
                        text_band = (0.0, chosen.bbox.y1)
                else:
                    body = [ln for ln in feats.lines if ln.vfrac <= 1.0 - zone]
                    body_bottom = max((ln.bbox.y1 for ln in body), default=None)
                    if (
                        body_bottom is not None
                        and chosen.bbox.y0 - body_bottom >= gap_min_pt
                    ):
                        text_band = (chosen.bbox.y0, height)

    if text_band is not None:
        return text_band
    if geom is not None:
        if zone_sign < 0:
            return 0.0, geom
        return geom, height
    return None, None


def _body_bounds(
    feats: PageFeatures,
    top_end: float | None,
    bot_start: float | None,
) -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1) of visible body content in pt, from raster + text."""
    hb, wb = feats.ink.shape
    block = feats.block
    ptpp = feats.pt_per_px
    width, height = feats.width, feats.height
    thr = max(2, int(0.0015 * wb))
    active = feats.row_sums >= thr

    def first_active_from(lo: int) -> int | None:
        idx = np.argmax(active[lo:]) if active[lo:].any() else None
        return lo + idx if idx is not None else None

    def last_active_to(hi: int) -> int | None:
        idxs = np.nonzero(active[: hi + 1])[0]
        return int(idxs[-1]) if len(idxs) else None

    if top_end is not None:
        lo = max(0, int(math.ceil(top_end / ptpp / block)))
        y0_blk = first_active_from(lo)
    else:
        y0_blk = int(np.argmax(active)) if active.any() else None
    if bot_start is not None:
        hi = min(
            hb - 1,
            int(math.ceil(bot_start / ptpp / block)) - 1,
        )
        y1_blk = last_active_to(hi)
    else:
        y1_blk = hb - int(np.argmax(active[::-1])) - 1 if active.any() else None

    if y0_blk is None or y1_blk is None or y1_blk < y0_blk:
        return 0.0, 0.0, width, height

    band = feats.ink[y0_blk : y1_blk + 1, :]
    col_active = band.sum(axis=0) >= max(2, int(0.0015 * (y1_blk - y0_blk + 1)))
    if not col_active.any():
        return 0.0, 0.0, width, height
    c0 = int(np.argmax(col_active))
    c1 = wb - int(np.argmax(col_active[::-1]))
    x0 = c0 * block * ptpp
    x1 = (c1 + 1) * block * ptpp
    y0 = y0_blk * block * ptpp
    y1 = (y1_blk + 1) * block * ptpp

    # widen with text / image boxes so light or thin content is never clipped
    if not feats.had_rotation:
        for ln in feats.lines:
            cy = (ln.bbox.y0 + ln.bbox.y1) / 2.0
            if top_end is not None and cy <= top_end:
                continue
            if bot_start is not None and cy >= bot_start:
                continue
            x0 = min(x0, ln.bbox.x0)
            x1 = max(x1, ln.bbox.x1)
            y0 = min(y0, ln.bbox.y0)
            y1 = max(y1, ln.bbox.y1)
    page_area = feats.width * feats.height
    for r in feats.image_bboxes:
        if abs(r.width) * abs(r.height) > 0.6 * page_area:
            continue  # full-page scan background: raster already covers it
        cy = (r.y0 + r.y1) / 2.0
        if top_end is not None and cy <= top_end:
            continue
        if bot_start is not None and cy >= bot_start:
            continue
        x0 = min(x0, r.x0)
        x1 = max(x1, r.x1)
        y0 = min(y0, r.y0)
        y1 = max(y1, r.y1)
    return x0, y0, x1, y1


def _crop_rect(feats: PageFeatures, config: Config) -> fitz.Rect:
    """Final crop rectangle for one page."""
    if feats.blank:
        return fitz.Rect(0, 0, feats.width, feats.height)
    zone = config.header_footer_zone
    top_end: float | None = None
    bot_start: float | None = None
    if config.header_footer_zone > 0:
        _, top_end = _detect_band(
            feats, -1, zone, feats._repeat_top, config.hf_gap_factor,
            rep_top=feats._rep_top,
        )
        bot_start, _ = _detect_band(
            feats, +1, zone, feats._repeat_bottom, config.hf_gap_factor,
            rep_top=feats._rep_top,
        )
        total_trim = (top_end or 0.0) + (
            feats.height - (bot_start or feats.height)
        )
        if total_trim > config.max_trim_share * feats.height:
            top_end = None
            bot_start = None

    x0, y0, x1, y1 = _body_bounds(feats, top_end, bot_start)
    pad = config.margin_padding_pt
    crop = fitz.Rect(
        max(0.0, x0 - pad),
        max(0.0, y0 - pad),
        min(feats.width, x1 + pad),
        min(feats.height, y1 + pad),
    )
    if (
        crop.width < config.min_content_share * feats.width
        or crop.height < config.min_content_share * feats.height
    ):
        # degenerate crop (nearly empty analysis): keep the page as-is
        crop = fitz.Rect(0, 0, feats.width, feats.height)
        top_end = None
        bot_start = None
    feats._band_top_end = top_end
    feats._band_bot_start = bot_start
    return crop


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------


def analyze_document(
    work: fitz.Document, rotations: list[int], config: Config
) -> tuple[list[PagePlan], list[PageFeatures]]:
    """Two-pass analysis.  Pass 1 extracts features, pass 2 (with global
    "running header" knowledge) computes crop rects and split segments."""
    feats_list: list[PageFeatures] = []
    for pno in range(len(work)):
        had_rot = rotations[pno] != 0 if pno < len(rotations) else False
        feats_list.append(_page_features(work[pno], pno, had_rot, config))

    # watermark / repeated-pattern removal (scanned books with a light
    # diagonal watermark that confuses header/footer detection)
    if config.remove_watermark:
        wm_pix = _compute_watermark_mask(work, feats_list, config)
        if wm_pix is not None:
            _apply_watermark_removal(work, feats_list, wm_pix, config)

    # global repeated-running-header detection
    n = len(feats_list)
    min_pages = max(2, math.ceil(config.repeat_min_share * n)) if n else 0
    zone = config.header_footer_zone
    counters: dict[int, Counter] = {-1: Counter(), +1: Counter()}
    for feats in feats_list:
        for ln in feats.lines:
            if ln.vfrac < zone and not _is_page_number(ln):
                counters[-1][ln.norm_text] += 1
            elif ln.vfrac > 1.0 - zone and not _is_page_number(ln):
                counters[+1][ln.norm_text] += 1

    # cross-page top-strip repetition for scanned pages (running headers in
    # scans have no text layer; the strip signature clusters identical pages)
    rep_sigs = _repeated_strip_signatures(feats_list, zone)

    # document-level reference line gap (pt): the header/footer separation
    # gap must be compared against a typical *line* gap, not against the
    # single gap of a page that happens to contain only two ink groups
    line_gaps_pt = []
    for feats in feats_list:
        hb, wb = feats.ink.shape
        active = feats.row_sums >= max(2, int(0.0015 * wb))
        fine = _row_groups(active, 1)
        if len(fine) > 1:
            gaps = [
                fine[i + 1][0] - fine[i][1] - 1
                for i in range(len(fine) - 1)
            ]
            line_gaps_pt.append(
                float(np.median(gaps)) * feats.block * feats.pt_per_px
            )
    global_line_gap = (
        float(np.median(line_gaps_pt)) if line_gaps_pt else None
    )

    for feats in feats_list:
        feats._repeat_top = {k for k, c in counters[-1].items() if c >= min_pages}
        feats._repeat_bottom = {k for k, c in counters[+1].items() if c >= min_pages}
        feats._rep_top = feats.index in rep_sigs
        feats._line_gap_pt = global_line_gap
        feats._band_top_end = None
        feats._band_bot_start = None

    plans: list[PagePlan] = []
    for feats in feats_list:
        crop = _crop_rect(feats, config)
        segments = _make_segments(crop, config, feats)
        plans.append(
            PagePlan(
                index=feats.index,
                width=feats.width,
                height=feats.height,
                is_scanned=feats.is_scanned,
                crop=crop,
                segments=segments,
                removed_top=feats._band_top_end,
                removed_bottom=feats._band_bot_start,
            )
        )
    return plans, feats_list


def _make_segments(
    crop: fitz.Rect, config: Config, feats: PageFeatures | None = None
) -> list[Segment]:
    def seg(clip: fitz.Rect) -> Segment:
        return Segment(
            clip=clip, width=abs(clip.width), height=abs(clip.height)
        )

    if not config.split:
        return [seg(crop)]

    overlap_mode = (
        config.split_overlap_ratio is not None
        or config.split_overlap_min_pt is not None
    )
    if overlap_mode:
        h = abs(crop.y1 - crop.y0)
        if h <= 2 * (config.split_overlap_min_pt or 0) + 2:
            return [seg(crop)]
        overlap = max(
            (config.split_overlap_ratio or 0.0) * h,
            config.split_overlap_min_pt or 0.0,
        )
        mid = (crop.y0 + crop.y1) / 2.0
        top = fitz.Rect(crop.x0, crop.y0, crop.x1, mid + overlap)
        bottom = fitz.Rect(crop.x0, mid - overlap, crop.x1, crop.y1)
        return [seg(top), seg(bottom)]

    # --- adaptive: cut in the whitespace between two lines ----------------
    split_y = _adaptive_split_y(feats, crop) if feats is not None else (
        (crop.y0 + crop.y1) / 2.0
    )
    split_y = max(crop.y0 + 1e-3, min(crop.y1 - 1e-3, split_y))
    top = fitz.Rect(crop.x0, crop.y0, crop.x1, split_y)
    bottom = fitz.Rect(crop.x0, split_y, crop.x1, crop.y1)
    return [seg(top), seg(bottom)]


def _adaptive_split_y(feats: PageFeatures, crop: fitz.Rect) -> float:
    """Choose the best horizontal cut position inside `crop`.

    The goal is to place the cut in the *blank space between two lines* (two
    text baselines), so no glyph row is cut and the two halves need no
    overlap.  Candidates are (in priority order):

    1. text-based: the gap between two consecutive text line boxes
       (exact for born-digital PDFs);
    2. raster-based: runs of ink-free rows in the *per-pixel* row projection,
       with a small pure-whitespace clearance around the cut (tightly typeset
       scans have only a few pixels of inter-line whitespace, invisible at
       block resolution).

    Candidates whose *neighbouring ink regions are both solid blocks* are
    treated as gaps *inside* a figure / large formula and are only used as a
    last resort (so images are not cut when a text gap exists anywhere).
    The candidate closest to the crop's vertical middle wins; wider gaps are
    preferred on ties.  If nothing qualifies, the vertical middle is used.
    """
    mid = (crop.y0 + crop.y1) / 2.0
    # candidate tuple: (inside_figure, distance_to_mid, -width, y)
    candidates: list[tuple[int, float, float, float]] = []

    # --- 1) text line gaps (text pages, unrotated) -------------------------
    if feats.lines and not feats.had_rotation:
        lines = sorted(
            (
                ln
                for ln in feats.lines
                if crop.y0 - 2 <= (ln.bbox.y0 + ln.bbox.y1) / 2 <= crop.y1 + 2
            ),
            key=lambda ln: ln.bbox.y0,
        )
        sizes = [ln.size for ln in feats.lines if ln.size > 0]
        median_line_h = (
            float(np.median(sizes)) * 1.25 if sizes else feats.height * 0.02
        )
        min_gap = 0.4 * median_line_h
        for a, b in zip(lines, lines[1:]):
            gap_top, gap_bot = a.bbox.y1, b.bbox.y0
            w = gap_bot - gap_top
            if w >= min_gap:
                y = (gap_top + gap_bot) / 2.0
                candidates.append((0, abs(y - mid), -w, y))

    # --- 2) raster blank bands (scans, rotated pages, any page) ------------
    # Per-PIXEL row projection: inter-line whitespace in tightly typeset
    # scans is only a few pixels tall; block-level (8 px) rows are polluted
    # by the neighbouring lines and cannot resolve it.
    pix = feats.pix_rows
    ptpp = feats.pt_per_px
    W_px = feats.ink.shape[1] * feats.block
    pix_thr = max(1, int(0.0002 * W_px))
    blank = pix <= pix_thr
    y0_px = max(0, int(math.floor(crop.y0 / ptpp)))
    y1_px = min(len(pix) - 1, int(math.ceil(crop.y1 / ptpp)))
    if y1_px > y0_px:
        band = blank[y0_px : y1_px + 1]

        # typical gap width = median blank-run width between active groups
        groups = _row_groups(pix[y0_px : y1_px + 1] > pix_thr, 1)
        gaps = [
            groups[i + 1][0] - groups[i][1] - 1
            for i in range(len(groups) - 1)
        ]
        median_gap = float(np.median(gaps)) if gaps else 4.0
        min_gap_px = max(2, int(round(0.5 * median_gap)))

        # solid-block detector (figure / big formula / table block)
        def _solid(start_px: int, end_px: int) -> bool:
            if end_px < start_px:
                return True
            rows = pix[start_px : end_px + 1]
            return bool((rows > pix_thr).mean() > 0.85) and (
                end_px - start_px
            ) >= 6 * feats.block

        run_start = None
        band_len = y1_px - y0_px + 1
        for i, a in enumerate(band):
            if a:
                if run_start is None:
                    run_start = i
            elif run_start is not None:
                _emit_run(feats, mid, y0_px, run_start, i - 1,
                          min_gap_px, pix_thr, band_len, _solid, candidates)
                run_start = None
        if run_start is not None:
            _emit_run(feats, mid, y0_px, run_start, band_len - 1,
                      min_gap_px, pix_thr, band_len, _solid, candidates)

    # pick the best candidate that keeps both halves reasonable
    min_share = 0.12 * (crop.y1 - crop.y0)
    best = None
    for cand in sorted(candidates):
        y = cand[3]
        if (y - crop.y0) >= min_share and (crop.y1 - y) >= min_share:
            best = y
            break
    return best if best is not None else mid


def _emit_run(
    feats: PageFeatures,
    mid: float,
    y0: int,
    run_start: int,
    run_end: int,
    min_gap: int,
    thr: int,
    band_len: int,
    solid,
    candidates: list,
) -> None:
    """Record one ink-free row run as a split candidate."""
    if run_start == 0 or run_end >= band_len - 1:
        # margin whitespace touching the crop edge: not a line gap
        return
    w = run_end - run_start + 1
    if w < min_gap:
        return
    start_abs, end_abs = y0 + run_start, y0 + run_end
    center = int((start_abs + end_abs) / 2)
    # the cut row and its immediate neighbours must be ink-free, so the cut
    # line passes through pure whitespace with a small clearance
    lo, hi = max(0, center - 1), min(len(feats.pix_rows) - 1, center + 1)
    if (feats.pix_rows[lo : hi + 1] > thr).any():
        return
    # figure/formula guard: a gap inside a solid ink block (both sides solid)
    above = _run_bounds_above(feats, start_abs - 1, thr)
    below = _run_bounds_below(feats, end_abs + 1, thr)
    inside_figure = int(solid(*above) and solid(*below))
    y = (center + 0.5) * feats.pt_per_px
    candidates.append((inside_figure, abs(y - mid), -w * feats.pt_per_px, y))


def _run_bounds_above(feats: PageFeatures, start: int, thr: int) -> tuple[int, int]:
    """Contiguous ink-run extent ending at row `start` (inclusive)."""
    if start < 0:
        return 0, 0
    end = start
    while end >= 0 and feats.pix_rows[end] > thr:
        end -= 1
    return end + 1, start


def _run_bounds_below(feats: PageFeatures, start: int, thr: int) -> tuple[int, int]:
    """Contiguous ink-run extent starting at row `start` (inclusive)."""
    h = len(feats.pix_rows)
    if start >= h:
        return h - 1, h - 1
    beg = start
    while beg < h and feats.pix_rows[beg] > thr:
        beg += 1
    return start, beg - 1


# --------------------------------------------------------------------------
# debug output
# --------------------------------------------------------------------------


def write_debug_overlays(
    work: fitz.Document,
    feats_list: list[PageFeatures],
    plans: list[PagePlan],
    out_dir: str | Path,
    config: Config,
) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for feats, plan in zip(feats_list, plans):
        page = work[feats.index]
        pix = page.get_pixmap(dpi=config.analysis_dpi, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        draw = ImageDraw.Draw(img)
        s = 1.0 / feats.pt_per_px  # pt -> analysis pixel

        def px(r: fitz.Rect) -> tuple[int, int, int, int]:
            return (
                int(r.x0 * s),
                int(r.y0 * s),
                int(r.x1 * s),
                int(r.y1 * s),
            )

        if plan.removed_top is not None:
            draw.rectangle(
                px(fitz.Rect(0, 0, feats.width, plan.removed_top)),
                outline=(255, 0, 0),
                width=2,
            )
        if plan.removed_bottom is not None:
            draw.rectangle(
                px(fitz.Rect(0, plan.removed_bottom, feats.width, feats.height)),
                outline=(255, 0, 0),
                width=2,
            )
        draw.rectangle(px(plan.crop), outline=(0, 200, 0), width=3)
        for i, seg in enumerate(plan.segments):
            draw.rectangle(px(seg.clip), outline=(0, 120, 255), width=2)
            draw.text((px(seg.clip)[0] + 4, px(seg.clip)[1] + 4), f"seg {i}")
        img.save(out_dir / f"page-{feats.index:04d}.png")
