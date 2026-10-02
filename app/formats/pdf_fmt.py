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

from .common import ExtractResult, Segment, WriteReport, make_seg_id

# Line merge parameters: the next line is appended to this segment only if
# the y gap is less than 1.8x the line height and the left indent is similar.
_MERGE_GAP_FACTOR = 1.8
_MERGE_INDENT_TOL = 3.0   # left edge x0 tolerance (pt)
_MIN_FONT_SIZE = 4.0      # lower bound for adaptive shrinking (pt)
_RECT_PAD = 1.0           # padding around the write rectangle (pt)

# target_lang prefix -> PyMuPDF built-in CJK font name (includes Latin glyphs, can mix CJK and Latin)
_CJK_FONTS = {"zh": "china-s", "ja": "japan", "ko": "korea"}


def _pick_font(target_lang: str | None, text: str | None = None) -> str:
    # The user may revise a Chinese-target segment back to Latin text.
    # Built-in CJK PDF fonts use full-width Latin metrics in insert_textbox.
    if text is not None and all(ord(char) < 256 for char in text):
        return "helv"
    lang = (target_lang or "").lower()
    for prefix, font in _CJK_FONTS.items():
        if lang.startswith(prefix):
            return font
    return "helv"  # Use built-in Helvetica for non-CJK target languages


def _int_to_rgb(color) -> tuple[float, float, float]:
    """span color int (0xRRGGBB) -> 0-1 RGB tuple."""
    try:
        c = int(color)
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0)
    if c < 0:
        return (0.0, 0.0, 0.0)
    return ((c >> 16 & 255) / 255, (c >> 8 & 255) / 255, (c & 255) / 255)


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


def _build_segment(parts: list[tuple[str, list[float], list[dict]]], page_index: int) -> Segment:
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
        raw = page.get_text("dict")
        for block in raw.get("blocks", []):
            lines = block.get("lines") or []
            cur: list[tuple[str, list[float], list[dict]]] = []

            def flush():
                if cur:
                    segments.append(_build_segment(cur, pno))
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

def _prepare_fitted(page, rect: fitz.Rect, text: str, fontname: str,
                    size: float, rgb: tuple):
    """Lay out an uncommitted shape before removing any source text.

    Use the writer's actual font metrics, rather than an estimate that can
    disagree with CJK wrapping. A failed fit leaves the source untouched.
    """
    floor = min(_MIN_FONT_SIZE, size)
    candidate = float(size)
    while True:
        shape = page.new_shape()
        if shape.insert_textbox(rect, text, fontname=fontname, fontsize=candidate, color=rgb) >= 0:
            return candidate
        if candidate <= floor:
            return None
        candidate = max(floor, round(candidate * 0.9, 2))


def write_back(src_path: Path, dst_path: Path,
               translations: dict[str, str], options: dict) -> WriteReport:
    report = WriteReport()
    if not translations:
        shutil.copy2(src_path, dst_path)  # no translations -> copy as-is
        return report

    # Open the source file read-only and save out to the destination.
    # (Cannot open(dst) and then save(dst): PyMuPDF only allows incremental
    # save on an already-opened original.)
    doc = fitz.open(src_path)
    try:
        # Re-extract to build the seg_id -> positioning mapping
        # (consistent algorithm => numbering matches the pipeline)
        segments, _rotated = _load_segments(src_path)
        numbered = {seg.seg_id: seg for seg in segments}
        # Step 1: pre-check font-size fit before redaction; segments that
        # don't fit keep their original text and are neither redacted nor written.
        jobs: dict[int, list[tuple]] = {}
        for seg_id, translated in translations.items():
            seg = numbered.get(seg_id)
            if seg is None or not (translated or "").strip():
                continue
            x0, y0, x1, y1 = seg.meta["bbox"]
            rect = fitz.Rect(x0 - _RECT_PAD, y0 - _RECT_PAD, x1 + _RECT_PAD, y1 + _RECT_PAD)
            page = doc[seg.meta["page"] - 1]
            fontname = _pick_font(options.get("target_lang"), translated)
            rgb = _int_to_rgb(seg.meta.get("color"))
            size = _prepare_fitted(page, rect, translated, fontname, float(seg.meta.get("size", 11.0)), rgb)
            if size is None:
                report.overflow.append(seg_id)
                continue
            jobs.setdefault(seg.meta["page"], []).append((rect, translated, fontname, size, rgb))

        # Redaction can rebuild font resources, so insert fresh shapes using
        # the exact font/size that passed the real layout preflight.
        for pno in sorted(jobs):
            page = doc[pno - 1]
            for rect, *_ in jobs[pno]:
                page.add_redact_annot(rect)
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=0)
            for rect, text, fontname, size, rgb in jobs[pno]:
                if page.insert_textbox(rect, text, fontname=fontname, fontsize=size, color=rgb) < 0:
                    from .common import FormatAdapterError
                    raise FormatAdapterError(f"Page {pno}: PDF text layout changed during export; the previous output has been kept")
                report.written += 1

        doc.save(dst_path, deflate=True)
    finally:
        doc.close()
    return report
