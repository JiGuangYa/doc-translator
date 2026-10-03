"""DOCX processing: full lxml XPath traversal of segments to ensure coverage
of tables, text boxes, headers and footers.

Core idea: copy the original file, only replace w:t text, keep all formatting.
extract and write_back traverse the same w:p elements in exactly the same
order, and match by seg_id.
"""
import shutil
from pathlib import Path

from .common import ExtractResult, Segment, WriteReport

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
        segments.append(Segment(seg_id="", text=text, context=ctx))
    return ExtractResult(segments=segments, skipped_count=skipped, warnings=warnings)


def validate_writeback(path: Path, options: dict):
    """Retain old IDs without silently repeating the old destructive run collapse."""
    from docx import Document
    from lxml import etree
    from .common import FormatAdapterError

    for paragraph, _ in _ordered_items(Document(str(path))):
        if _should_skip(paragraph) or not _para_text(paragraph).strip():
            continue
        runs = paragraph.xpath(_RUN_XPATH)
        styles = {etree.tostring(run.xpath('./w:rPr')[0], method='c14n')
                  if run.xpath('./w:rPr') else b'' for run in runs}
        if paragraph.xpath('.//w:hyperlink') or any(len(run.xpath('./w:t')) > 1 for run in runs) or len(styles) > 1:
            raise FormatAdapterError(
                "旧 MacBook Word 编号对应的段落包含链接或混合样式，旧写回方式无法保证保真。"
                "已保留当前译文，可继续导出；请使用“另译一份”进行结构化翻译和修订。")


def write_back(src_path: Path, dst_path: Path, translations: dict[str, str],
               options: dict) -> WriteReport:
    from docx import Document

    validate_writeback(src_path, options)
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
        runs = p.xpath(_RUN_XPATH)
        if not runs:
            continue
        # Pick the first run with rPr as the style carrier, else fall back to the first run
        carrier = next((r for r in runs if r.xpath("./w:rPr")), runs[0])
        t_elems = carrier.xpath("./w:t")
        t = t_elems[0]
        t.text = translation
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        # Clear the remaining w:t (keep node structure, do not affect other formatting)
        for r in runs:
            if r is carrier:
                continue
            for other_t in r.xpath("./w:t"):
                other_t.text = ""
        report.written += 1

    doc.save(str(dst_path))
    return report
