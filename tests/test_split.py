"""Tests for the horizontal (landscape) page split with overlap."""

from __future__ import annotations

import pymupdf as fitz

from kindle_pdf_cropper.analysis import analyze_document, normalize_work_doc
from kindle_pdf_cropper.config import Config
from kindle_pdf_cropper.output_pdf import write_pdf

OVERLAP_PT = 20.0
OVERLAP_RATIO = 0.03


def split_analyze(path: str):
    src = fitz.open(path)
    try:
        work, rotations = normalize_work_doc(src)
        try:
            cfg = Config(
                split=True,
                split_overlap_min_pt=OVERLAP_PT,
                split_overlap_ratio=OVERLAP_RATIO,
            ).derived()
            return analyze_document(work, rotations, cfg)
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
