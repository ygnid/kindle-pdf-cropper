"""Tests for the horizontal (landscape) page split with overlap."""

from __future__ import annotations

import pymupdf as fitz

from kindle_pdf_cropper.analysis import analyze_document, normalize_work_doc
from kindle_pdf_cropper.config import Config
from kindle_pdf_cropper.output_pdf import write_pdf

OVERLAP_PT = 20.0
OVERLAP_RATIO = 0.03


def split_analyze(path: str, **cfg):
    src = fitz.open(path)
    try:
        work, rotations = normalize_work_doc(src)
        try:
            c = Config(
                split=True,
                split_overlap_min_pt=cfg.get("min_pt", OVERLAP_PT),
                split_overlap_ratio=cfg.get("ratio", OVERLAP_RATIO),
            ).derived()
            return analyze_document(work, rotations, c)
        finally:
            work.close()
    finally:
        src.close()


def adaptive_split_analyze(path: str, **cfg):
    """Split with no overlap parameters -> adaptive line-gap cut."""
    src = fitz.open(path)
    try:
        work, rotations = normalize_work_doc(src)
        try:
            c = Config(split=True, **cfg).derived()
            return analyze_document(work, rotations, c)
        finally:
            work.close()
    finally:
        src.close()


def test_split_doubles_pages_and_is_landscape(text_pdf, tmp_path):
    plans, feats = split_analyze(str(text_pdf))
    assert len(plans) == 5
    assert all(len(p.segments) == 2 for p in plans)
    for p in plans:
        for seg in p.segments:
            assert seg.width > seg.height, seg  # landscape orientation


def test_split_overlap_keeps_middle_line(text_pdf, tmp_path):
    plans, feats = split_analyze(str(text_pdf))
    out = tmp_path / "split.pdf"
    src = fitz.open(str(text_pdf))
    try:
        work, rotations = normalize_work_doc(src)
        try:
            write_pdf(work, plans, str(out))
        finally:
            work.close()
    finally:
        src.close()

    doc = fitz.open(str(out))
    try:
        assert len(doc) == 10
        # the line whose centre falls inside the overlap band must be present
        # in BOTH segments of its source page
        for p, f in zip(plans, feats):
            crop = p.crop
            overlap = max(OVERLAP_RATIO * crop.height, OVERLAP_PT)
            mid = (crop.y0 + crop.y1) / 2.0
            in_overlap = [
                ln
                for ln in f.lines
                if mid - overlap <= (ln.bbox.y0 + ln.bbox.y1) / 2 <= mid + overlap
            ]
            if not in_overlap:
                continue  # sparse sample: nothing straddles the cut
            seg0 = doc[2 * p.index].get_text()
            seg1 = doc[2 * p.index + 1].get_text()
            for ln in in_overlap:
                key = " ".join(ln.text.split())[:30]
                assert key in seg0, (key, seg0)
                assert key in seg1, (key, seg1)
    finally:
        doc.close()


def test_split_overlap_amount(text_pdf):
    plans, _ = split_analyze(str(text_pdf))
    for p in plans:
        crop = p.crop
        expected = max(OVERLAP_RATIO * crop.height, OVERLAP_PT)
        mid = (crop.y0 + crop.y1) / 2.0
        top, bottom = p.segments
        assert abs(top.clip.y1 - mid - expected) < 1e-6, top.clip
        assert abs(mid - bottom.clip.y0 - expected) < 1e-6, bottom.clip
        # the two halves together cover the whole crop
        assert top.clip.y0 == crop.y0 and bottom.clip.y1 == crop.y1
        assert bottom.clip.y0 < top.clip.y1  # real overlap exists


def test_adaptive_split_no_overlap_and_between_lines(text_pdf):
    plans, feats = adaptive_split_analyze(str(text_pdf))
    assert all(len(p.segments) == 2 for p in plans)
    for p, f in zip(plans, feats):
        top, bottom = p.segments
        # no overlap: halves are adjacent and cover the crop exactly
        assert abs(top.clip.y1 - bottom.clip.y0) < 1e-6
        assert top.clip.y0 == p.crop.y0 and bottom.clip.y1 == p.crop.y1
        split_y = top.clip.y1
        # the cut must not pass through any text line
        for ln in f.lines:
            if p.crop.y0 <= (ln.bbox.y0 + ln.bbox.y1) / 2 <= p.crop.y1:
                assert not (ln.bbox.y0 < split_y < ln.bbox.y1), (
                    split_y, ln.bbox
                )


def test_adaptive_split_scan_in_blank_band(scan_pdf):
    plans, feats = adaptive_split_analyze(str(scan_pdf))
    for p, f in zip(plans, feats):
        top, bottom = p.segments
        split_y = top.clip.y1
        assert abs(top.clip.y1 - bottom.clip.y0) < 1e-6
        # the cut falls on an ink-free band of the scanned page (pixel level)
        c = int(split_y / f.pt_per_px)
        lo, hi = max(0, c - 2), min(len(f.pix_rows) - 1, c + 2)
        assert not (f.pix_rows[lo : hi + 1] > 0).any(), (split_y, c)


def test_adaptive_split_tight_scan_clean_cut(tight_scan_pdf):
    """Regression: on a dense scan (few-pixel line gaps) the cut must land in
    pixel-level whitespace, never through a text line."""
    plans, feats = adaptive_split_analyze(str(tight_scan_pdf))
    for p, f in zip(plans, feats):
        top, bottom = p.segments
        split_y = top.clip.y1
        assert abs(top.clip.y1 - bottom.clip.y0) < 1e-6
        c = int(split_y / f.pt_per_px)
        lo, hi = max(0, c - 2), min(len(f.pix_rows) - 1, c + 2)
        assert not (f.pix_rows[lo : hi + 1] > 0).any(), (split_y, c)


def test_adaptive_split_rotated_scan(scan_rot_pdf):
    plans, feats = adaptive_split_analyze(str(scan_rot_pdf))
    for p in plans:
        top, bottom = p.segments
        assert abs(top.clip.y1 - bottom.clip.y0) < 1e-6  # no overlap
        # neither half may degenerate into a sliver (code guarantee: 12%)
        h = p.crop.height
        assert top.clip.height >= 0.12 * h
        assert bottom.clip.height >= 0.12 * h
