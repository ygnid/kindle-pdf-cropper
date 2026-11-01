"""Generate small synthetic PDFs used by tests and manual validation:
1. a text-based PDF (running header, page numbers, table, formula-like text),
2. a scanned-style image PDF,
3. a rotated variant.
"""

from __future__ import annotations

import io
import random

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont

PAGE_W, PAGE_H = 595.0, 842.0  # A4 portrait
MARGIN_L, MARGIN_R, MARGIN_T, MARGIN_B = 80.0, 80.0, 90.0, 90.0


def _fit_line(words: list[str], max_w: float, fontsize: float = 12.0) -> str:
    """Greedily join words into one line that fits `max_w` points."""
    line = ""
    for w in words:
        cand = f"{line} {w}".strip()
        if line and fitz.get_text_length(cand, fontname="helv", fontsize=fontsize) > max_w:
            break
        line = cand
    return line


def make_text_pdf(path: str, pages: int = 5) -> str:
    body_lines = [
        "The quick brown fox jumps over the lazy dog while the margins of "
        "this synthetic page stay completely white and easy to measure.",
        "This paragraph has enough filler text to keep the body region full "
        "of ink so the crop box can be measured very stably for a page.",
        "Formulas like a = b + c or x^2 + y^2 = z^2 are drawn as plain text "
        "and must never be reflowed, only cropped as one single unit.",
        "Tables keep their rules and cell text intact because all vector "
        "graphics are preserved verbatim by the output writer.",
        "The running header above and page number below are repeated "
        "ornaments that the analyzer should detect and remove cleanly.",
    ]
    body_lines = [
        _fit_line(line.split(), PAGE_W - MARGIN_L - MARGIN_R, 12.0)
        for line in body_lines
    ]
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        # running header (repeated every page)
        page.insert_text(
            (PAGE_W / 2 - 60, 60), "Chapter 1  Introduction", fontsize=10,
            fontname="helv",
        )
        y = 150.0
        for para in range(3):
            line = body_lines[(i + para) % len(body_lines)]
            page.insert_text(
                (MARGIN_L, y), line, fontsize=12, fontname="helv"
            )
            y += 24 + 12
        # a table with rules (vector graphics) and cell text
        tb = fitz.Rect(MARGIN_L, y, PAGE_W - MARGIN_R, y + 60)
        page.draw_rect(tb, color=(0, 0, 0), width=1)
        page.draw_line(
            fitz.Point(tb.x0, (tb.y0 + tb.y1) / 2),
            fitz.Point(tb.x1, (tb.y0 + tb.y1) / 2),
            color=(0, 0, 0),
            width=1,
        )
        page.insert_text((tb.x0 + 8, tb.y0 + 18), "Header row", fontsize=11)
        page.insert_text((tb.x0 + 8, tb.y0 + 42), "a = b + c  (x^2)", fontsize=11)
        # footer page number (changes every page)
        page.insert_text(
            (PAGE_W / 2 - 10, PAGE_H - 50), f"- {i + 1} -", fontsize=9,
            fontname="helv",
        )
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


def _make_scan_page(w: int, h: int, page_no: int) -> Image.Image:
    img = Image.new("L", (w, h), 245)
    px = img.load()
    rnd = random.Random(page_no * 977)
    for _ in range(4000):  # paper grain
        x, y = rnd.randrange(w), rnd.randrange(h)
        px[x, y] = 235 + rnd.randrange(12)
    d = ImageDraw.Draw(img)
    try:
        f_title = ImageFont.load_default(size=48)
        f_body = ImageFont.load_default(size=36)
    except TypeError:  # very old Pillow
        f_title = f_body = ImageFont.load_default()
    d.text((w // 2 - 200, 60), f"SCANNED PAGE {page_no}", fill=30, font=f_title)
    y = 300
    for para in range(4):
        d.text((160, y), "This is scanned body text with gray paper grain. " * 3,
               fill=45, font=f_body)
        y += 90
    d.line([(160, y + 10), (w - 160, y + 10)], fill=40, width=6)
    d.text((160, y + 50), "Table cell content", fill=45, font=f_body)
    d.text((w // 2 - 30, h - 140), f"{page_no}", fill=40, font=f_title)
    return img


def make_scanned_pdf(path: str, pages: int = 3, rotate: int = 0) -> str:
    doc = fitz.open()
    for i in range(pages):
        img = _make_scan_page(1240, 1754, i + 1)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(fitz.Rect(0, 0, PAGE_W, PAGE_H), stream=buf.getvalue())
        if rotate and i == 0:
            page.set_rotation(rotate)
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


def _make_tight_scan_page(w: int, h: int, page_no: int) -> Image.Image:
    """A book-like dense scan: centered running header sitting exactly one
    line-gap above the body, tightly spaced text lines (like the real
    Chinese math textbook regression case)."""
    img = Image.new("L", (w, h), 248)
    px = img.load()
    rnd = random.Random(page_no * 311)
    for _ in range(3000):
        x, y = rnd.randrange(w), rnd.randrange(h)
        px[x, y] = 238 + rnd.randrange(10)
    d = ImageDraw.Draw(img)
    try:
        f_header = ImageFont.load_default(size=30)
        f_body = ImageFont.load_default(size=34)
    except TypeError:  # very old Pillow
        f_header = f_body = ImageFont.load_default()
    # running header, centered, one line-gap above the body
    d.text((w // 2 - 90, 80), f"Section {page_no}", fill=35, font=f_header)
    # dense body lines: line pitch 52 px, ink height ~30 px -> 22 px gap
    y = 210
    for ln in range(14):
        d.text((150, y), f"Text line number {ln} of the scanned body "
                         f"with dense leading page {page_no}.", fill=45, font=f_body)
        y += 52
    return img


def make_tight_scan_pdf(path: str, pages: int = 4) -> str:
    doc = fitz.open()
    for i in range(pages):
        img = _make_tight_scan_page(1240, 1754, i + 1)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(fitz.Rect(0, 0, PAGE_W, PAGE_H), stream=buf.getvalue())
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


def _make_watermark_scan_page(w: int, h: int, page_no: int) -> Image.Image:
    """Scan page with a light watermark strip over the footer area and a
    separate page-number footer -- the regression case for watermark
    removal (a light repeated pattern must not block header/footer
    detection)."""
    img = Image.new("L", (w, h), 248)
    px = img.load()
    rnd = random.Random(page_no * 977)
    for _ in range(3000):
        x, y = rnd.randrange(w), rnd.randrange(h)
        px[x, y] = 238 + rnd.randrange(10)
    d = ImageDraw.Draw(img)
    try:
        f_header = ImageFont.load_default(size=30)
        f_body = ImageFont.load_default(size=34)
        f_wm = ImageFont.load_default(size=60)
    except TypeError:  # very old Pillow
        f_header = f_body = f_wm = ImageFont.load_default()
    # running header (one line-gap above the body, repeated every page)
    d.text((w // 2 - 90, 80), f"Section {page_no}", fill=35, font=f_header)
    # body lines
    y = 210
    for ln in range(10):
        d.text((150, y), f"Body text line {ln} of the watermarked page {page_no}.", fill=45, font=f_body)
        y += 60
    # light watermark strip sitting exactly where the footer lives
    for yy in (h - 300, h - 190):
        d.text((w // 2 - 220, yy), "WATERMARK", fill=205, font=f_wm)
    # footer page number, separated from the body
    d.text((w // 2 - 20, h - 160), f"{page_no}", fill=40, font=f_header)
    return img


def make_watermark_scan_pdf(path: str, pages: int = 5) -> str:
    doc = fitz.open()
    for i in range(pages):
        img = _make_watermark_scan_page(1240, 1754, i + 1)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(fitz.Rect(0, 0, PAGE_W, PAGE_H), stream=buf.getvalue())
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


def _make_furniture_scan_page(w: int, h: int, page_no: int) -> Image.Image:
    """Dense scan with the real-world footer/edge problems:

    * a light gray watermark block + text across the footer,
    * a page number + rule sitting immediately under the last body line,
    * a small dark mark on the left paper edge (survives a naive ink box).
    """
    img = Image.new("L", (w, h), 248)
    px = img.load()
    rnd = random.Random(page_no * 733)
    for _ in range(3000):  # paper grain
        x, y = rnd.randrange(w), rnd.randrange(h)
        px[x, y] = 238 + rnd.randrange(10)
    d = ImageDraw.Draw(img)
    try:
        f_header = ImageFont.load_default(size=30)
        f_body = ImageFont.load_default(size=34)
        f_wm = ImageFont.load_default(size=70)
    except TypeError:  # very old Pillow
        f_header = f_body = f_wm = ImageFont.load_default()

    # light repeated watermark occupying the footer area
    d.rectangle([w * 0.18, h * 0.68, w * 0.62, h * 0.95], fill=232)
    d.text((w * 0.24, h * 0.73), "WATERMARK", fill=205, font=f_wm)

    # running header
    d.text((w // 2 - 90, 80), f"Section {page_no}", fill=35, font=f_header)

    # dense body lines reaching close to the footer
    y = 210
    for ln in range(21):
        d.text(
            (150, y),
            f"Text line {ln} of the scanned body with dense leading "
            f"page {page_no}.",
            fill=45,
            font=f_body,
        )
        y += 55

    # page number + thin rule, only ~one short gap below the body
    footer_y = y + 18
    d.text((w // 2 - 20, footer_y), f"{page_no}", fill=40, font=f_header)
    d.line(
        [(w // 2 - 120, footer_y + 38), (w // 2 + 120, footer_y + 38)],
        fill=40,
        width=5,
    )

    # dark scan mark touching the left paper edge
    d.rectangle([0, h * 0.45, 24, h * 0.52], fill=25)
    return img


def make_furniture_scan_pdf(path: str, pages: int = 5) -> str:
    doc = fitz.open()
    for i in range(pages):
        img = _make_furniture_scan_page(1240, 1754, i + 1)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(fitz.Rect(0, 0, PAGE_W, PAGE_H), stream=buf.getvalue())
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


def _make_edge_text_scan_page(w: int, h: int, page_no: int) -> Image.Image:
    """Tight scan whose body lines run all the way to the right paper edge.

    This is common in real books: the scan is cropped at the text block, so
    every line's bounding box touches the page border.  A border-strip filter
    must not mistake those lines for scanner shadows.
    """
    img = Image.new("L", (w, h), 248)
    d = ImageDraw.Draw(img)
    try:
        f_body = ImageFont.load_default(size=34)
    except TypeError:  # very old Pillow
        f_body = ImageFont.load_default()
    rnd = random.Random(page_no * 911)
    for _ in range(2000):
        x, y = rnd.randrange(w), rnd.randrange(h)
        img.putpixel((x, y), 238 + rnd.randrange(10))
    # first line touches the top edge, last line touches the bottom edge
    y = 4
    for ln in range(16):
        text = (
            f"Tight line {ln} of page {page_no} with body text that is cut "
            "exactly at the paper edge"
        )
        tw = d.textlength(text, font=f_body)
        # start so the last glyph is clipped by the page edge; the line's
        # bbox therefore touches the right border along its short side
        d.text((w - tw + 3, y), text, fill=45, font=f_body)
        y += 62
    d.text(
        (4, h - 26),
        f"full width bottom line of page {page_no} reaching both paper edges "
        "and continuing on and on",
        fill=45,
        font=f_body,
    )
    return img


def _make_preface_scan_page(w: int, h: int, page_no: int) -> Image.Image:
    """Scan of a preface end: a short body block, a huge blank gap, then a
    right-aligned signature.  The signature is separated from the body by
    much more than a line gap, so a naive footer detector would carry it away
    even though it is real content."""
    img = Image.new("L", (w, h), 248)
    d = ImageDraw.Draw(img)
    try:
        f_body = ImageFont.load_default(size=34)
    except TypeError:  # very old Pillow
        f_body = ImageFont.load_default()
    rnd = random.Random(page_no * 577)
    for _ in range(2000):
        x, y = rnd.randrange(w), rnd.randrange(h)
        img.putpixel((x, y), 238 + rnd.randrange(10))
    y = 160
    for ln in range(7):
        d.text(
            (150, y),
            f"Preface body line {ln} of page {page_no} ends the text block.",
            fill=45,
            font=f_body,
        )
        y += 60
    base = h - 250 - (page_no - 1) * 40  # vary so it is not "repeated furniture"
    for j, sig in enumerate(("Hersh Shefrin", f"October, 2004  ({page_no})")):
        tw = d.textlength(sig, font=f_body)
        d.text((w - 150 - tw, base + j * 60), sig, fill=45, font=f_body)
    return img


def make_preface_scan_pdf(path: str, pages: int = 4) -> str:
    doc = fitz.open()
    for i in range(pages):
        img = _make_preface_scan_page(1240, 1754, i + 1)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(fitz.Rect(0, 0, PAGE_W, PAGE_H), stream=buf.getvalue())
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


def _make_bottom_title_scan_page(w: int, h: int, page_no: int) -> Image.Image:
    """Page whose only large text is a centered part title near the bottom,
    separated from a small top block by a huge blank gap (real TOC opener)."""
    img = Image.new("L", (w, h), 248)
    d = ImageDraw.Draw(img)
    try:
        f_body = ImageFont.load_default(size=34)
        f_title = ImageFont.load_default(size=64)
    except TypeError:  # very old Pillow
        f_body = f_title = ImageFont.load_default()
    rnd = random.Random(page_no * 641)
    for _ in range(2000):
        x, y = rnd.randrange(w), rnd.randrange(h)
        img.putpixel((x, y), 238 + rnd.randrange(10))
    d.text((180, 170), f"TOC entry {page_no}", fill=45, font=f_body)
    d.text((180, 260), f"another entry {page_no}", fill=45, font=f_body)
    title = f"Part {page_no}: behavioural finance"
    tw = d.textlength(title, font=f_title)
    d.text(((w - tw) / 2, int(h * 0.82)), title, fill=30, font=f_title)
    return img


def make_bottom_title_scan_pdf(path: str, pages: int = 4) -> str:
    doc = fitz.open()
    for i in range(pages):
        img = _make_bottom_title_scan_page(1240, 1754, i + 1)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(fitz.Rect(0, 0, PAGE_W, PAGE_H), stream=buf.getvalue())
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


def make_edge_text_scan_pdf(path: str, pages: int = 4) -> str:
    doc = fitz.open()
    for i in range(pages):
        img = _make_edge_text_scan_page(1240, 1754, i + 1)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_image(fitz.Rect(0, 0, PAGE_W, PAGE_H), stream=buf.getvalue())
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return path


if __name__ == "__main__":
    import sys

    kind = sys.argv[1] if len(sys.argv) > 1 else "text"
    out = sys.argv[2] if len(sys.argv) > 2 else f"sample_{kind}.pdf"
    if kind == "text":
        make_text_pdf(out)
    elif kind == "scan":
        make_scanned_pdf(out, rotate=int(sys.argv[3]) if len(sys.argv) > 3 else 0)
    print(out)
