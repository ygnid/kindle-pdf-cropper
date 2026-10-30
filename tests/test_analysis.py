"""Tests for the page-content analysis (margins, header/footer)."""

from __future__ import annotations

import pymupdf as fitz

from kindle_pdf_cropper.analysis import analyze_document, normalize_work_doc
from kindle_pdf_cropper.config import Config


def analyze(path: str, **cfg_overrides):
    src = fitz.open(path)
    try:
        work, rotations = normalize_work_doc(src)
        try:
            cfg = Config(**cfg_overrides).derived()
            return analyze_document(work, rotations, cfg)
        finally:
            work.close()
    finally:
        src.close()


def test_text_page_trims_all_four_margins(text_pdf):
    plans, feats = analyze(str(text_pdf))
    assert len(plans) == 5
    for plan in plans:
        c = plan.crop
        assert c.width < plan.width  # left+right margins trimmed
        assert c.height < plan.height  # top+bottom trimmed
        # margins are 80pt; body starts ~80pt; crop box stays inside it
        assert 55 <= c.x0 <= 85, c
        assert c.x1 <= plan.width - 60, c  # right margin gone
        # header (y~60) removed; body starts ~137pt
        assert c.y0 >= 100, c
        # footer page number (y~782) removed; body ends ~326pt
        assert c.y1 <= 450, c
        # header/footer removal happened on every page
        assert plan.removed_top is not None and plan.removed_bottom is not None


def test_text_header_footer_actually_removed(text_pdf):
    plans, _ = analyze(str(text_pdf))
    # crop top must sit below the header line and above the body
    for plan in plans:
        assert plan.removed_top is not None
        assert plan.removed_top < plan.crop.y0
        assert plan.removed_bottom is not None
        assert plan.removed_bottom > plan.crop.y1


def test_scanned_page_trims_margins_and_footer(scan_pdf):
    plans, feats = analyze(str(scan_pdf))
    assert all(p.is_scanned for p in plans)
    for plan in plans:
        assert plan.crop.width < plan.width
        assert plan.crop.height < plan.height
    # geometric band detection must catch header and footer on scans
    assert sum(1 for p in plans if p.removed_top is not None) >= 2
    assert sum(1 for p in plans if p.removed_bottom is not None) >= 2


def test_rotated_page_keeps_content(scan_rot_pdf):
    plans, _ = analyze(str(scan_rot_pdf))
    p0 = plans[0]
    # rotated page must not lose most of its content to a bogus band trim
    kept = p0.crop.width * p0.crop.height / (p0.width * p0.height)
    assert kept > 0.6, p0.crop
    # horizontal margins are trimmed
    assert p0.crop.x0 > 0 or p0.crop.x1 < p0.width


def test_no_header_footer_mode_only_trims_margins(text_pdf):
    plans, _ = analyze(str(text_pdf), header_footer_zone=0.0)
    for plan in plans:
        assert plan.removed_top is None
        assert plan.removed_bottom is None
        # but margins are still trimmed
        assert plan.crop.width < plan.width
        assert plan.crop.height < plan.height


def test_tight_scan_repeated_header_removed(tight_scan_pdf):
    """Regression: book-like dense scan whose centered running header sits
    exactly one line-gap above the body must still be removed (repetition-
    based detection; the geometric gap test alone cannot tell them apart)."""
    plans, feats = analyze(str(tight_scan_pdf))
    assert sum(1 for p in plans if p.removed_top is not None) >= 2
    for p in plans:
        if p.removed_top is not None:
            assert p.removed_top < p.crop.y0, p  # header band is gone
            assert p.crop.y0 > 60, p


def test_watermark_scan_header_and_footer_removed(watermark_scan_pdf):
    """Regression: a light repeated watermark must not block header/footer
    detection on scanned pages (cross-page pixel-median watermark removal)."""
    plans, feats = analyze(str(watermark_scan_pdf))
    # running header is gone
    assert sum(1 for p in plans if p.removed_top is not None) >= 2
    # footer page number is gone
    assert sum(1 for p in plans if p.removed_bottom is not None) >= 2
    # the watermark is excluded from the crop (content does not reach the
    # very bottom edge where the watermark / footer strip sits)
    for p in plans:
        if p.removed_bottom is not None:
            assert p.crop.y1 < p.height - 10, p


def test_blank_page_is_left_alone(tmp_path):
    import pymupdf as fitz

    p = tmp_path / "blank.pdf"
    doc = fitz.open()
    doc.new_page(width=595, height=842)
    doc.save(str(p))
    doc.close()
    plans, _ = analyze(str(p))
    assert plans[0].crop == fitz.Rect(0, 0, 595, 842)
