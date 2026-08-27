"""XLSX handling: openpyxl reads/writes cell text strings; formulas/numbers/dates are preserved.

Note: openpyxl loses charts and images when rewriting the workbook — an accepted tradeoff
(we surface a warning when the translation is written).
extract and write_back iterate the same set of translatable cells in sheet/row/column order
and match them by seg_id.
"""
import shutil
from pathlib import Path

from .common import ExtractResult, Segment, WriteReport


def _is_skippable(cell) -> bool:
    """Skip formulas, non-strings (numbers, dates, booleans)."""
    if cell.data_type == "f":
        return True
    if not isinstance(cell.value, str):
        return True
    return cell.value.startswith("=")


def _iter_translatable_cells(wb) -> tuple[list, int]:
    """Return [(cell, context)] in iteration order. Only visible sheets; untranslatable
    strings are counted as skipped."""
    from ..utils import is_translatable

    items: list = []
    skipped = 0
    for ws in wb.worksheets:
        if ws.sheet_state != "visible":
            continue
        for row in ws.iter_rows():
            for cell in row:
                if _is_skippable(cell):
                    continue
                ok, _reason = is_translatable(cell.value)
                if not ok:
                    skipped += 1
                    continue
                items.append((cell, f"{ws.title}!{cell.coordinate}"))
    return items, skipped


def extract(path: Path, options: dict) -> ExtractResult:
    import openpyxl

    wb = openpyxl.load_workbook(str(path), keep_links=False)
    cells, skipped = _iter_translatable_cells(wb)
    segments = [Segment(seg_id="", text=cell.value, context=ctx) for cell, ctx in cells]
    return ExtractResult(segments=segments, skipped_count=skipped, warnings=[])


def write_back(src_path: Path, dst_path: Path, translations: dict[str, str],
               options: dict) -> WriteReport:
    import openpyxl

    shutil.copy2(src_path, dst_path)
    wb = openpyxl.load_workbook(str(dst_path), keep_links=False)
    report = WriteReport()
    cells, _skipped = _iter_translatable_cells(wb)
    index = -1
    wrote = False
    for cell, ctx in cells:  # noqa: F841 ctx is unused; iteration order matches extract
        index += 1
        translation = translations.get(f"s{index:06d}")
        if not translation:
            continue
        cell.value = translation
        report.written += 1
        wrote = True
    if wrote:
        report.warnings.append("xlsx write-back is based on openpyxl; embedded objects (charts, images, etc.) will not be preserved")
    wb.save(str(dst_path))
    return report
