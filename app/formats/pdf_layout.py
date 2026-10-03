"""Lay out Unicode text in an isolated PDF before touching a source page."""

import html
import unicodedata

import pymupdf as fitz

from .common import FormatAdapterError


def render_text(text: str, width: float, height: float | None, size: float, *,
                color=(0.0, 0.0, 0.0), font="", minimum_size=4.0, align="left"):
    if any(unicodedata.category(char) == "Cc" and char not in "\n\r\t" for char in text):
        raise FormatAdapterError("PDF translation contains an unsupported control character")
    if width <= 0 or height is not None and height <= 0:
        return None
    size = max(1, float(size))
    requested_height = height
    color = tuple(max(0, min(255, round(value * 255))) for value in color)
    name = font.lower()
    family = "monospace" if "courier" in name or "mono" in name else "sans-serif"
    weight = "bold" if any(word in name for word in ("bold", "heavy", "black")) else "normal"
    style = "italic" if "italic" in name or "oblique" in name else "normal"
    padding = [size * 0.1] * 4
    css_padding = " ".join(f"{value}pt" for value in (padding[0], padding[1], 0, padding[3]))
    css = (f"body {{margin:0;padding:{css_padding};font-family:{family};font-size:{size}pt;"
           f"line-height:1.3;font-weight:{weight};font-style:{style};"
           f"color:rgb({color[0]},{color[1]},{color[2]});}}"
           f"p {{margin:0;padding:0;white-space:pre-wrap;text-align:{align};}}")
    escaped = html.escape(text).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    # MuPDF's flow fitter excludes the body's trailing padding. A trailing
    # empty block with top padding reserves genuine space for descenders.
    story = fitz.Story(html=f'<p>{escaped}</p><div style="padding-top:{padding[2]}pt"></div>', user_css=css)
    if height is None:
        measurement = story.fit_scale(fitz.Rect(0, 0, width, 13944), scale_min=1, scale_max=1)
        if not measurement.big_enough:
            return None
        height = max(size * 1.1, measurement.filled[3]) + 1
    fit = story.fit_scale(fitz.Rect(0, 0, width, height), scale_min=1,
                          scale_max=max(1.0, size / min(size, minimum_size)))
    if not fit.big_enough:
        return None
    document = story.write_with_links(lambda *_: (fit.rect, fit.rect, fitz.Identity))
    try:
        if document.page_count != 1:
            raise FormatAdapterError("PDF paragraph layout unexpectedly spans multiple pages")
        page = document[0]
        extent = fitz.Rect(page.rect)
        for span in page.get_texttrace():
            extent |= fitz.Rect(span["bbox"])
        if requested_height is not None and size * min(width / extent.width, height / extent.height) < min(size, minimum_size) - 0.01:
            document.close()
            return None
        if requested_height is None and extent.height > 13944:
            document.close()
            return None
        if extent != page.rect:
            # Fallback-script glyphs can extend beyond the CSS line box. Expand
            # the isolated fragment, then uniformly fit that complete extent.
            page.set_mediabox(extent * ~page.transformation_matrix)
            page = document.reload_page(page)
        for span in page.get_texttrace():
            for codepoint, glyph, *_ in span["chars"]:
                character = chr(codepoint) if 0 <= codepoint <= 0x10FFFF else "\ufffd"
                if glyph == 0 and not character.isspace() and unicodedata.category(character) != "Cf":
                    raise FormatAdapterError(f"PDF font cannot display U+{codepoint:04X}; no output was replaced")
        # Grafting a full CJK font for every paragraph would multiply output size
        # and memory. Keep only the glyphs used by this fragment before grafting.
        document.subset_fonts()
        page = document.reload_page(page)
        # ActualText preserves logical Unicode for searching/copying shaped scripts
        # and ligatures, independently of the font's glyph-to-Unicode mapping.
        actual = (b"\xfe\xff" + text.encode("utf-16-be")).hex().encode()
        content = b"/Span <</ActualText <" + actual + b">>> BDC\n" + page.read_contents() + b"\nEMC\n"
        xref = document.get_new_xref()
        document.update_object(xref, "<<>>")
        document.update_stream(xref, content)
        page.set_contents(xref)
        return document
    except Exception:
        if not document.is_closed:
            document.close()
        raise


def annotation_array(page) -> str:
    kind, value = page.parent.xref_get_key(page.xref, "Annots")
    if kind == "null":
        return "[]"
    if kind == "xref":
        value = page.parent.xref_object(int(value.split()[0]))
    if not value.strip().startswith("["):
        raise FormatAdapterError("PDF annotation structure cannot be safely preserved")
    return value


def validate_source(document, bilingual=False):
    for index, page in enumerate(document):
        if next(page.widgets() or iter(()), None) is not None:
            raise FormatAdapterError(f"Page {index + 1} contains PDF form fields; safe translation export is unavailable")
        annotations = list(page.annots() or [])
        if any(annotation.type[0] == fitz.PDF_ANNOT_REDACT for annotation in annotations):
            raise FormatAdapterError(f"Page {index + 1} has unapplied redaction marks; remove them before translating")
        if bilingual and annotations:
            raise FormatAdapterError(f"Page {index + 1} contains annotations that bilingual export cannot preserve")
        if bilingual and any(link["kind"] not in (fitz.LINK_URI, fitz.LINK_GOTO) for link in page.get_links()):
            raise FormatAdapterError(f"Page {index + 1} contains an unsupported link action for bilingual export")


def copy_page_links(source_page, destination_page):
    # get_links() reports rectangles in the displayed page's coordinate system,
    # matching show_pdf_page's unscaled original on the left of a bilingual page.
    for link in source_page.get_links():
        payload = {key: value for key, value in link.items() if key not in ("xref", "id")}
        destination_page.insert_link(payload)


def show_original_page(destination, rectangle, source, number):
    page = source[number]
    rotation = page.rotation
    try:
        # show_pdf_page expects an unrotated source rectangle. Honour the PDF's
        # display rotation explicitly, including a non-default crop box.
        page.set_rotation(0)
        destination.show_pdf_page(rectangle, source, number, rotate=-rotation)
    finally:
        page.set_rotation(rotation)
