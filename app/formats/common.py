"""Common contract for the format layer: Segment model, parser/writer
registry, translatability filter.

Each format module must expose two deterministic functions (write-back
re-traverses in the same order and matches by seg_id):
    extract(path: Path, options: dict) -> ExtractResult
    write_back(src_path: Path, dst_path: Path, translations: dict[str, str], options: dict) -> WriteReport
"""
from dataclasses import dataclass, field


@dataclass
class Segment:
    seg_id: str            # e.g. s000123
    text: str              # original text
    context: str = ""      # location description, e.g. "Slide 3 / table B2"
    meta: dict = field(default_factory=dict)  # format-private info (page number, bbox, etc.), for preview/write-back


@dataclass
class ExtractResult:
    segments: list[Segment]
    skipped_count: int = 0           # number of segments filtered out as untranslatable
    warnings: list[str] = field(default_factory=list)  # e.g. "Text in SmartArt on page 3 cannot be translated automatically"


@dataclass
class WriteReport:
    written: int = 0                 # number of segments successfully written
    overflow: list[str] = field(default_factory=list)     # seg_ids that did not fit and kept their original text (PDF)
    warnings: list[str] = field(default_factory=list)


# Uniform prefix: s + 6-digit index, consistent across formats
def make_seg_id(index: int) -> str:
    return f"s{index:06d}"


UNTRANSLATED_MARK = "⟪ untranslated:"


def mark_untranslated(text: str) -> str:
    return f"{UNTRANSLATED_MARK}{text} ⟫"


def is_untranslated(value) -> bool:
    """Placeholder values (markers that retain the original text after a
    batch failure) do not count as translated."""
    return bool(value) and str(value).startswith(UNTRANSLATED_MARK)


def filter_translatable(segments: list[Segment]) -> tuple[list[Segment], int]:
    """Filter out untranslatable segments. Language-direction-specific
    checks are added by the pipeline."""
    from ..utils import is_translatable
    kept, skipped = [], 0
    for seg in segments:
        ok, _reason = is_translatable(seg.text)
        if ok:
            kept.append(seg)
        else:
            skipped += 1
    return kept, skipped


def dedupe_segments(segments: list[Segment]) -> tuple[list[Segment], dict[str, list[str]]]:
    """Deduplicate identical text: returns the unique segment list and a
    {representative seg_id: [other seg_ids]} mapping."""
    unique: list[Segment] = []
    alias: dict[str, list[str]] = {}
    seen: dict[str, str] = {}
    for seg in segments:
        key = seg.text.strip()
        if key in seen:
            alias.setdefault(seen[key], []).append(seg.seg_id)
        else:
            seen[key] = seg.seg_id
            unique.append(seg)
    return unique, alias


# ---------- registry ----------

_REGISTRY: dict = {}


def get_format_handler(ext: str):
    """Return the module for a given extension (including the dot, lowercased);
    returns None if unsupported. Lazy-imported to avoid spurious dependency errors."""
    ext = ext.lower()
    if ext not in _REGISTRY:
        try:
            if ext == ".docx":
                from . import docx_fmt as mod
            elif ext == ".pptx":
                from . import pptx_fmt as mod
            elif ext == ".xlsx":
                from . import xlsx_fmt as mod
            elif ext == ".pdf":
                from . import pdf_fmt as mod
            else:
                return None
        except ImportError as e:
            raise RuntimeError(f"Missing dependency, cannot handle {ext} files: {e}") from e
        _REGISTRY[ext] = mod
    return _REGISTRY[ext]


SUPPORTED_EXTENSIONS = {".docx", ".pptx", ".xlsx", ".pdf"}
