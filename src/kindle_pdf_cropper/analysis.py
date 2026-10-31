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
    """Robust *dark-ink* binarization threshold.

    Scanned pages frequently contain a large, fairly uniform light area: a
    gray watermark, a scanner shadow, or the paper itself when the scan is
    not pure white.  A plain two-mode midpoint (or Otsu) happily classifies
    that area as ink, which both drowns the real text and makes the ink
    projections useless.

    Instead the threshold is anchored to the darkest printed strokes: take a
    low percentile of the pixels that are clearly darker than the background
    mode, then place the cut a fixed fraction of the way from that dark core
    up towards the background.  Light watermarks sit far above the resulting
    threshold and are ignored, while anti-aliased glyph edges (which always
    have a dark core) are kept.
    """
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 128
    bg = int(np.argmax(hist))
    # pixels that are noticeably darker than the dominant (paper) mode
    darker = gray[gray < bg - 6]
    if darker.size < max(64, 0.002 * gray.size):
        # no real dark content (blank / faint-only page): keep a mid default
        return max(30, bg - 8)
    dark = float(np.percentile(darker, 2))
    t = bg - 0.40 * (bg - dark)
    # never creep too close to the background, or paper noise turns to ink
    t = min(t, bg - max(8.0, 0.08 * (bg - dark)))
    return int(max(25, min(235, t)))


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
    # drop thin scan borders / shadows touching the paper edge; they are not
    # content and must not become the "bottommost band" (which would hide a
    # page number sitting above them)
    inkb = _remove_border_blobs(inkb)

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
    # A full-page dark/full-bleed image (cover, photo plate) has no paper
    # margins to find; cropping it to its dark ink would destroy the artwork.
    photo_page = image_area >= 0.8 * page_area and float(np.median(gray)) < 160

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
        photo_page=photo_page,
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
    """Build a pixel-level watermark / repeated-furniture mask.

    A watermark, running header, page frame or scanner artifact occupies the
    *same* pixel on (almost) every page, so that pixel is darker than the
    page paper on a large majority of the sampled pages.  Real body text sits
    at a different place on each page (and even when the line grid repeats,
    individual glyph strokes do not), so its darkness is not consistent.

    Earlier versions used the cross-page *median* gray, which fails on dense
    books: when text lines align across pages, more than half the sample can
    be dark at a stroke pixel and the median wrongly reports a watermark,
    erasing real text from the analysis.  Using the *fraction of pages that
    are dark* at each pixel fixes this: only genuinely static marks reach a
    high fraction, text stays scattered.

    Returns a boolean mask (True = watermark / repeated mark) or None when
    the document is too small or has no obvious repeated pattern.
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

    dark = np.zeros((hmax, wmax), dtype=np.uint32)
    valid = np.zeros((hmax, wmax), dtype=np.uint32)
    gsum = np.zeros((hmax, wmax), dtype=np.float64)
    for g in grays:
        h, w = g.shape
        pbg = float(np.median(g))  # this page's paper level
        d = g < (pbg - 8.0)
        dark[:h, :w] += d
        gsum[:h, :w] += g
        valid[:h, :w] += 1

    enough = valid >= max(3, int(0.5 * len(grays)))
    frac = dark / np.maximum(valid, 1)
    med = np.where(valid > 0, gsum / np.maximum(valid, 1), 255.0)
    bg = float(np.median(med[valid > 0])) if np.any(valid > 0) else 255.0
    mask = (frac >= 0.85) & (med < (bg - 8.0)) & enough
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
    """Remove repeated watermark pixels from the per-pixel split profile.

    The *block* ink mask is deliberately left untouched.  It is already
    thresholded to strong printed strokes, so a light watermark is absent
    anyway; subtracting the repeated-pattern mask there would also erase the
    running header / footer text (which is repeated by definition) before the
    band detector gets a chance to see it.  The cleaned pixel row profile is
    still useful for the adaptive split: watermark strokes must not be
    mistaken for a line of text.

    A page whose cleaned content would shrink below a third of its original
    ink is left untouched (guard against documents where the repeated
    pattern is real content, e.g. a repeated table).
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
        if content.sum() < 0.35 * ink.sum():
            continue  # suspicious: keep the page's original profile
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
        if g[0] <= 1:
            # a band touching the paper edge is a scan border / shadow, not
            # the running header; the real header sits further in
            continue
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


def _repeated_footer_bands(
    feats_list: list[PageFeatures], zone: float
) -> set[int]:
    """Return page indices whose bottom band is repeated page furniture.

    Page numbers plus their tiny rule sit at almost the same height on page
    after page, while body last-line positions vary with the amount of text.
    A footer band is also much *narrower* than a body line (a short number /
    rule rather than a full text measure).  Clustering the bottommost band by
    normalised position, height and coverage catches footers even when the
    gap above them is only one normal line gap -- the situation where a pure
    "bigger than a line gap" test fails.
    """
    from collections import Counter

    n = len(feats_list)
    if n == 0:
        return set()
    keys: dict[int, tuple[int, int, bool]] = {}
    counts: Counter = Counter()
    for feats in feats_list:
        if feats.blank or feats.photo_page or not feats.ink.any():
            continue
        hb, wb = feats.ink.shape
        active = feats.row_sums >= max(2, int(0.0015 * wb))
        groups = _row_groups(active, 1)
        if not groups:
            continue
        g_top, g_bot = groups[-1]
        zone_blk = max(1, int(zone * hb))
        if g_bot < hb - zone_blk - 1:
            continue
        h = g_bot - g_top + 1
        if h > max(8, int(0.08 * hb)):
            continue
        band = feats.ink[g_top : g_bot + 1, :]
        coverage = float((band.sum(axis=0) > 0).mean())
        # a body line normally covers most of the text measure; footer
        # furniture (page number / short rule) is much narrower
        if coverage > 0.55:
            continue
        vfrac = ((g_top + g_bot) / 2.0) / hb
        key = (round(vfrac * 40), round(h * 100.0 / hb), coverage > 0.35)
        keys[feats.index] = key
        counts[key] += 1
    min_count = max(3, math.ceil(0.03 * n))
    return {i for i, k in keys.items() if counts[k] >= min_count}


def _detect_band(
    feats: PageFeatures,
    zone_sign: int,
    zone: float,
    repeat_keys: set[str],
    gap_factor: float,
    rep_top: bool = False,
    rep_bottom: bool = False,
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
    if feats.blank or feats.photo_page:
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
            small_footer = max(3, int(0.04 * hb), int(2.0 * line_h))
            if (
                g[1] >= hb - zone_blk - 1
                and (g[1] - g[0] + 1) <= small_footer
            ):
                band = feats.ink[g[0] : g[1] + 1, :]
                coverage = (band.sum(axis=0) > 0).mean()
                prev_gap = g[0] - fine[-2][1] - 1 if len(fine) > 1 else 0
                # A repeated narrow bottom band is page furniture even when
                # the gap above it is only one normal line gap (common when
                # the page number sits directly under the last text line).
                repeated_furniture = (
                    rep_bottom and coverage <= 0.55 and prev_gap >= 1
                )
                if (
                    prev_gap > ref_gap_blk + 0.5 or repeated_furniture
                ) and coverage <= 0.7:
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


def _binary_open(mask: np.ndarray, radius: int = 1) -> np.ndarray:
    """Morphological opening (erode then dilate) on a boolean grid.

    Implemented with numpy shifts so analysis keeps its numpy/Pillow-only
    dependencies.  Used to delete tiny isolated specks -- dust, scanner
    edge marks, watermark crumbs -- before measuring the content bounding
    box.  Full text lines and figures (wider/taller than ``2*radius``)
    survive untouched.
    """
    if radius <= 0 or not mask.any():
        return mask

    def shift(m: np.ndarray, dy: int, dx: int) -> np.ndarray:
        h, w = m.shape
        out = np.zeros_like(m)
        ys0, ys1 = max(0, dy), min(h, h + dy)
        xs0, xs1 = max(0, dx), min(w, w + dx)
        yd0, yd1 = max(0, -dy), min(h, h - dy)
        xd0, xd1 = max(0, -dx), min(w, w - dx)
        out[ys0:ys1, xs0:xs1] = m[yd0:yd1, xd0:xd1]
        return out

    eroded = mask
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            eroded = eroded & shift(mask, dy, dx)
    grown = eroded
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            grown = grown | shift(eroded, dy, dx)
    return grown


def _remove_border_blobs(
    mask: np.ndarray,
    max_area_share: float = 0.12,
    max_thickness_share: float = 0.12,
) -> np.ndarray:
    """Drop thin blobs that touch the page boundary (scan borders / shadows).

    Scanned books often carry a black strip along one or more paper edges.
    The strip is connected to the page boundary and is thin, whereas real
    content either does not touch the boundary or is a large full-bleed
    image.  Any border-touching component that is both thin and small
    relative to the total ink is therefore treated as an artifact.

    A dark cover / full-page image forms one big component made of nearly
    all the ink, so ``area <= max_area_share * total`` protects it.
    """
    h, w = mask.shape
    total = int(mask.sum())
    if total == 0:
        return mask
    seen = np.zeros((h, w), dtype=bool)
    out = mask.copy()
    max_thick_h = max(2, int(round(max_thickness_share * h)))
    max_thick_w = max(2, int(round(max_thickness_share * w)))
    ys, xs = np.nonzero(mask)
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if seen[sy, sx]:
            continue
        stack = [(sy, sx)]
        seen[sy, sx] = True
        cells: list[tuple[int, int]] = []
        while stack:
            y, x = stack.pop()
            cells.append((y, x))
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        ys_c = [c[0] for c in cells]
        xs_c = [c[1] for c in cells]
        y0, y1 = min(ys_c), max(ys_c)
        x0, x1 = min(xs_c), max(xs_c)
        touches_border = x0 == 0 or y0 == 0 or x1 == w - 1 or y1 == h - 1
        if not touches_border:
            continue
        thin = (x1 - x0 + 1) <= max_thick_w or (y1 - y0 + 1) <= max_thick_h
        if thin and len(cells) <= max_area_share * total:
            for y, x in cells:
                out[y, x] = False
    return out


def _mass_span(
    mass: np.ndarray, total: int, trim: int
) -> tuple[int | None, int | None]:
    """Smallest [start, end] index range that contains ``total-2*trim`` of the
    mass, with up to ``trim`` units discarded from each end.

    Used to make the content box immune to small isolated marks: a few
    hundred ink pixels at the page edge are far below ``trim`` and never
    move the bound, while real body content is orders of magnitude heavier.
    """
    if total <= 0 or mass.size == 0:
        return None, None
    cum = np.cumsum(mass)
    a = int(np.searchsorted(cum, trim, side="right"))
    b = int(np.searchsorted(cum, total - trim, side="right")) - 1
    if a > b:
        peak = int(np.argmax(mass))
        return peak, peak
    a = max(0, min(a, mass.size - 1))
    b = max(0, min(b, mass.size - 1))
    return a, b


def _body_bounds(
    feats: PageFeatures,
    top_end: float | None,
    bot_start: float | None,
) -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1) of visible body content in pt, from raster + text.

    The raster box is measured on a lightly opened mask and with a small
    mass trim, so an isolated speck / watermark crumb / edge scratch cannot
    stretch the crop to the page edge (a very common failure on scans whose
    paper edge carries a few dark pixels).
    """
    hb = feats.ink.shape[0]
    block = feats.block
    ptpp = feats.pt_per_px
    width, height = feats.width, feats.height

    lo = 0
    hi = hb - 1
    if top_end is not None:
        lo = min(hb - 1, max(0, int(math.ceil(top_end / ptpp / block))))
    if bot_start is not None:
        hi = max(0, min(hb - 1, int(math.ceil(bot_start / ptpp / block)) - 1))
    if hi < lo:
        return 0.0, 0.0, width, height

    sub = _binary_open(feats.ink[lo : hi + 1, :], 1)
    sub = _remove_border_blobs(sub)
    if not sub.any():
        return 0.0, 0.0, width, height

    total = int(sub.sum())
    # tiny trim only: the opening and border-blob filter already remove the
    # usual specks, so this must never eat a real (even thin) text column
    trim = max(1, min(int(round(0.001 * total)), 2))
    r0, r1 = _mass_span(sub.sum(axis=1).astype(np.int64), total, trim)
    c0, c1 = _mass_span(sub.sum(axis=0).astype(np.int64), total, trim)
    if r0 is None or c0 is None:
        return 0.0, 0.0, width, height

    x0 = c0 * block * ptpp
    x1 = (c1 + 1) * block * ptpp
    y0 = (lo + r0) * block * ptpp
    y1 = (lo + r1 + 1) * block * ptpp

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
    if feats.blank or feats.photo_page:
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
            rep_top=feats._rep_top, rep_bottom=feats._rep_bottom,
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
    # `min_content_share` guards against a bogus near-empty crop.  But a page
    # that genuinely holds only a few lines (section titles, notes pages)
    # would be wrongly reset to the full page, leaving all its margins
    # uncropped.  Only fall back when the analysis mask itself is nearly
    # empty; a real sparse page keeps its (small) correct crop.
    ink_share = (
        feats.ink.sum() / feats.ink.size if feats.ink.size else 0.0
    )
    sparse = ink_share < 0.005
    if sparse or (
        crop.width < config.min_content_share * feats.width
        and crop.height < config.min_content_share * feats.height
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
    # cross-page bottom-band repetition (page number + rule sitting directly
    # under the last body line, too close for a gap-only test)
    rep_footers = _repeated_footer_bands(feats_list, zone)

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
        feats._rep_bottom = feats.index in rep_footers
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
