"""Versioned Word text units; patch XML text without re-saving the Office package."""

import copy
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from .common import ExtractResult, FormatAdapterError, Segment, WriteReport

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
PART = re.compile(r"word/(document|header\d+|footer\d+|footnotes|endnotes)\.xml$")


@dataclass
class Unit:
    nodes: list
    text: str
    context: str
    paragraph: str
    sensitive: bool = False
    mirrors: list[list] = field(default_factory=list)


def _owner(node, tag):
    return next((parent for parent in node.iterancestors() if parent.tag == tag), None)


def _inside(node, parent):
    return any(ancestor is parent for ancestor in node.iterancestors())


def _units(root, kind: str, warnings: set[str]) -> list[Unit]:
    result = []
    for number, paragraph in enumerate(root.iter(W + "p"), 1):
        if _owner(paragraph, MC + "Fallback") is not None:
            continue
        runs = [run for run in paragraph.iter(W + "r") if _owner(run, W + "p") is paragraph]
        own_nodes = [child for run in runs for child in run]
        style = paragraph.find(W + "pPr/" + W + "pStyle")
        if (any(node.tag in (W + "instrText", W + "fldChar") for node in own_nodes) or
                any(node.tag in (W + "fldSimple", W + "ins", W + "del")
                    for node in paragraph.iterdescendants() if _owner(node, W + "p") is paragraph) or
                style is not None and style.get(W + "val", "").upper().startswith("TOC")):
            warnings.add("Word fields, tables of contents and paragraphs with tracked changes keep their original text.")
            continue
        groups = []
        current, previous_key, previous_run = [], None, None
        for run in runs:
            properties = run.find(W + "rPr")
            formatting = etree.tostring(properties, method="c14n") if properties is not None else b""
            # Wrapper identity preserves individual hyperlink/bookmark/SDT ranges.
            key = (formatting, run.getparent())
            if current and (key != previous_key or previous_run.getnext() is not run):
                groups.append(current)
                current = []
            previous_key = key
            previous_run = run
            for child in run:
                if child.tag == W + "rPr":
                    continue
                if child.tag == W + "t":
                    current.append(child)
                else:
                    if current:
                        groups.append(current)
                        current = []
                    previous_key = None  # drawings, tabs, breaks and references are fixed boundaries
        if current:
            groups.append(current)
        paragraph_text = "".join(node.text or "" for group in groups for node in group)
        context = "Text box" if _owner(paragraph, W + "txbxContent") is not None else kind
        if _owner(paragraph, W + "tc") is not None:
            context += " / Table"
        nonempty = [group for group in groups if "".join(n.text or "" for n in group).strip()]
        for group in nonempty:
            text = "".join(node.text or "" for node in group)
            role = " / Hyperlink" if _owner(group[0], W + "hyperlink") is not None else ""
            result.append(Unit(group, text, f"{context} / Paragraph {number}{role}",
                               paragraph_text, len(nonempty) > 1))
        if len(nonempty) > 1:
            warnings.add("Word inline styles and links are translated in separate fragments; review their wording in page preview.")
    return result


def _read(source: Path):
    parts, units, warnings = {}, [], set()
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        if "word/document.xml" not in names or "[Content_Types].xml" not in names:
            raise FormatAdapterError("Word package is missing its document or content types")
        if len(names) != len(set(names)):
            raise FormatAdapterError("Word package contains duplicate archive entries")
        if any(name.startswith("_xmlsignatures/") for name in names):
            raise FormatAdapterError("This Word file is digitally signed. Save an unsigned copy before translating.")
        names = sorted((name for name in names if PART.fullmatch(name)),
                       key=lambda name: (name != "word/document.xml", name))
        for name in names:
            parser = etree.XMLParser(resolve_entities=False, no_network=True)
            root = etree.fromstring(archive.read(name), parser)
            expected_root = ("document" if name == "word/document.xml" else "hdr" if "/header" in name else
                             "ftr" if "/footer" in name else "footnotes" if "/footnotes" in name else "endnotes")
            if root.tag != W + expected_root:
                raise FormatAdapterError(f"Invalid Word XML part: {name}")
            if root.getroottree().docinfo.doctype:
                raise FormatAdapterError("Word XML with a document type declaration is unsupported")
            kind = ("Header" if "/header" in name else "Footer" if "/footer" in name else
                    "Footnote" if "/footnotes" in name else "Endnote" if "/endnotes" in name else "Body")
            primary = _units(root, kind, warnings)
            # Modern Word often keeps an equivalent VML text-box fallback. Patch
            # both copies with the same translations, billing/displaying only one.
            for alternate in root.iter(MC + "AlternateContent"):
                if _owner(alternate, MC + "Fallback") is not None:
                    continue
                choices = alternate.findall(MC + "Choice")
                fallback = alternate.find(MC + "Fallback")
                if len(choices) != 1 and list(alternate.iter(W + "t")):
                    raise FormatAdapterError("Word has multiple text-box compatibility choices. Save a simplified copy before translating.")
                if fallback is None or not list(fallback.iter(W + "t")):
                    continue
                chosen = [unit for unit in primary if choices and _inside(unit.nodes[0], choices[0])]
                # Detach an analysis copy so its descendants are no longer under
                # mc:Fallback; original nodes remain the actual write targets.
                copy_root = copy.deepcopy(fallback)
                copy_root.tag = "fallback-analysis"
                fallback_units = _units(copy_root, kind, warnings)
                original_text_nodes = list(fallback.iter(W + "t"))
                copied_text_nodes = list(copy_root.iter(W + "t"))
                mapping = dict(zip(copied_text_nodes, original_text_nodes, strict=True))
                if [unit.text for unit in chosen] != [unit.text for unit in fallback_units] or len(choices) != 1:
                    raise FormatAdapterError("Word has non-equivalent text-box compatibility copies. Save a simplified copy before translating.")
                for selected, mirror in zip(chosen, fallback_units, strict=True):
                    selected.mirrors.append([mapping[node] for node in mirror.nodes])
            parts[name] = root
            units.extend((name, unit) for unit in primary)
        if any(name.startswith(("word/charts/", "word/diagrams/")) for name in archive.namelist()):
            warnings.add("Word charts and SmartArt are preserved; text inside these objects is not translated.")
    return parts, units, sorted(warnings)


def extract(source: Path) -> ExtractResult:
    _, units, warnings = _read(source)
    segments = []
    for part, unit in units:
        meta = {"docx_version": 2, "docx_part": part}
        if unit.sensitive:
            # Context-dependent fragments must not share a context-free memory hit.
            meta["context_sensitive"] = True
            position = unit.paragraph.find(unit.text)
            meta["translation_context"] = unit.paragraph[max(0, position - 160):position + len(unit.text) + 160][:480]
        segments.append(Segment("", unit.text, unit.context, meta))
    return ExtractResult(segments, warnings=warnings)


def _replace(nodes, original: str, translated: str):
    leading = re.match(r"^\s*", original).group()
    trailing = re.search(r"\s*$", original).group()
    text = translated
    if leading and not text[:1].isspace():
        text = leading + text
    if trailing and not text[-1:].isspace():
        text += trailing
    first = nodes[0]
    pieces = re.split(r"(\n|\t)", text.replace("\r\n", "\n").replace("\r", "\n"))
    first.text = pieces[0]
    first.set(XML_SPACE, "preserve")
    previous = first
    for piece in pieces[1:]:
        tag = "br" if piece == "\n" else "tab" if piece == "\t" else "t"
        node = etree.Element(W + tag)
        if tag == "t":
            node.text = piece
            node.set(XML_SPACE, "preserve")
        previous.addnext(node)
        previous = node
    for node in nodes[1:]:
        node.text = ""


def write_back(source: Path, destination: Path, translations: dict[str, str]) -> WriteReport:
    if not translations:
        shutil.copy2(source, destination)
        return WriteReport()
    parts, units, warnings = _read(source)
    report = WriteReport(warnings=warnings)
    changed = set()
    for index, (part, unit) in enumerate(units):
        translation = translations.get(f"s{index:06d}")
        if not translation:
            continue
        report.written += 1
        if translation == unit.text:
            continue
        _replace(unit.nodes, unit.text, translation)
        for mirror in unit.mirrors:
            _replace(mirror, unit.text, translation)
        changed.add(part)
    if not changed:
        shutil.copy2(source, destination)
        return report
    xml = {name: etree.tostring(parts[name], encoding="UTF-8", xml_declaration=True, standalone=True)
           for name in changed}
    with zipfile.ZipFile(source) as before, zipfile.ZipFile(destination, "w") as after:
        after.comment = before.comment
        for info in before.infolist():
            after.writestr(copy.copy(info), xml.get(info.filename, before.read(info.filename)))
    return report
