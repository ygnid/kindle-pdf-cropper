"""EPUB output (EPUB 3 fixed-layout).

An EPUB is a reflowable container, so "exact page layout" can only be kept by
pinning every page to a fixed viewport.  Two strategies are provided:

* ``image``  -- every output page is a bitmap (JPEG/PNG).  Absolutely
  faithful to the source page, works everywhere, but text is no longer
  selectable (same trade-off as a scanned book).
* ``svg``    -- every output page is an inline SVG generated from the PDF.
  Text stays text, vector graphics (table rules, formulas, drawings) stay
  vector, images are embedded.  Requires an EPUB reader with SVG support.
* ``auto``   -- the default: svg for text pages, image for scanned pages.
"""

from __future__ import annotations

import logging
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import pymupdf as fitz
from PIL import Image

from .config import Config
from .model import PagePlan

log = logging.getLogger(__name__)


def _render_image(
    page: fitz.Page, clip: fitz.Rect, dpi: int, max_side: int
) -> Image.Image:
    longest_pt = max(abs(clip.width), abs(clip.height))
    dpi = min(dpi, int(max_side * 72.0 / longest_pt)) if longest_pt > 0 else dpi
    dpi = max(72, dpi)
    pix = page.get_pixmap(
        dpi=dpi, clip=clip, alpha=False, colorspace=fitz.csRGB
    )
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    # detect grayscale pages -> store as 'L' (roughly 2/3 smaller)
    if img.mode == "RGB":
        ex = img.convert("RGB").getextrema()
        if ex[0] == ex[1] == ex[2]:
            img = img.convert("L")
    return img


def _segment_svg(
    work: fitz.Document, plan: PagePlan, clip: fitz.Rect
) -> tuple[str, float, float]:
    segdoc = fitz.open()
    try:
        sp = segdoc.new_page(width=clip.width, height=clip.height)
        sp.show_pdf_page(sp.rect, work, plan.index, clip=clip)
        svg = sp.get_svg_image(text_as_path=False)
    finally:
        segdoc.close()
    # the root <svg> keeps its own user units (viewBox); CSS scales it to the
    # fixed-layout page box
    return svg, abs(clip.width), abs(clip.height)


def write_epub(
    work: fitz.Document,
    plans: list[PagePlan],
    out_path: str | Path,
    config: Config,
    title: str,
    src_meta: dict | None = None,
    src_toc: list | None = None,
) -> None:
    out_path = Path(out_path)
    tmp = out_path.parent / (out_path.stem + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    pages: list[dict] = []  # per output page
    image_files: list[tuple[Path, str]] = []  # (file, media-type)
    (tmp / "images").mkdir(parents=True, exist_ok=True)
    try:
        for plan in plans:
            page = work[plan.index]
            for si, seg in enumerate(plan.segments):
                use_svg = (
                    config.epub_mode == "svg"
                    or (config.epub_mode == "auto" and not plan.is_scanned)
                )
                name = f"page-{len(pages) + 1:05d}"
                if use_svg:
                    svg, w, h = _segment_svg(work, plan, seg.clip)
                    pages.append(
                        {
                            "name": name,
                            "kind": "svg",
                            "svg": svg,
                            "width": w,
                            "height": h,
                        }
                    )
                else:
                    img = _render_image(
                        page, seg.clip, config.epub_dpi, config.epub_max_side
                    )
                    if config.epub_image_format == "png":
                        ext, media, save = "png", "image/png", {}
                    else:
                        ext, media, save = (
                            "jpg",
                            "image/jpeg",
                            {"quality": config.epub_jpeg_quality},
                        )
                    rel = f"images/{name}.{ext}"
                    img.save(tmp / rel, **save)
                    image_files.append((tmp / rel, media))
                    pages.append(
                        {
                            "name": name,
                            "kind": "image",
                            "src": rel,
                            "width": img.width,
                            "height": img.height,
                        }
                    )
                # keep src page indexing for nav mapping
                pages[-1]["src_page"] = plan.index

        # cover from the first page
        cover_rel: str | None = None
        if pages:
            first = pages[0]
            if first["kind"] == "image":
                cover_rel = first["src"]
            else:
                cover = _render_image(
                    work[plans[0].index],
                    plans[0].segments[0].clip,
                    150,
                    config.epub_max_side,
                )
                cover_rel = "images/cover.jpg"
                cover.save(tmp / cover_rel, quality=85)
                image_files.append((tmp / cover_rel, "image/jpeg"))

        _assemble_epub(tmp, out_path, pages, image_files, cover_rel,
                       title, src_meta, src_toc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _assemble_epub(
    tmp: Path,
    out_path: Path,
    pages: list[dict],
    image_files: list[tuple[Path, str]],
    cover_rel: str | None,
    title: str,
    src_meta: dict | None,
    src_toc: list | None,
) -> None:
    uid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lang = (src_meta or {}).get("language") or "en"
    author = (src_meta or {}).get("author") or ""
    creator = (src_meta or {}).get("creator") or ""

    # --- per-page XHTML ---------------------------------------------------
    (tmp / "pages").mkdir(parents=True, exist_ok=True)
    css_blocks = []
    for p in pages:
        w = max(1, int(round(p["width"])))
        h = max(1, int(round(p["height"])))
        p["w_px"], p["h_px"] = w, h
        if p["kind"] == "svg":
            body = (
                f'<div class="page {p["name"]}">\n'
                f'{p["svg"]}\n'
                "</div>"
            )
        else:
            body = (
                f'<div class="page {p["name"]}">\n'
                f'<img src="../{p["src"]}" alt="" />\n'
                "</div>"
            )
        css_blocks.append(f".{p['name']}{{width:{w}px;height:{h}px;}}")
        xhtml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops" '
            f'xml:lang="{escape(lang)}">\n'
            "<head>\n"
            f"<title>{escape(title)}</title>\n"
            '<meta charset="utf-8"/>\n'
            f'<meta name="viewport" content="width={w},height={h}"/>\n'
            '<meta property="rendition:layout">pre-paginated</meta>\n'
            '<link rel="stylesheet" type="text/css" href="../css/style.css"/>\n'
            "</head>\n"
            f'<body class="{p["name"]}">\n{body}\n</body>\n'
            "</html>\n"
        )
        (tmp / "pages" / f"{p['name']}.xhtml").write_text(
            xhtml, encoding="utf-8"
        )

    # --- CSS --------------------------------------------------------------
    css = "\n".join(
        [
            "html,body{margin:0;padding:0;}",
            "body{background:#fff;}",
            ".page{position:relative;margin:0;padding:0;overflow:hidden;}",
            ".page img{display:block;width:100%;height:100%;}",
            ".page svg{display:block;width:100%;height:100%;}",
            *css_blocks,
        ]
    )
    (tmp / "css").mkdir(parents=True, exist_ok=True)
    (tmp / "css" / "style.css").write_text(css, encoding="utf-8")

    # --- nav --------------------------------------------------------------
    nav_items = []
    if src_toc:
        for entry in src_toc:
            level, t, page = entry[0], entry[1], entry[2]
            # map source page to the first output page derived from it
            out_idx = _first_out_index(pages, page - 1)
            nav_items.append((level, t, out_idx))
    if not nav_items:
        nav_items = [(1, f"Page {i + 1}", i) for i in range(len(pages))]
    nav_entries = []
    for level, t, idx in nav_items:
        nav_entries.append(
            f'<li><a href="pages/{pages[idx]["name"]}.xhtml">'
            f"{escape(t)}</a></li>"
        )
    nav = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en">\n'
        "<head><title>Table of Contents</title></head>\n"
        '<body><nav epub:type="toc"><ol>\n'
        + "\n".join(nav_entries)
        + "\n</ol></nav></body></html>\n"
    )
    (tmp / "nav.xhtml").write_text(nav, encoding="utf-8")

    # --- OPF ---------------------------------------------------------------
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
        'properties="nav"/>',
        '<item id="css" href="css/style.css" media-type="text/css"/>',
    ]
    spine = []
    for i, p in enumerate(pages):
        manifest.append(
            f'<item id="p{i}" href="pages/{p["name"]}.xhtml" '
            'media-type="application/xhtml+xml"/>'
        )
        spine.append(f'<itemref idref="p{i}" linear="yes"/>')
    for i, (f, media) in enumerate(image_files):
        rel = f.relative_to(tmp).as_posix()
        props = ' properties="cover-image"' if rel == cover_rel else ""
        manifest.append(
            f'<item id="img{i}" href="{rel}" media-type="{media}"{props}/>'
        )
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="pub-id" xml:lang="en">\n'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'<dc:identifier id="pub-id">urn:uuid:{uid}</dc:identifier>\n'
        f"<dc:title>{escape(title)}</dc:title>\n"
        f"<dc:language>{escape(lang)}</dc:language>\n"
        + (f"<dc:creator>{escape(author)}</dc:creator>\n" if author else "")
        + (f"<dc:contributor>{escape(creator)}</dc:contributor>\n" if creator else "")
        + f'<meta property="dcterms:modified">{now}</meta>\n'
        + '<meta property="rendition:layout">pre-paginated</meta>\n'
        + '<meta property="rendition:orientation">auto</meta>\n'
        + '<meta property="rendition:spread">none</meta>\n'
        + "</metadata>\n"
        + '<manifest>\n' + "\n".join(manifest) + "\n</manifest>\n"
        + '<spine>\n' + "\n".join(spine) + "\n</spine>\n"
        + "</package>\n"
    )
    (tmp / "content.opf").write_text(opf, encoding="utf-8")

    (tmp / "META-INF").mkdir(parents=True, exist_ok=True)
    (tmp / "META-INF" / "container.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        '<rootfiles><rootfile full-path="content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles>\n'
        "</container>\n",
        encoding="utf-8",
    )
    # Apple Books fixed-layout switch
    (tmp / "META-INF" / "com.apple.ibooks.display-options.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<display_options xmlns="http://www.idpf.org/2014/displayoptions">\n'
        '<platform name="*"><option name="fixed-layout">true</option>'
        '<option name="specified-fonts">true</option></platform>\n'
        "</display_options>\n",
        encoding="utf-8",
    )

    # --- zip ---------------------------------------------------------------
    with zipfile.ZipFile(out_path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            (tmp / "META-INF/container.xml").read_bytes(),
            zipfile.ZIP_DEFLATED,
        )
        z.writestr(
            "META-INF/com.apple.ibooks.display-options.xml",
            (tmp / "META-INF/com.apple.ibooks.display-options.xml").read_bytes(),
            zipfile.ZIP_DEFLATED,
        )
        z.write(tmp / "content.opf", "content.opf", zipfile.ZIP_DEFLATED)
        z.write(tmp / "nav.xhtml", "nav.xhtml", zipfile.ZIP_DEFLATED)
        z.write(tmp / "css/style.css", "css/style.css", zipfile.ZIP_DEFLATED)
        for p in pages:
            z.write(
                tmp / "pages" / f"{p['name']}.xhtml",
                f"pages/{p['name']}.xhtml",
                zipfile.ZIP_DEFLATED,
            )
        for f, _ in image_files:
            z.write(f, f.relative_to(tmp).as_posix(), zipfile.ZIP_DEFLATED)


def _first_out_index(pages: list[dict], src_page: int) -> int:
    for i, p in enumerate(pages):
        if p.get("src_page") == src_page:
            return i
    return min(src_page, max(len(pages) - 1, 0))
