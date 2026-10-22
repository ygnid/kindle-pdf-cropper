"""PDF output: physically re-render each page into new pages whose visible
area is exactly the crop (or split) rectangle.

Unlike setting a /CropBox, this produces a PDF whose page size *is* the crop,
so every viewer (Kindle, Sumatra, Acrobat...) shows the trimmed page without
relying on CropBox support.  Because `Page.show_pdf_page` copies the source
content as a Form XObject (no rasterization), text remains selectable text
and vector graphics / fonts are preserved verbatim.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from .model import PagePlan


def write_pdf(
    work: fitz.Document,
    plans: list[PagePlan],
    out_path: str | Path,
    src_meta: dict | None = None,
    src_toc: list | None = None,
) -> None:
    out = fitz.open()
    page_map: dict[int, int] = {}  # source page -> first output page (1-based)
    for plan in plans:
        first = len(out)
        for seg in plan.segments:
            np_ = out.new_page(width=seg.width, height=seg.height)
            np_.show_pdf_page(np_.rect, work, plan.index, clip=seg.clip)
        page_map[plan.index] = first + 1

    meta = {
        "title": (src_meta or {}).get("title") or "Cropped PDF",
        "author": (src_meta or {}).get("author") or "",
        "subject": (src_meta or {}).get("subject") or "",
        "keywords": (src_meta or {}).get("keywords") or "",
        "creator": (src_meta or {}).get("creator")
        or "kindle-pdf-cropper",
        "producer": "kindle-pdf-cropper",
    }
    out.set_metadata(meta)

    if src_toc:
        remapped = []
        for entry in src_toc:
            level, title, page = entry[0], entry[1], entry[2]
            dst = page_map.get(page - 1, page)
            remapped.append([level, title, dst])
        try:
            out.set_toc(remapped)
        except Exception:
            pass

    out.save(
        str(out_path),
        garbage=4,
        deflate=True,
        deflate_images=True,
        deflate_fonts=True,
    )
    out.close()
