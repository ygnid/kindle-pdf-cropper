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
    import numpy as np

    from kindle_pdf_cropper.analysis import _remove_border_blobs

    plans, feats = analyze(str(scan_rot_pdf))
    p0, f0 = plans[0], feats[0]
    # the crop must contain the page's real content: a rotated page may place
    # its content in a sub-region of the displayed (landscape) page, so the
    # correct behaviour is a *tight* crop around that content, not a bogus
    # band trim that removes it.  Use the cleaned mask (specks/paper grain
    # deliberately excluded) as the content reference.
    content = _remove_border_blobs(f0.ink, max_speck_area=3)
    ys, xs = np.nonzero(content)
    assert xs.size
    block_pt = f0.block * f0.pt_per_px
    cx0, cx1 = xs.min() * block_pt, (xs.max() + 1) * block_pt
    cy0, cy1 = ys.min() * block_pt, (ys.max() + 1) * block_pt
    assert p0.crop.x0 <= cx0 + 1e-6
    assert p0.crop.y0 <= cy0 + 1e-6
    assert p0.crop.x1 >= cx1 - 1e-6
    assert p0.crop.y1 >= cy1 - 1e-6
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


def test_scan_edge_mark_and_close_footer_are_ignored(furniture_scan_pdf):
    """Regression for real scanned books: a dark mark on the paper edge must
    not keep a wide left margin, and a page number sitting directly under the
    last body line (inside a light watermark) must still be removed."""
    plans, _ = analyze(str(furniture_scan_pdf))
    for plan in plans:
        assert plan.crop.x0 > 20, plan.crop
        assert plan.crop.y1 < plan.height * 0.82, plan.crop
    assert sum(1 for p in plans if p.removed_top is not None) >= 4
    assert sum(1 for p in plans if p.removed_bottom is not None) >= 4


def test_full_width_lines_touching_page_edge_are_kept(edge_text_scan_pdf):
    """Regression: a body line ending at the paper edge must not be removed
    as a "thin scan border".  Doing so collapsed the column projection and
    cropped away the right half of tightly scanned pages."""
    plans, _ = analyze(str(edge_text_scan_pdf))
    for plan in plans:
        assert plan.crop.x1 >= plan.width - 6, plan.crop
        assert plan.crop.width > 0.8 * plan.width, plan.crop
        # ...and a line that runs into the top/bottom edge must survive too
        assert plan.crop.y0 <= 8, plan.crop
        assert plan.crop.y1 >= plan.height - 8, plan.crop


def test_sparse_page_signature_not_eaten_as_footer(preface_scan_pdf):
    """Regression: a right-aligned signature at the bottom of a sparse page is
    separated from the body by a large blank gap.  It is real content and must
    survive (the over-eager footer rule used to crop it away)."""
    plans, _ = analyze(str(preface_scan_pdf))
    for plan in plans:
        assert plan.removed_bottom is None, plan.removed_bottom
        assert plan.crop.y1 > plan.height * 0.78, plan.crop


def test_bottom_part_title_not_eaten_as_footer(bottom_title_scan_pdf):
    """Regression: a centered part title sitting at the bottom of a sparse TOC
    opener is real content, not a page number, and must survive."""
    plans, _ = analyze(str(bottom_title_scan_pdf))
    for plan in plans:
        assert plan.removed_bottom is None, plan.removed_bottom
        assert plan.crop.y1 > plan.height * 0.85, plan.crop


def test_blank_page_is_left_alone(tmp_path):
    import pymupdf as fitz

    p = tmp_path / "blank.pdf"
    doc = fitz.open()
    doc.new_page(width=595, height=842)
    doc.save(str(p))
    doc.close()
    plans, _ = analyze(str(p))
    assert plans[0].crop == fitz.Rect(0, 0, 595, 842)
