"""DOCX processing: full lxml XPath traversal of segments to ensure coverage
of tables, text boxes, headers and footers.

Core idea: copy the original file, only replace w:t text, keep all formatting.
extract and write_back traverse the same w:p elements in exactly the same
order, and match by seg_id.
"""
import shutil
from pathlib import Path

from .common import ExtractResult, FormatAdapterError, Segment, WriteReport

# python-docx's oxml elements provide a pre-registered-namespace xpath method
# (but without the mc prefix; mc needs the Clark-notation full name), no need
# to pass nsmap manually.
_MC_NS = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"


def _para_context(p_elem) -> str:
    """Infer a location description from ancestor elements."""
    if p_elem.xpath("ancestor::w:txbxContent"):
        return "Text box"
    if p_elem.xpath("ancestor::w:pict"):
        return "Text box"
    if p_elem.xpath("ancestor::w:tbl"):
        return "Table"
    return ""


def _iter_body_paragraphs(doc):
    """Return all w:p elements inside the body in document order (naturally
    covering table cells, nested tables, and SDT).

    Deduplication: each element is only taken once; segments inside
    mc:Fallback are skipped (the AlternateContent compatibility copy
    duplicates the mc:Choice content).
    """
    seen: set[int] = set()
    out = []
    for p in doc.element.body.xpath(".//w:p"):
        key = id(p)
        if key in seen:
            continue
        seen.add(key)
        if any(anc.tag == _MC_NS + "Fallback" for anc in p.iterancestors()):
            continue
        out.append(p)
    return out


def _iter_hf_paragraphs(doc, seen_parts: set[int]):
    """Iterate paragraphs in headers/footers (default/first/even) in section
    order, deduplicating across sections."""
    for section in doc.sections:
        items = [
            ("Header", section.header), ("Header", section.first_page_header),
            ("Header", section.even_page_header),
            ("Footer", section.footer), ("Footer", section.first_page_footer),
            ("Footer", section.even_page_footer),
        ]
        for kind, hf in items:
            try:
                if hf.is_linked_to_previous:
                    continue  # no independent part, content inherits from previous section to avoid duplicate extraction
                el = hf._element
            except Exception:
                continue
            if id(el) in seen_parts:
                continue
            seen_parts.add(id(el))
            for p in el.xpath(".//w:p"):
                yield p, kind


def _should_skip(p_elem) -> bool:
    """Field code paragraphs and TOC paragraphs are not translated."""
    if p_elem.xpath(".//w:instrText"):
        return True
    styles = p_elem.xpath("./w:pPr/w:pStyle/@w:val")
    return bool(styles and str(styles[0]).upper().startswith("TOC"))


# Runs that directly belong to this paragraph (exclude runs from nested
# paragraphs inside text boxes, to avoid double-counting / mistakenly clearing them).
_RUN_XPATH = ".//w:r[w:t][count(ancestor::w:p)=1]"


def _para_text(p_elem) -> str:
    """Concatenate the text of all w:t-bearing runs in the paragraph
    (including runs inside hyperlinks)."""
    parts = []
    for t in p_elem.xpath(_RUN_XPATH + "/w:t"):
        parts.append(t.text or "")
    return "".join(parts)


def _ordered_items(doc):
    """Unified traversal entry: (w:p element, context) list, shared by
    extract/write_back to guarantee consistent ordering."""
    items = [(p, _para_context(p)) for p in _iter_body_paragraphs(doc)]
    seen_parts: set[int] = set()
    items.extend(_iter_hf_paragraphs(doc, seen_parts))
    return items


def extract(path: Path, options: dict) -> ExtractResult:
    if options.get("docx_version", 1) not in (1, 2, 3):
        raise FormatAdapterError("This Word task requires a newer app version")
    if options.get("docx_version") == 3:
        from . import docx_macbook
        return docx_macbook.extract(path, options)
    if options.get("docx_version", 1) == 2:
        from . import docx_structured
        return docx_structured.extract(path)
    from docx import Document

    doc = Document(str(path))
    segments: list[Segment] = []
    skipped = 0
    warnings: list[str] = []
    for p, ctx in _ordered_items(doc):
        if _should_skip(p):
            skipped += 1
            continue
        text = _para_text(p)
        if not text.strip():
            continue  # empty paragraphs are not included in the result
        seg_id = f"s{len(segments):06d}"
        has_hyperlink = bool(p.xpath(".//w:hyperlink"))
        if has_hyperlink:
            warnings.append(
                f"Paragraph {seg_id} contains a hyperlink; its original text will be kept "
                "to preserve the clickable link")
        elif len(p.xpath(_RUN_XPATH)) > 1:
            from lxml import etree
            styles = {etree.tostring(r.xpath("./w:rPr")[0]) if r.xpath("./w:rPr") else b""
                      for r in p.xpath(_RUN_XPATH)}
            if len(styles) > 1:
                warnings.append(f"Paragraph {seg_id} has mixed inline styles; review its translated formatting")
        segments.append(Segment(seg_id="", text=text, context=ctx,
                                meta={"skip_translation": has_hyperlink} if has_hyperlink else {}))
    return ExtractResult(segments=segments, skipped_count=skipped, warnings=warnings)


def validate_writeback(path: Path, options: dict):
    if options.get("docx_version", 1) not in (1, 2, 3):
        raise FormatAdapterError("This Word task requires a newer app version")
    if options.get("docx_version", 1) == 2 and not options.get("no_translation"):
        from . import docx_structured
        docx_structured.extract(path)


def write_back(src_path: Path, dst_path: Path, translations: dict[str, str],
               options: dict) -> WriteReport:
    if options.get("docx_version", 1) not in (1, 2, 3):
        raise FormatAdapterError("This Word task requires a newer app version")
    if options.get("docx_version") == 3:
        from . import docx_macbook
        return docx_macbook.write_back(src_path, dst_path, translations, options)
    if options.get("docx_version", 1) == 2:
        from . import docx_structured
        return docx_structured.write_back(src_path, dst_path, translations)
    from docx import Document

    shutil.copy2(src_path, dst_path)
    doc = Document(str(dst_path))
    report = WriteReport()

    # Same filtering logic as extract, to guarantee one-to-one index correspondence
    index = -1
    for p, ctx in _ordered_items(doc):  # noqa: F841 ctx only used to keep the structure consistent with extract
        if _should_skip(p):
            continue
        text = _para_text(p)
        if not text.strip():
            continue
        index += 1
        seg_id = f"s{index:06d}"
        translation = translations.get(seg_id)
        if not translation:
            continue
        if translation == text:
            report.written += 1
            continue  # preserve every run, hyperlink, and style byte-for-byte
        if p.xpath(".//w:hyperlink"):
            report.warnings.append(
                f"Paragraph {seg_id} kept its original text to preserve a clickable hyperlink")
            continue
        runs = p.xpath(_RUN_XPATH)
        if not runs:
            continue
        # Prefer an unstyled run so a translated sentence does not inherit
        # bold/italic formatting from a short fragment at the start.
        carrier = next((r for r in runs if not r.xpath("./w:rPr")), runs[0])
        t_elems = carrier.xpath("./w:t")
        t = t_elems[0]
        t.text = translation
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        for other_t in t_elems[1:]:
            other_t.text = ""
        # Clear the remaining w:t (keep node structure, do not affect other formatting)
        for r in runs:
            if r is carrier:
                continue
            for other_t in r.xpath("./w:t"):
                other_t.text = ""
        report.written += 1

    doc.save(str(dst_path))
    return report
