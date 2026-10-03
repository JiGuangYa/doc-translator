"""PDF format: PyMuPDF-based text layer extraction and redact + rewrite
write-back.

Extraction is line-based; adjacent lines in the same block are merged into
a segment based on spacing/indentation. On write-back, the original area is
first redacted (preserving background images and vector graphics), then the
translation is written at the original location. The font size is shrunk
adaptively to fit the rectangle; segments that cannot fit keep their
original text and are reported in overflow.
"""
import shutil
from collections import Counter
from pathlib import Path

import pymupdf as fitz

from .common import ExtractResult, FormatAdapterError, Segment, WriteReport, make_seg_id
from . import pdf_layout

# Line merge parameters: the next line is appended to this segment only if
# the y gap is less than 1.8x the line height and the left indent is similar.
_MERGE_GAP_FACTOR = 1.8
_MERGE_INDENT_TOL = 3.0   # left edge x0 tolerance (pt)
def _int_to_rgb(color) -> tuple[float, float, float]:
    """span color int (0xRRGGBB) -> 0-1 RGB tuple."""
    try:
        c = int(color)
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0)
    if c < 0:
        return (0.0, 0.0, 0.0)
    return ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255)


def normalized_box(page, bbox) -> list[float]:
    """Image-relative, bottom-left coordinates shared with Apple Vision."""
    visible = (fitz.Rect(bbox) * page.rotation_matrix) & page.rect
    width, height = page.rect.width, page.rect.height
    return [max(0, min(1, visible.x0 / width)), max(0, min(1, 1 - visible.y1 / height)),
            max(0, min(1, visible.x1 / width)), max(0, min(1, 1 - visible.y0 / height))]


def inspect_ocr_pages(path: Path) -> dict:
    """Find scan pages without treating every photograph in a text PDF as a scan."""
    required, optional = [], []
    with fitz.open(path) as document:
        for number, page in enumerate(document, 1):
            text = page.get_text().strip()
            raw = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)
            rotated = any(tuple(line.get("dir", (1.0, 0.0))) != (1.0, 0.0)
                          for block in raw.get("blocks", []) for line in block.get("lines", []))
            images = page.get_image_info()
            area = max(1, page.rect.width * page.rect.height)
            image_area = sum(max(0, (fitz.Rect(image["bbox"]) * page.rotation_matrix & page.rect).get_area())
                             for image in images)
            if images:
                optional.append(number)
            sparse = len("".join(text.split())) < 50
            if rotated or (images and (not text or image_area / area >= 0.8 or sparse and image_area / area >= 0.25)) or (
                    not text and not images and page.get_drawings()):
                required.append(number)
        return {"pages": document.page_count, "required": required, "optional": optional}


# ---------- extraction ----------

def _line_parts(line: dict) -> tuple[str, list[float], list[dict]] | None:
    """Get a line's concatenated text, bbox, and span list; returns None for
    invalid bbox."""
    spans = line.get("spans") or []
    if not spans:
        return None
    xs0 = min(s["bbox"][0] for s in spans)
    ys0 = min(s["bbox"][1] for s in spans)
    xs1 = max(s["bbox"][2] for s in spans)
    ys1 = max(s["bbox"][3] for s in spans)
    if xs1 - xs0 <= 0 or ys1 - ys0 <= 0:
        return None  # Invalid bbox is filtered here; empty text / pure symbols are handled by the caller.
    text = "".join(s.get("text", "") for s in spans).strip()
    return text, [xs0, ys0, xs1, ys1], spans


def _merge_text(parts: list[str]) -> str:
    """Cross-line concatenation: insert a space when both ends are ASCII,
    otherwise concatenate directly (CJK does not need a space)."""
    out = ""
    for t in parts:
        if out and out[-1].isascii() and not out[-1].isspace() and t[:1].isascii():
            out += " "
        out += t
    return out.strip()


def _build_segment(parts: list[tuple[str, list[float], list[dict]]], page_index: int, page) -> Segment:
    """Pack merged line groups into a Segment; meta records the information
    needed for positioning."""
    all_spans = [s for _, _, sps in parts for s in sps]
    bbox = [min(p[1][0] for p in parts), min(p[1][1] for p in parts),
            max(p[1][2] for p in parts), max(p[1][3] for p in parts)]
    sizes = [round(s.get("size", 11.0), 1) for s in all_spans]
    colors = [s.get("color", 0) for s in all_spans]
    fonts = [s.get("font", "") or "" for s in all_spans]
    return Segment(
        seg_id="",  # assigned by the caller
        text=_merge_text([p[0] for p in parts]),
        context=f"Page {page_index + 1}",
        meta={
            "page": page_index + 1,                       # 1-based
            "bbox": [round(v, 2) for v in bbox],
            "size": Counter(sizes).most_common(1)[0][0],  # mode of font sizes
            "color": Counter(colors).most_common(1)[0][0],
            "font": Counter(fonts).most_common(1)[0][0],
            "span_bboxes": [list(span["bbox"]) for span in all_spans],
            "normalized_bbox": normalized_box(page, bbox),
            "normalized_span_bboxes": [normalized_box(page, span["bbox"]) for span in all_spans],
            "lines": [{"text": text, "bbox": box, "span_bboxes": [list(span["bbox"]) for span in spans]}
                      for text, box, spans in parts],
        },
    )


def _extract_pages(doc) -> tuple[list[Segment], int]:
    """Iterate all pages to produce the segment list. Order is deterministic;
    shared by extract and write_back.

    Returns (segments, number of rotated/vertical lines skipped).
    """
    segments: list[Segment] = []
    rotated = 0

    for pno, page in enumerate(doc):
        raw = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)
        for block in raw.get("blocks", []):
            lines = block.get("lines") or []
            cur: list[tuple[str, list[float], list[dict]]] = []

            def flush():
                if cur:
                    segments.append(_build_segment(cur, pno, page))
                    cur.clear()

            for line in lines:
                parsed = _line_parts(line)
                if parsed is None:
                    continue
                text, bbox, spans = parsed
                if tuple(line.get("dir", (1.0, 0.0))) != (1.0, 0.0):
                    rotated += 1  # rotated/vertical text is skipped and only counted
                    continue
                if not cur:
                    cur.append((text, bbox, spans))
                    continue
                prev_bbox = cur[-1][1]
                size = round(spans[0].get("size", 11.0), 1)
                height = max(prev_bbox[3] - prev_bbox[1], bbox[3] - bbox[1])
                gap = bbox[1] - prev_bbox[3]
                same_indent = abs(bbox[0] - prev_bbox[0]) <= _MERGE_INDENT_TOL
                if 0 <= gap < _MERGE_GAP_FACTOR * max(height, size) and same_indent:
                    cur.append((text, bbox, spans))
                else:
                    flush()
                    cur.append((text, bbox, spans))
            flush()

    # Empty-text segments (e.g. lines that contain only whitespace) are not emitted
    return [s for s in segments if s.text], rotated


def _load_segments(src: Path) -> tuple[list[Segment], int]:
    """Extract all segments and number them per pipeline rules (enumerate
    over the full list, no filtering). Returns (segments, rotated-skip count).
    Shared by extract and write_back, to guarantee consistent numbering."""
    doc = fitz.open(src)
    try:
        has_text = any(page.get_text().strip() for page in doc)
        if not has_text:
            from .common import FormatAdapterError
            raise FormatAdapterError("This PDF has no text layer (may be a scan); translation is not supported")
        segments, rotated = _extract_pages(doc)
    finally:
        doc.close()
    for i, seg in enumerate(segments):
        seg.seg_id = make_seg_id(i)
    return segments, rotated


def extract(path: Path, options: dict) -> ExtractResult:
    segments, rotated = _load_segments(path)

    warnings = []
    if rotated:
        warnings.append(f"Skipped {rotated} rotated/vertical text occurrences")

    # Empty text / pure symbols / URLs are filtered by the caller's
    # filter_translatable, not here.
    return ExtractResult(segments=segments, skipped_count=0, warnings=warnings)


# ---------- write-back ----------

def validate_writeback(path: Path, options: dict) -> None:
    if options.get("no_translation"):
        return
    with fitz.open(path) as document:
        pdf_layout.validate_source(document, bilingual=options.get("ocr_scanned", False))


def _redaction_strips(boxes) -> list:
    strips = []
    for box in boxes:
        rect = fitz.Rect(box)
        middle = (rect.y0 + rect.y1) / 2
        strips.append(fitz.Rect(rect.x0 + 0.001, middle - 0.05, rect.x1 - 0.001, middle + 0.05))
    return strips


def _placement(segment, text):
    lines = segment.meta.get("lines") or []
    markers = [line for line in lines if line["text"].strip() in {"•", "●", "▪", "‣", "◦", "·"}]
    content = [line for line in lines if line not in markers]
    if markers and content:
        # Legacy extraction sometimes merged a following bullet with an intro
        # line. Keep that bullet at its original location, without renumbering IDs.
        for marker in markers:
            text = text.replace(marker["text"].strip(), "", 1)
        rectangle = fitz.Rect(content[0]["bbox"])
        for line in content[1:]:
            rectangle |= fitz.Rect(line["bbox"])
        boxes = [box for line in content for box in line["span_bboxes"]]
    else:
        rectangle = fitz.Rect(segment.meta["bbox"])
        boxes = segment.meta.get("span_bboxes", [segment.meta["bbox"]])
    return text.strip(), rectangle, _redaction_strips(boxes)


def write_back(src_path: Path, dst_path: Path,
               translations: dict[str, str], options: dict) -> WriteReport:
    report = WriteReport()
    if not translations:
        shutil.copy2(src_path, dst_path)
        return report
    document = fitz.open(src_path)
    try:
        pdf_layout.validate_source(document)
        segments, _ = _load_segments(src_path)
        pages = {}
        for segment in segments:
            pages.setdefault(segment.meta["page"], []).append(segment)
        for number, entries in pages.items():
            page = document[number - 1]
            original_annotations = pdf_layout.annotation_array(page)
            raw = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)
            rotated_boxes = [fitz.Rect(span["bbox"]) for block in raw.get("blocks", [])
                             for line in block.get("lines", []) if tuple(line.get("dir", (1.0, 0.0))) != (1.0, 0.0)
                             for span in line.get("spans", [])]
            plans = []
            try:
                for segment in entries:
                    text = translations.get(segment.seg_id)
                    if not text or not text.strip():
                        continue
                    if text == segment.text:
                        report.written += 1
                        continue
                    text, rectangle, strips = _placement(segment, text)
                    if not text:
                        report.overflow.append(segment.seg_id)
                        continue
                    # Prevent removal of a neighbouring text block. Narrow strips
                    # remove the intended glyphs without painting over the background.
                    other_boxes = [fitz.Rect(box) for other in entries if other.seg_id != segment.seg_id
                                   for box in other.meta.get("span_bboxes", [other.meta["bbox"]])]
                    overlaps = any(any(strip.intersects(box) for strip in strips) or
                                   rectangle.contains((box.tl + box.br) / 2) for box in other_boxes)
                    overlaps = overlaps or any(any(strip.intersects(box) for strip in strips) or
                                               rectangle.contains((box.tl + box.br) / 2)
                                               for box in rotated_boxes)
                    if overlaps:
                        report.overflow.append(segment.seg_id)
                        report.warnings.append(f"Page {number}: overlapping text kept in its original form")
                        continue
                    page_width = page.cropbox.width
                    centered = (segment.meta.get("size", 11) >= 14 and rectangle.x0 > page_width * 0.2 and
                                abs((rectangle.x0 + rectangle.x1) / 2 - page_width / 2) < page_width * 0.015)
                    fragment = pdf_layout.render_text(
                        text, rectangle.width, rectangle.height, segment.meta.get("size", 11),
                        color=_int_to_rgb(segment.meta.get("color")), font=segment.meta.get("font", ""),
                        align="center" if centered else "left")
                    if fragment is None:
                        report.overflow.append(segment.seg_id)
                        continue
                    plans.append((segment.seg_id, rectangle, strips, fragment))
                if plans:
                    for _identifier, _rectangle, strips, _fragment in plans:
                        for strip in strips:
                            page.add_redact_annot(strip, fill=False, cross_out=False)
                    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=0)
                    # Redaction removes overlapping links. The original annotation
                    # objects remain valid; restore their references and metadata.
                    document.xref_set_key(page.xref, "Annots", original_annotations)
                    for identifier, rectangle, _strips, fragment in plans:
                        try:
                            page.show_pdf_page(rectangle, fragment, 0)
                        except Exception as error:
                            raise FormatAdapterError(
                                f"Page {number}, {identifier}: PDF text could not be written safely; export stopped") from error
                        report.written += 1
            finally:
                for _identifier, _rectangle, _strips, fragment in plans:
                    fragment.close()
        document.save(dst_path, deflate=True)
    finally:
        document.close()
    return report


def write_scanned_bilingual(src_path: Path, dst_path: Path, translations: dict[str, str],
                            segments: list[dict], options: dict) -> WriteReport:
    """Keep each original page unchanged beside searchable, shaped Unicode text."""
    report = WriteReport()
    if not translations and not any(segment.get("translatable") for segment in segments):
        shutil.copy2(src_path, dst_path)
        report.warnings.append("No text requires translation; original PDF preserved")
        return report
    by_page = {}
    for segment in segments:
        by_page.setdefault(int(segment["meta"]["page"]), []).append(segment)
    source = fitz.open(src_path)
    output = fitz.open()
    try:
        pdf_layout.validate_source(source, bilingual=True)
        for page_index, original in enumerate(source):
            entries = by_page.get(page_index + 1, [])
            pieces = []
            for entry in entries:
                translated = translations.get(entry["seg_id"])
                if translated:
                    pieces.append(translated)
                    report.written += 1
                elif entry.get("translatable"):
                    pieces.append("[Untranslated] " + entry["text"])
                else:
                    pieces.append(entry["text"])
            width, original_height = float(original.rect.width), float(original.rect.height)
            fragment = None
            try:
                if pieces:
                    text_width = max(80, width - 44)
                    fragment = pdf_layout.render_text("\n\n".join(pieces), text_width, None,
                                                       10, minimum_size=10)
                    output_width = width + max(text_width, fragment[0].rect.width if fragment else 0) + 64
                    if fragment is None or output_width > 14400:
                        raise FormatAdapterError(f"Translation for page {page_index + 1} exceeds PDF export limits")
                    height = max(original_height, fragment[0].rect.height + 56)
                    page = output.new_page(width=output_width, height=height)
                else:
                    page = output.new_page(width=width, height=original_height)
                if original.read_contents().strip():
                    pdf_layout.show_original_page(page, fitz.Rect(0, 0, width, original_height), source, page_index)
                if fragment is not None:
                    page.draw_line(fitz.Point(width + 10, 20), fitz.Point(width + 10, page.rect.height - 20),
                                   color=(0.7, 0.7, 0.7))
                    right = fitz.Rect(width + 30, 28, width + 30 + fragment[0].rect.width,
                                      28 + fragment[0].rect.height)
                    page.show_pdf_page(right, fragment, 0)
            finally:
                if fragment is not None:
                    fragment.close()
        # All target pages must exist before copying internal page destinations.
        for page_index, original in enumerate(source):
            pdf_layout.copy_page_links(original, output[page_index])
        output.save(dst_path, deflate=True)
    finally:
        output.close()
        source.close()
    report.warnings.append("PDF exported as bilingual pages; original page contents were preserved")
    return report
