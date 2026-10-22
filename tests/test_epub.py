"""Tests for the fixed-layout EPUB writer."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile

import pymupdf as fitz

from kindle_pdf_cropper.analysis import analyze_document, normalize_work_doc
from kindle_pdf_cropper.config import Config
from kindle_pdf_cropper.output_epub import write_epub

NS_OPF = "http://www.idpf.org/2007/opf"


def build_epub(src_path: str, out_path: str, **cfg_overrides):
    src = fitz.open(src_path)
    try:
        work, rotations = normalize_work_doc(src)
        try:
            cfg = Config(**cfg_overrides).derived()
            plans, feats = analyze_document(work, rotations, cfg)
            write_epub(
                work, plans, out_path, cfg,
                title="Test Book",
                src_meta=src.metadata,
                src_toc=src.get_toc(),
            )
            return plans
        finally:
            work.close()
    finally:
        src.close()


def test_epub_structure_and_svg_text(text_pdf, tmp_path):
    out = tmp_path / "book.epub"
    plans = build_epub(str(text_pdf), str(out))
    with zipfile.ZipFile(str(out)) as z:
        names = z.namelist()
        first = z.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"

        opf = ET.fromstring(z.read("content.opf"))
        manifest = opf.find(f"{{{NS_OPF}}}manifest")
        spine = opf.find(f"{{{NS_OPF}}}spine")
        items = {
            it.attrib["id"]: it.attrib for it in manifest
        }
        refs = spine.findall(f"{{{NS_OPF}}}itemref")
        assert len(refs) == len(plans)

        # every manifest file exists in the zip
        for it in items.values():
            assert it["href"] in names, it["href"]

        # text pages are SVG and contain real text
        xhtml = z.read("pages/page-00001.xhtml").decode()
        ET.fromstring(xhtml)  # well-formed XML
        assert "<svg" in xhtml
        assert "<text" in xhtml


def test_epub_scanned_uses_images(scan_pdf, tmp_path):
    out = tmp_path / "book.epub"
    build_epub(str(scan_pdf), str(out))
    with zipfile.ZipFile(str(out)) as z:
        xhtml = z.read("pages/page-00001.xhtml").decode()
        assert "<svg" not in xhtml
        assert "images/page-00001.jpg" in xhtml
        assert z.read("images/page-00001.jpg")


def test_epub_split_doubles_pages(text_pdf, tmp_path):
    out = tmp_path / "book.epub"
    plans = build_epub(
        str(text_pdf), str(out),
        split=True, split_overlap_min_pt=20.0, split_overlap_ratio=0.03,
    )
    with zipfile.ZipFile(str(out)) as z:
        spine = ET.fromstring(z.read("content.opf")).find(f"{{{NS_OPF}}}spine")
        assert len(spine.findall(f"{{{NS_OPF}}}itemref")) == 2 * len(plans)


def test_epub_image_mode_is_explicit(text_pdf, tmp_path):
    out = tmp_path / "book.epub"
    build_epub(str(text_pdf), str(out), epub_mode="image")
    with zipfile.ZipFile(str(out)) as z:
        xhtml = z.read("pages/page-00001.xhtml").decode()
        assert "<svg" not in xhtml
        assert "<img" in xhtml
