"""Tests for the horizontal (landscape) page split with overlap."""

from __future__ import annotations

import pymupdf as fitz
import pytest

from kindle_pdf_cropper.analysis import analyze_document, normalize_work_doc
from kindle_pdf_cropper.config import Config, kindle_device_ratio
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


def test_split_max_aspect_ratio_adds_side_padding(text_pdf):
    plans, _ = adaptive_split_analyze(
        str(text_pdf), split_max_aspect_ratio=0.1
    )
    padded = 0
    for p in plans:
        for seg in p.segments:
            assert seg.height / seg.width <= 0.1 + 1e-6
            if seg.width > seg.clip.width + 1e-6:
                padded += 1
                assert abs(seg.height / seg.width - 0.1) < 1e-6
                assert seg.pad_left > 0 and seg.pad_right > 0
                assert abs(
                    seg.pad_left + seg.clip.width + seg.pad_right - seg.width
                ) < 1e-6
                assert seg.clip.height == seg.height
    assert padded > 0


def test_split_max_aspect_ratio_leaves_wide_pages_unchanged(text_pdf):
    plans, _ = adaptive_split_analyze(
        str(text_pdf), split_max_aspect_ratio=0.95
    )
    for p in plans:
        for seg in p.segments:
            assert seg.width == seg.clip.width
            assert seg.pad_left == 0 and seg.pad_right == 0


def test_device_type_derives_landscape_ratio():
    c = Config(split=True, device_type="paperwhite-5").derived()
    assert abs(c.split_max_aspect_ratio - 1236 / 1648) < 1e-12
    # family aliases are conservative enough for every generation
    alias = Config(split=True, device_type="paperwhite").derived()
    assert alias.split_max_aspect_ratio <= 758 / 1024
    assert kindle_device_ratio("kindle-paperwhite-5") == 1236 / 1648
    assert kindle_device_ratio("Kindle Paperwhite") == 758 / 1024
    assert kindle_device_ratio("amazon-kindle-oasis-3") == 1264 / 1680


def test_aspect_ratio_options_are_validated():
    with pytest.raises(ValueError):
        Config(
            split=True,
            device_type="kindle",
            split_max_aspect_ratio=0.75,
        ).derived()
    with pytest.raises(ValueError):
        Config(device_type="kindle").derived()
    with pytest.raises(ValueError):
        Config(split=True, device_type="not-a-kindle").derived()
    with pytest.raises(ValueError):
        Config(split=True, split_max_aspect_ratio=1.5).derived()


def test_device_type_none_means_no_limit():
    c = Config(split=True, device_type="none").derived()
    assert c.split_max_aspect_ratio is None


def test_pdf_output_uses_padded_page_size(text_pdf, tmp_path):
    plans, _ = adaptive_split_analyze(
        str(text_pdf), split_max_aspect_ratio=0.1
    )
    out = tmp_path / "split-wide.pdf"
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
        for page, seg in zip(
            doc, [s for p in plans for s in p.segments]
        ):
            assert abs(page.rect.width - seg.width) < 0.5
            assert page.rect.height / page.rect.width <= 0.1 + 1e-3
            if seg.width > seg.clip.width + 1e-6:
                assert abs(page.rect.height / page.rect.width - 0.1) < 1e-3
        # content still selectable, padding did not rasterize the page
        assert "quick brown fox" in doc[0].get_text()
    finally:
        doc.close()
