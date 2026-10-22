"""Tests for PDF output: physical cropping with text/vector preservation."""

from __future__ import annotations

import pymupdf as fitz

from kindle_pdf_cropper.config import Config
from kindle_pdf_cropper.output_pdf import write_pdf
from kindle_pdf_cropper.analysis import analyze_document, normalize_work_doc


def make_output(src_path: str, out_path: str, **cfg_overrides):
    src = fitz.open(src_path)
    try:
        work, rotations = normalize_work_doc(src)
        try:
            cfg = Config(**cfg_overrides).derived()
            plans, feats = analyze_document(work, rotations, cfg)
            write_pdf(work, plans, out_path, src_meta=src.metadata)
            return plans
        finally:
            work.close()
    finally:
        src.close()


def test_pdf_page_count_and_size(text_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    plans = make_output(str(text_pdf), str(out))
    doc = fitz.open(str(out))
    try:
        assert len(doc) == len(plans)
        for page, plan in zip(doc, plans):
            assert abs(page.rect.width - plan.segments[0].width) < 0.5
            assert abs(page.rect.height - plan.segments[0].height) < 0.5
    finally:
        doc.close()


def test_pdf_text_stays_text(text_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    make_output(str(text_pdf), str(out))
    doc = fitz.open(str(out))
    try:
        t = doc[0].get_text()
        # body / table / formula text is still selectable text
        assert "quick brown fox" in t
        assert "Header row" in t
        assert "a = b + c" in t
        assert "x^2" in t
        # header and footer are gone
        assert "Chapter 1" not in t
        assert "- 1 -" not in t
    finally:
        doc.close()


def test_pdf_scanned_keeps_image(scan_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    make_output(str(scan_pdf), str(out))
    doc = fitz.open(str(out))
    try:
        page = doc[0]
        pix = page.get_pixmap(dpi=72)
        # rendered page has real content (not blank)
        import numpy as np

        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )
        dark = (arr[..., :3].mean(axis=2) < 200).mean()
        assert dark > 0.01
    finally:
        doc.close()
