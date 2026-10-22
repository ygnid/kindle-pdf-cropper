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

    t = _threshold(gray)
    ink = gray < t
    if ink.mean() > 0.6:  # dark-background page (negative scan): flip
        ink = ~ink

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


def _detect_band(
    feats: PageFeatures,
    zone_sign: int,
    zone: float,
    repeat_keys: set[str],
    gap_factor: float,
) -> tuple[float | None, float | None]:
    """Detect a header (sign=-1) or footer (sign=+1) band.

    Returns ``(removed_start_pt, removed_end_pt)`` -- the vertical extent of
    the band that should be cropped away -- or ``(None, None)`` when nothing
    qualifies.
    """
    if feats.blank:
        return None, None
    height = feats.height
    hb = feats.ink.shape[0]
    wb = feats.ink.shape[1]
    pt_per_px = feats.pt_per_px
    zone_blk = max(1, int(zone * hb))

    # --- geometric signal: isolated small band separated by a clear gap ----
    active = feats.row_sums >= max(2, int(0.0015 * wb))
    any_ink = feats.row_sums > 0
    merge = max(1, int(0.012 * hb))
    groups = _row_groups(active, merge)
    geom: float | None = None
    if groups:
        if zone_sign < 0:
            g = groups[0]
            if len(groups) > 1:
                body_gaps = [
                    groups[i + 1][0] - groups[i][1] - 1
                    for i in range(1, len(groups) - 1)
                ]
                gap = groups[1][0] - g[1] - 1
                median_body = (
                    float(np.median(body_gaps)) if body_gaps else 0.0
                )
                gap_min = max(merge * 2, int(0.012 * hb))
                if (
                    g[0] <= zone_blk
                    and (g[1] - g[0] + 1) <= int(0.07 * hb)
                    and gap > max(gap_min, median_body + 1)
                ):
                    # grow the band over faint glyph edges (thin ascenders
                    # may not pass the row-density threshold)
                    end = g[1]
                    grow_cap = max(2, int(0.015 * hb))
                    grown = 0
                    while (
                        end + 1 < hb
                        and any_ink[end + 1]
                        and grown < grow_cap
                    ):
                        end += 1
                        grown += 1
                    geom = (end + 1) * feats.block * pt_per_px
        else:
            g = groups[-1]
            if len(groups) > 1:
                body_gaps = [
                    groups[i + 1][0] - groups[i][1] - 1
                    for i in range(0, len(groups) - 2)
                ]
                gap = g[0] - groups[-2][1] - 1
                median_body = (
                    float(np.median(body_gaps)) if body_gaps else 0.0
                )
                gap_min = max(merge * 2, int(0.012 * hb))
                if (
                    g[1] >= hb - zone_blk - 1
                    and (g[1] - g[0] + 1) <= int(0.07 * hb)
                    and gap > max(gap_min, median_body + 1)
                ):
                    start = g[0]
                    grow_cap = max(2, int(0.015 * hb))
                    grown = 0
                    while (
                        start - 1 >= 0
                        and any_ink[start - 1]
                        and grown < grow_cap
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
            feats, -1, zone, feats._repeat_top, config.hf_gap_factor
        )
        bot_start, _ = _detect_band(
            feats, +1, zone, feats._repeat_bottom, config.hf_gap_factor
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
    for feats in feats_list:
        feats._repeat_top = {k for k, c in counters[-1].items() if c >= min_pages}
        feats._repeat_bottom = {k for k, c in counters[+1].items() if c >= min_pages}
        feats._band_top_end = None
        feats._band_bot_start = None

    plans: list[PagePlan] = []
    for feats in feats_list:
        crop = _crop_rect(feats, config)
        segments = _make_segments(crop, config)
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


def _make_segments(crop: fitz.Rect, config: Config) -> list[Segment]:
    def seg(clip: fitz.Rect) -> Segment:
        return Segment(
            clip=clip, width=abs(clip.width), height=abs(clip.height)
        )

    if not config.split:
        return [seg(crop)]

    h = abs(crop.y1 - crop.y0)
    if h <= 2 * config.split_overlap_min_pt + 2:
        return [seg(crop)]
    overlap = max(config.split_overlap_ratio * h, config.split_overlap_min_pt)
    mid = (crop.y0 + crop.y1) / 2.0
    top = fitz.Rect(crop.x0, crop.y0, crop.x1, mid + overlap)
    bottom = fitz.Rect(crop.x0, mid - overlap, crop.x1, crop.y1)
    return [seg(top), seg(bottom)]


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
