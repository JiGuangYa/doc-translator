"""PPTX processing: recursively walks the shape tree to extract text;
preserves run styles on write-back.

extract and write_back recursively traverse in the same order (slide ->
shape tree -> paragraphs), matching by seg_id. Text inside charts/SmartArt
cannot be safely rewritten and is skipped with a warning.
"""
import shutil
import json
import subprocess
from pathlib import Path

from .common import ExtractResult, Segment, WriteReport

# target language -> OOXML lang attribute value
_LANG_MAP = {
    "en": "en-US", "zh": "zh-CN", "zh-tw": "zh-TW",
    "ja": "ja-JP", "ko": "ko-KR", "th": "th-TH",
    "fr": "fr-FR", "de": "de-DE", "es": "es-ES", "ru": "ru-RU",
}


def _xml_lang(target_lang: str | None) -> str:
    if not target_lang:
        return "zh-CN"
    return _LANG_MAP.get(str(target_lang).lower(), str(target_lang))


def _is_graphic_special(shape) -> bool:
    """Charts or SmartArt (with diagramData) inside a GraphicFrame cannot
    be translated automatically.

    Note: tables are also GraphicFrame, so they must be excluded first.
    """
    if getattr(shape, "has_table", False):
        return False
    try:
        el = shape._element
    except Exception:
        return False
    if el.find(".//{http://schemas.openxmlformats.org/drawingml/2006/main}graphic") is None:
        return False
    if el.find(".//{http://schemas.openxmlformats.org/drawingml/2006/chart}chart") is not None:
        return True
    # SmartArt: drawingData reference (dgm namespace)
    if el.find(".//{http://schemas.openxmlformats.org/drawingml/2006/diagram}relIds") is not None:
        return True
    return False


def _walk(shapes, ctx: str, segments: list[Segment], special_count: list[int]):
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in shapes:
        if _is_graphic_special(shape):
            special_count[0] += 1
            continue
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            _walk(shape.shapes, f"{ctx} / group shape", segments, special_count)
            continue
        if shape.has_text_frame:
            for para in shape.text_frame.paragraphs:
                text = para.text or ""
                if text.strip():
                    segments.append(Segment(seg_id="", text=text, context=ctx))
        if getattr(shape, "has_table", False):
            table = shape.table
            for r, row in enumerate(table.rows, 1):
                for c, cell in enumerate(row.cells, 1):
                    for para in cell.text_frame.paragraphs:
                        text = para.text or ""
                        if text.strip():
                            segments.append(Segment(
                                seg_id="", text=text,
                                context=f"{ctx} / table R{r}C{c}"))


def extract(path: Path, options: dict) -> ExtractResult:
    from pptx import Presentation

    prs = Presentation(str(path))
    segments: list[Segment] = []
    special_count = [0]
    for slide_idx, slide in enumerate(prs.slides, 1):
        ctx = f"Slide {slide_idx}"
        _walk(slide.shapes, ctx, segments, special_count)
        if options.get("translate_notes", True) and slide.has_notes_slide:
            notes_tf = slide.notes_slide.notes_text_frame
            for para in notes_tf.paragraphs:
                text = para.text or ""
                if text.strip():
                    segments.append(Segment(
                        seg_id="", text=text, context=f"Notes Slide {slide_idx}"))
    warnings = []
    if special_count[0]:
        warnings.append(f"Detected {special_count[0]} chart/SmartArt occurrences whose text cannot be translated automatically")
    return ExtractResult(segments=segments, skipped_count=0, warnings=warnings)


def _write_translation(para, carrier, translation: str) -> None:
    """Write the translation into the carrier run, and reconstruct <a:br/>
    for soft line breaks.

    \\x0b cannot be written directly into a:t (OOXML save escapes it as
    _x000B_, corrupting the text); it must be split and expressed as
    <a:br/> elements (which are restored to \\x0b on re-read). \\n is a
    legal character and can be written as-is. Existing a:br elements in
    the paragraph are also removed to avoid duplication.
    """
    from copy import deepcopy

    from pptx.oxml.ns import qn

    parts = translation.split("\x0b")
    # Remove soft line breaks left over from old content
    for br in para._p.findall(qn("a:br")):
        para._p.remove(br)
    carrier.text = parts[0]
    anchor = carrier._r
    for part in parts[1:]:
        br = para._p.makeelement(qn("a:br"), {})
        anchor.addnext(br)
        anchor = br
        new_r = deepcopy(carrier._r)  # copy style
        new_r.find(qn("a:t")).text = part
        anchor.addnext(new_r)
        anchor = new_r


def write_back(src_path: Path, dst_path: Path, translations: dict[str, str],
               options: dict) -> WriteReport:
    """Same traversal order as extract; write translations paragraph by paragraph.

    To reuse the traversal logic, this method walks the tree again, but uses
    (paragraph index + write callback) instead of collection.
    """
    from pptx import Presentation

    shutil.copy2(src_path, dst_path)
    prs = Presentation(str(dst_path))
    report = WriteReport()
    lang = _xml_lang(options.get("target_lang"))
    index = -1

    def write_para(para) -> bool:
        """Write the translation into the style carrier run of this paragraph;
        return False when there is no run at all."""
        nonlocal index
        index += 1
        translation = translations.get(f"s{index:06d}")
        if not translation:
            return True  # no translation -> keep original, not a failure
        if translation == para.text:
            report.written += 1
            return True
        runs = list(para.runs)
        if not runs:
            report.warnings.append(
                f"Paragraph s{index:06d} ({para.text[:20]}...) has no run; skipped write")
            return True
        carrier = next((r for r in runs
                        if r._r.find("{http://schemas.openxmlformats.org/drawingml/2006/main}rPr") is not None),
                       runs[0])
        _write_translation(para, carrier, translation)
        carrier._r.get_or_add_rPr().set("lang", lang)
        for r in runs:
            if r is not carrier:
                r.text = ""
        report.written += 1
        return True

    def walk(shapes):
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        for shape in shapes:
            if _is_graphic_special(shape):
                continue
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                walk(shape.shapes)
                continue
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    if (para.text or "").strip():
                        write_para(para)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    for cell in row.cells:
                        for para in cell.text_frame.paragraphs:
                            if (para.text or "").strip():
                                write_para(para)

    for slide_idx, slide in enumerate(prs.slides, 1):
        walk(slide.shapes)
        if options.get("translate_notes", True) and slide.has_notes_slide:
            for para in slide.notes_slide.notes_text_frame.paragraphs:
                if (para.text or "").strip():
                    write_para(para)

    prs.save(str(dst_path))
    from ..services.renderer import genoffice_path
    cli = genoffice_path()
    if cli and report.written:
        try:
            audit = subprocess.run(
                [cli, "slides", "audit", str(dst_path), "--json"],
                capture_output=True, text=True, timeout=90)
            result = json.loads(audit.stdout.strip().splitlines()[-1])
            if audit.returncode == 0 and result.get("status") == "ok":
                for issue in result.get("detail", {}).get("issues", []):
                    if issue.get("code", "").startswith("text_overflow"):
                        slide = int(issue.get("slide", 0)) + 1
                        report.warnings.append(
                            f"Slide {slide}: translated text may overflow its box; review in GenOffice")
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            report.warnings.append("Slide layout audit was unavailable; review the translated deck")
    return report
