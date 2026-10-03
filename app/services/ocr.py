"""Durable page-by-page OCR, including mixed text-layer and scanned documents."""

import re
import time
import unicodedata

from .. import store
from ..formats import common, pdf_fmt
from ..utils import is_translatable

OPTIONAL_WARNING = "PDF images may contain additional text; use local OCR to include it"


class OCRRevisionConflict(RuntimeError):
    pass


def required_pages(job):
    value = job.get("ocr_required_pages")
    if value is not None:
        return value
    return list(range(1, int(job.get("ocr_pages") or 0) + 1)) if job.get("ocr_scanned") else []


def completed_pages(job):
    value = job.get("ocr_completed_pages")
    if value is not None:
        return value
    return required_pages(job) if job.get("status") != "ocr_pending" else []


def initialize(job, source, *, force=False):
    plan = pdf_fmt.inspect_ocr_pages(source)
    job["ocr_pages"] = plan["pages"]
    job["ocr_optional_pages"] = plan["optional"]
    if any(not segment.get("meta", {}).get("normalized_bbox") and segment.get("meta", {}).get("bbox")
           and not segment.get("meta", {}).get("ocr") for segment in job.get("segments", [])):
        import pymupdf as fitz
        with fitz.open(source) as document:
            current_segments = {segment.seg_id: segment for segment in pdf_fmt.extract(source, {}).segments}
            for segment in job.get("segments", []):
                meta = segment.get("meta") or {}
                if not meta.get("normalized_bbox") and meta.get("bbox") and not meta.get("ocr") and meta.get("page"):
                    meta["normalized_bbox"] = pdf_fmt.normalized_box(document[meta["page"] - 1], meta["bbox"])
                    if segment["seg_id"] in current_segments:
                        meta["normalized_span_bboxes"] = current_segments[segment["seg_id"]].meta["normalized_span_bboxes"]
    requested = plan["required"]
    if force:
        requested = sorted(set(plan["required"] + plan["optional"])) or list(range(1, plan["pages"] + 1))
    if not job.get("segments") and not requested:
        requested = list(range(1, plan["pages"] + 1))
    if requested:
        job.update(ocr_scanned=True, ocr_required_pages=requested,
                   ocr_completed_pages=[], ocr_empty_pages=[], ocr_dismissed_pages=[], status="ocr_pending")
    refresh(job)


def refresh(job):
    segments = job.get("segments") or []
    job["segment_count"] = sum(bool(segment.get("translatable")) for segment in segments)
    job["skipped_count"] = len(segments) - job["segment_count"]
    job["total_chars"] = sum(len(segment["text"]) for segment in segments if segment.get("translatable"))
    warnings = [warning for warning in job.get("source_warnings", []) if warning != OPTIONAL_WARNING]
    completed = set(completed_pages(job))
    if set(job.get("ocr_optional_pages") or []) - completed:
        warnings.append(OPTIONAL_WARNING)
    low = sum((segment.get("meta", {}).get("confidence", 1) < 0.75 or
               bool(segment.get("meta", {}).get("needs_review"))) and
              not segment.get("meta", {}).get("reviewed") for segment in segments)
    if low:
        warnings.append(f"Review {low} OCR lines (low-confidence or overlapping text) before translation")
    empty = set(job.get("ocr_empty_pages") or []) - set(job.get("ocr_dismissed_pages") or [])
    job["ocr_review_count"] = low + len(empty)
    if empty:
        warnings.append("No additional text was recognized on pages " + ", ".join(map(str, sorted(empty))) +
                        "; add the missing text or confirm these pages have no text to translate")
    if job.get("ocr_scanned"):
        warnings.append("Local OCR document: the export keeps original pages beside their translation")
        job["status"] = "ocr_pending" if set(required_pages(job)) - completed else "pending_confirm"
    job["warnings"] = list(dict.fromkeys(warnings))


def _bump(job):
    job["revision"] = int(job.get("revision", 0)) + 1
    job["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


def _editable(job):
    if not job or not job.get("ocr_scanned") or job.get("status") not in ("ocr_pending", "pending_confirm"):
        raise ValueError("OCR text can only be changed before translation starts")


def _normalize(text):
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _native_overlap(line, segment):
    x0, y0, x1, y1 = line["bbox"]
    area = max(1e-12, (x1 - x0) * (y1 - y0))
    meta = segment.get("meta") or {}
    boxes = meta.get("normalized_span_bboxes") or ([meta["normalized_bbox"]] if meta.get("normalized_bbox") else [])
    return max((max(0, min(x1, box[2]) - max(x0, box[0])) *
                max(0, min(y1, box[3]) - max(y0, box[1])) / area for box in boxes), default=0)


def _already_in_text_layer(line, native_segments):
    text = _normalize(line["text"])
    for segment in native_segments:
        overlap = _native_overlap(line, segment)
        if overlap >= 0.8 or (overlap > 0 and text and text in _normalize(segment["text"])):
            return True
    return False


def _next_id(segments):
    numbers = [int(segment["seg_id"][1:]) for segment in segments if re.fullmatch(r"s\d+", segment["seg_id"])]
    return max(numbers, default=-1) + 1


def _sort_key(segment):
    meta = segment.get("meta") or {}
    box = meta.get("normalized_bbox") or (meta.get("bbox") if meta.get("ocr") else None)
    return (meta.get("page", 0), -round(box[3], 2) if box else 0, box[0] if box else 0)


@store.task_operation
def accept_page(task_id, page, lines):
    job = store.load_job(task_id)
    if job and page in completed_pages(job):
        return job  # Idempotent retry must not overwrite a reviewed correction.
    _editable(job)
    if page not in required_pages(job):
        raise ValueError("This page is not awaiting OCR")
    job["ocr_required_pages"] = required_pages(job)
    job["ocr_completed_pages"] = completed_pages(job)
    segments = job.setdefault("segments", [])
    native = [segment for segment in segments if segment.get("meta", {}).get("page") == page
              and not segment.get("meta", {}).get("ocr")]
    next_id = _next_id(segments)
    added = 0
    for line in lines:
        text = line["text"].strip()
        if not text or _already_in_text_layer(line, native):
            continue
        translatable, _ = is_translatable(text)
        overlap_review = any(_native_overlap(line, segment) > 0 and
                             len(_normalize(segment["text"])) >= 3 and
                             _normalize(segment["text"]) in _normalize(text) for segment in native)
        segments.append({"seg_id": common.make_seg_id(next_id), "text": text,
                         "context": f"Page {page} · OCR", "translation": None,
                         "translatable": translatable,
                         "meta": {"page": page, "bbox": line["bbox"], "normalized_bbox": line["bbox"],
                                  "confidence": float(line["confidence"]), "ocr": True,
                                  "needs_review": overlap_review}})
        next_id += 1
        added += 1
    if len(segments) > 50000:
        raise ValueError("OCR document exceeds the 50000-segment limit; split the document")
    job["ocr_completed_pages"] = sorted(set(job["ocr_completed_pages"]) | {page})
    if added == 0 and sum(len(_normalize(segment["text"])) for segment in native) < 50:
        job["ocr_empty_pages"] = sorted(set(job.get("ocr_empty_pages") or []) | {page})
    segments.sort(key=_sort_key)
    refresh(job)
    _bump(job)
    store.save_job(task_id, job)
    return job


@store.task_operation
def accept_all(task_id, lines):
    job = store.load_job(task_id)
    _editable(job)
    required = required_pages(job)
    if any(line["page"] not in required for line in lines):
        raise ValueError("Prepare these pages for OCR before submitting their text")
    for page in required:
        job = accept_page(task_id, page, [line for line in lines if line["page"] == page])
    return job


@store.task_operation
def prepare(task_id):
    job = store.load_job(task_id)
    if not job or job.get("ext") != ".pdf" or job.get("status") not in ("ocr_pending", "pending_confirm"):
        raise ValueError("Create a translation copy to recognize a document that has already started translating")
    path = store.task_dir(task_id) / "original.pdf"
    if not job.get("ocr_scanned"):
        initialize(job, path, force=True)
    else:
        completed = completed_pages(job)
        job["ocr_required_pages"] = sorted(set(required_pages(job)) | set(job.get("ocr_optional_pages") or []))
        job["ocr_completed_pages"] = completed
        refresh(job)
    _bump(job)
    store.save_job(task_id, job)
    return job


@store.task_operation
def revise(task_id, seg_id, text, expected_revision=None):
    job = store.load_job(task_id)
    _editable(job)
    if expected_revision is not None and job.get("revision", 0) != expected_revision:
        raise OCRRevisionConflict("OCR text changed after this editor opened; reload before saving")
    segment = next((segment for segment in job["segments"] if segment["seg_id"] == seg_id), None)
    if segment is None or not text.strip():
        raise ValueError("OCR segment not found or replacement text is empty")
    segment["text"] = text.strip()
    segment["translatable"] = is_translatable(segment["text"])[0]
    segment.setdefault("meta", {})["reviewed"] = True
    refresh(job)
    _bump(job)
    store.save_job(task_id, job)


@store.task_operation
def confirm_empty(task_id, page):
    job = store.load_job(task_id)
    _editable(job)
    if page not in (job.get("ocr_empty_pages") or []):
        raise ValueError("This page does not need an empty-page review")
    if page in (job.get("ocr_dismissed_pages") or []):
        return job
    job["ocr_dismissed_pages"] = sorted(set(job.get("ocr_dismissed_pages") or []) | {page})
    refresh(job)
    _bump(job)
    store.save_job(task_id, job)
    return job


@store.task_operation
def add_page_text(task_id, page, text):
    job = store.load_job(task_id)
    _editable(job)
    if page not in completed_pages(job) or not text.strip():
        raise ValueError("Recognize this page first, then supply non-empty corrected text")
    segments = job.setdefault("segments", [])
    if any(segment.get("meta", {}).get("page") == page and segment.get("meta", {}).get("manual")
           and segment["text"] == text.strip() for segment in segments):
        return job
    if len(segments) >= 50000:
        raise ValueError("OCR document exceeds the 50000-segment limit; split the document")
    segments.append({"seg_id": common.make_seg_id(_next_id(segments)), "text": text.strip(),
                     "translation": None, "context": f"Page {page} · Manual OCR",
                     "translatable": is_translatable(text)[0],
                     "meta": {"page": page, "bbox": [0, 0, 1, 1], "normalized_bbox": [0, 0, 1, 1],
                              "confidence": 1, "ocr": True, "reviewed": True, "manual": True}})
    job["ocr_empty_pages"] = [value for value in job.get("ocr_empty_pages", []) if value != page]
    segments.sort(key=_sort_key)
    refresh(job)
    _bump(job)
    store.save_job(task_id, job)
    return job
