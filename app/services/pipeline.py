"""Core orchestration: parse -> segment -> translate -> write back."""
import shutil
import copy
import hashlib
import os
import time
import threading
import uuid
from pathlib import Path

from .. import store
from ..formats import common
from ..formats.common import FormatAdapterError
from . import ocr, output_transaction, task_manager
from .llm import translator


class OutputConflictError(RuntimeError):
    """The translated file changed outside this app since the last write."""


class RevisionConflictError(RuntimeError):
    """An editor submitted text based on an older document revision."""


def _check_revision(job: dict, expected_revision: int | None):
    if expected_revision is not None and expected_revision != job.get("revision", 0):
        raise RevisionConflictError("The translation changed after this editor was opened. "
                                    "Reload the latest text before saving; your draft was not applied.")


def _require_idle(job: dict):
    runtime = task_manager.get_runtime(job["task_id"]) or {}
    if (job.get("status") in ("translating", "queued") or
            runtime.get("status") in ("translating", "queued") or task_manager.is_active(job["task_id"])):
        raise ValueError("Task is currently translating or queued; cancel it before editing")


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_output_unchanged(job: dict) -> None:
    path = store.task_dir(job["task_id"]) / f"translated{job['ext']}"
    if not path.exists():
        return
    if job.get("external_edit"):
        raise OutputConflictError("This task uses the externally edited file. "
                                  "Export it or explicitly rebuild the app translation.")
    expected = job.get("translated_sha256")
    if expected:
        if _file_hash(path) != expected:
            raise OutputConflictError("The translated file was changed outside this app. "
                                      "Your external version remains intact; export or back it up "
                                      "before rebuilding the app translation.")
        return
    # Legacy jobs predate recorded hashes. A newer file timestamp is evidence
    # of an external edit; avoid blessing it and then overwriting it.
    try:
        updated = time.mktime(time.strptime(job.get("updated_at", ""), "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return
    if path.stat().st_mtime > updated + 2:
        raise OutputConflictError("The translated file appears to have been edited externally. "
                                  "It was not overwritten.")


@store.task_operation
def resolve_output_conflict(task_id: str, action: str, expected_revision: int | None = None) -> dict:
    """Keep an external edit or back it up before an explicit rebuild."""
    job = store.load_job(task_id)
    if not job:
        raise ValueError("Task does not exist")
    _require_idle(job)
    if job.get("migration_issue"):
        raise ValueError(job["migration_issue"])
    _check_revision(job, expected_revision)
    path = store.task_dir(task_id) / f"translated{job['ext']}"
    if not path.exists():
        raise ValueError("Translated file does not exist")
    if action == "keep_external":
        job["translated_sha256"] = _file_hash(path)
        job["external_edit"] = True
        job["revision"] = int(job.get("revision", 0)) + 1
        job["warnings"] = list(dict.fromkeys((job.get("warnings") or []) + [
            "Externally edited version is active; app paragraph revisions are disabled"]))
        store.save_job(task_id, job)
        return job
    if action != "rebuild":
        raise ValueError("action must be keep_external or rebuild")
    versions = path.parent / "versions"
    versions.mkdir(exist_ok=True)
    backup = versions / f"external-{uuid.uuid4().hex[:12]}{job['ext']}"
    shutil.copy2(path, backup)
    job["translated_sha256"] = _file_hash(path)
    job["external_edit"] = False
    store.save_job(task_id, job)
    _apply_translations(task_id, job["ext"], job.get("translations") or {})
    return store.load_job(task_id)


def _validate_output(path: Path, ext: str, source: Path | None = None) -> None:
    """Reject an incomplete or corrupt writer result before replacing an export."""
    if ext in {".docx", ".pptx", ".xlsx"}:
        import zipfile
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise FormatAdapterError("Office output contains a corrupt archive entry")
        if source and ext in {".docx", ".pptx"}:
            protected = (("word/media/", "word/embeddings/", "word/charts/",
                          "word/diagrams/", "word/comments") if ext == ".docx" else
                         ("ppt/media/", "ppt/embeddings/", "ppt/charts/",
                          "ppt/diagrams/", "ppt/slideMasters/"))
            with zipfile.ZipFile(source) as before, zipfile.ZipFile(path) as after:
                current = set(after.namelist())
                for name in before.namelist():
                    if name.startswith(protected):
                        if name not in current or before.read(name) != after.read(name):
                            raise FormatAdapterError(
                                f"Protected Office object changed or disappeared: {name}")
    elif ext == ".pdf":
        import fitz
        with fitz.open(path) as document:
            if document.page_count < 1:
                raise FormatAdapterError("PDF output has no pages")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def parse_upload(task_id: str, filename: str, src_path: Path, options: dict) -> dict:
    """Synchronously parse the uploaded file and return a job summary
    (including segment_count, etc.)."""
    ext = src_path.suffix.lower()
    options = dict(options)
    if ext == ".docx":
        options.setdefault("docx_version", 2)
    handler = common.get_format_handler(ext)
    result = handler.extract(src_path, options)

    segments = []
    for i, seg in enumerate(result.segments):
        seg.seg_id = common.make_seg_id(i)
        segments.append(seg)
    kept, skipped = common.filter_translatable(segments)
    kept_ids = {s.seg_id for s in kept}  # dataclass value comparison is slow and error-prone; partition by id

    job = {
        "task_id": task_id,
        "filename": filename,
        "ext": ext,
        "status": "pending_confirm",
        "created_at": _now(),
        "updated_at": _now(),
        "segments": [
            {"seg_id": s.seg_id, "text": s.text, "context": s.context, "meta": s.meta,
             "translatable": s.seg_id in kept_ids, "translation": None}
            for s in segments
        ],
        "segment_count": len(kept),
        "total_chars": sum(len(s.text) for s in kept),
        "skipped_count": skipped,
        "warnings": result.warnings,
        "source_warnings": result.warnings,
        "translations": {},       # incremental results for resumption
        "provider_id": None,
        "source_lang": None,
        "target_lang": None,
        "revision": 0,
        "format_options": {key: options[key] for key in ("docx_version", "translate_notes") if key in options},
    }
    # Copy the original file (the upload endpoint may have already written
    # to this path, in which case skip the self-copy)
    dst = store.task_dir(task_id) / f"original{ext}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src_path.exists() and src_path.resolve() != dst.resolve():
        shutil.copy2(src_path, dst)
    if ext == ".pdf":
        ocr.initialize(job, dst)
    store.save_job(task_id, job)
    return job


def register_scanned_pdf(task_id: str, filename: str, src_path: Path) -> dict:
    """Keep an image-only PDF until the macOS app supplies local OCR text."""
    import fitz
    with fitz.open(src_path) as document:
        pages = document.page_count
    job = {
        "task_id": task_id, "filename": filename, "ext": ".pdf",
        "status": "ocr_pending", "created_at": _now(), "updated_at": _now(),
        "segments": [], "segment_count": 0, "total_chars": 0, "skipped_count": 0,
        "warnings": ["Scanned PDF: review locally recognized text before translation"],
        "translations": {}, "provider_id": None, "source_lang": None,
        "target_lang": None, "revision": 0, "ocr_scanned": True,
        "ocr_pages": pages,
        "source_warnings": [],
    }
    destination = store.task_dir(task_id) / "original.pdf"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if src_path.resolve() != destination.resolve():
        shutil.copy2(src_path, destination)
    ocr.initialize(job, destination)
    store.save_job(task_id, job)
    return job


def accept_ocr(task_id: str, lines: list[dict]) -> dict:
    return ocr.accept_all(task_id, lines)


def revise_ocr_text(task_id: str, seg_id: str, text: str, expected_revision=None) -> None:
    ocr.revise(task_id, seg_id, text, expected_revision)


@store.task_operation
def start_translation(task_id: str, provider_id: str, source_lang: str, target_lang: str,
                      max_total_tokens: int | None = None, confirm_snapshot: bool = False) -> None:
    """Submit a translation task to the thread pool."""
    job = store.load_job(task_id)
    if not job:
        raise ValueError("Task does not exist")
    if job.get("archived"):
        raise ValueError("Restore the archived document before translating")
    if job.get("migration_issue"):
        raise ValueError(job["migration_issue"])
    source_lang, target_lang = source_lang.strip(), target_lang.strip()
    if not source_lang or not target_lang:
        raise ValueError("Source and target language must not be empty")
    has_saved = bool(job.get("translations")) or any(s.get("translation") for s in job.get("segments", []))
    if has_saved and ((job.get("source_lang") or source_lang) != source_lang or
                      (job.get("target_lang") or target_lang) != target_lang):
        raise ValueError("Create a separate translation copy to change languages; "
                         "resuming keeps the language of existing translated paragraphs.")
    if has_saved and job.get("provider_id") and job["provider_id"] != provider_id:
        raise ValueError("Resume must use the original provider; create another translation to change models")
    if job.get("requires_snapshot_confirmation") and not confirm_snapshot:
        raise ValueError("请确认旧任务的续翻模型与地址；历史配置没有完整记录")
    if job.get("status") == "ocr_pending":
        raise ValueError("Review the scanned PDF's OCR text before translating")
    if job.get("ocr_scanned"):
        empty_pages = set(job.get("ocr_empty_pages") or []) - set(job.get("ocr_dismissed_pages") or [])
        if empty_pages:
            raise ValueError("Review pages with no recognized text before translating: " +
                             ", ".join(map(str, sorted(empty_pages))))
        pending_review = sum(1 for segment in job.get("segments", [])
                             if (segment.get("meta", {}).get("confidence", 1) < 0.75 or
                                 segment.get("meta", {}).get("needs_review"))
                             and not segment.get("meta", {}).get("reviewed"))
        if pending_review:
            raise ValueError(f"Review {pending_review} low-confidence OCR lines first")
    if task_manager.is_shutdown_requested():
        raise ValueError("The translation engine is shutting down")
    rt = task_manager.get_runtime(task_id) or {}
    if job.get("status") == "translating" and rt.get("status") == "translating":
        raise ValueError("Task is currently translating; do not start it again")
    if job["status"] == "done" and not _has_untranslated(job):
        return  # already fully translated
    _assert_output_unchanged(job)
    handler = common.get_format_handler(job["ext"])
    if callable(getattr(handler, "validate_writeback", None)):
        handler.validate_writeback(store.task_dir(task_id) / f"original{job['ext']}",
                                   {**(job.get("format_options") or {}),
                                    "ocr_scanned": job.get("ocr_scanned", False),
                                    "no_translation": job.get("segment_count", 0) == 0})
    provider = store.get_provider(provider_id) or {}
    if provider_id != "mock" and not provider and job.get("segment_count", 0) > 0:
        raise ValueError("Translation model provider is not configured")

    snapshot = job.get("provider_snapshot") or {key: provider.get(key) for key in
        ("id", "name", "base_url", "model", "input_price_per_million", "output_price_per_million")}
    settings = dict(job.get("translation_settings") or store.load_settings())
    settings.setdefault("glossary", store.load_glossary())
    if "translate_notes" not in (job.get("format_options") or {}):
        job.setdefault("format_options", {})["translate_notes"] = settings.get("translate_notes", True)
    # CAS-style dedup placeholder: repeated clicks within the queueing window
    # would otherwise cause a double submission and double token burn
    if not task_manager.try_mark_submitted(task_id):
        raise ValueError("Task has already been submitted; do not start it again")

    initial_status = "translating" if task_manager.count_in_flight() == 1 else "queued"
    job.update({
        "status": initial_status,
        "provider_id": provider_id,
        "provider_snapshot": snapshot, "translation_settings": settings,
        "requires_snapshot_confirmation": False,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "token_budget": max_total_tokens if max_total_tokens is not None
                        else job.get("token_budget", 0),
        "updated_at": _now(),
    })
    job["input_price_per_million"] = snapshot.get("input_price_per_million")
    job["output_price_per_million"] = snapshot.get("output_price_per_million")
    try:
        store.save_job(task_id, job)
    except Exception:
        task_manager.mark_finished(task_id)
        raise
    # Synchronously mark as running: otherwise the runtime status lags behind
    # while the task is queued, creating a gap in the dedup check.
    task_manager.update(task_id, status=initial_status, cancel_requested=False)

    def run():
        current = store.load_job(task_id)
        if not current or current.get("status") == "cancelled":
            return
        if task_manager.is_shutdown_requested():
            if current.get("status") == "translating":
                task_manager.persist_status(task_id, status="paused")
            return
        with store.task_lock(task_id):
            current = store.load_job(task_id)
            if not current or current.get("status") == "cancelled":
                return
            task_manager.update(task_id, status="translating")
            task_manager.persist_status(task_id, status="translating", error=None)
        _run_translation(task_id, job["ext"], provider_id,
                         source_lang, target_lang, settings)

    try:
        task_manager.submit(run, task_id)
    except Exception:
        task_manager.mark_finished(task_id)
        task_manager.persist_status(task_id, status="paused", error="Could not queue translation; retry to resume")
        task_manager.update(task_id, status="paused")
        raise


@store.task_operation
def duplicate_task(task_id: str, source_lang: str, target_lang: str, provider_id: str | None,
                   token_budget: int = 0) -> dict:
    """Create an independent translation using the preserved original and OCR review."""
    from .. import utils
    original = store.load_job(task_id)
    if not original:
        raise ValueError("Task does not exist")
    new_id = utils.new_task_id()
    directory = store.task_dir(new_id)
    directory.mkdir(parents=True)
    try:
        shutil.copy2(store.task_dir(task_id) / f"original{original['ext']}",
                     directory / f"original{original['ext']}")
        job = {key: copy.deepcopy(original[key]) for key in (
            "filename", "ext", "segments", "segment_count", "total_chars", "skipped_count", "format_options",
            "ocr_scanned", "ocr_pages", "source_warnings", "ocr_required_pages",
            "ocr_completed_pages", "ocr_empty_pages", "ocr_dismissed_pages", "ocr_optional_pages") if key in original}
        if job["ext"] == ".pdf" and not job.get("ocr_scanned"):
            job = parse_upload(new_id, job["filename"], directory / "original.pdf", {})
        if job["ext"] == ".docx":
            job = parse_upload(new_id, job["filename"], directory / "original.docx", {})
        for segment in job.get("segments", []):
            segment["translation"] = None
        job.update(task_id=new_id, status="ocr_pending" if original.get("status") == "ocr_pending" or job.get("status") == "ocr_pending"
                   else "pending_confirm", created_at=_now(), updated_at=_now(),
                   translations={}, revision=0, warnings=job.get("source_warnings", original.get("warnings", [])),
                   source_lang=source_lang.strip() or "auto", target_lang=target_lang.strip() or "zh-CN",
                   provider_id=provider_id, token_budget=token_budget, copied_from=task_id)
        if job["ext"] == ".pdf":
            ocr.refresh(job)
        store.save_job(new_id, job)
        return job
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def resume_queued() -> None:
    """Re-submit queued jobs after the local service restarts."""
    for job in reversed(store.list_jobs()):
        if job.get("status") == "queued" and job.get("provider_id"):
            try:
                start_translation(job["task_id"], job["provider_id"],
                                  job.get("source_lang") or "auto",
                                  job.get("target_lang") or "zh-CN")
            except (ValueError, OutputConflictError):
                task_manager.persist_status(job["task_id"], status="paused")


def _has_untranslated(job: dict) -> bool:
    for s in _job_segments(job):
        if not s["translatable"]:
            continue
        cur = job.get("translations", {}).get(s["seg_id"], s.get("translation"))
        if not cur or common.is_untranslated(cur):
            return True
    return False


def untranslated_count(job: dict) -> int:
    """Number of segments still untranslated (missing or placeholders)."""
    n = 0
    tr = job.get("translations") or {}
    for s in job.get("segments") or []:
        if not s.get("translatable"):
            continue
        cur = tr.get(s["seg_id"], s.get("translation"))
        if not cur or common.is_untranslated(cur):
            n += 1
    return n


def _job_segments(job: dict) -> list[dict]:
    segs = job.get("segments") or []
    # Backward compat: merge the translations dict into segments
    tr = job.get("translations") or {}
    for s in segs:
        if s["seg_id"] in tr:
            s["translation"] = tr[s["seg_id"]]
    return segs


def _run_translation(task_id: str, ext: str, provider_id: str,
                     source_lang: str, target_lang: str, settings: dict) -> None:
    job = store.load_job(task_id)
    segments = [s for s in _job_segments(job) if s["translatable"]]

    from ..formats.common import Segment
    seg_objs = [Segment(seg_id=s["seg_id"], text=s["text"], context=s.get("context", ""),
                        meta=s.get("meta", {})) for s in segments]
    # Resume starting point: existing translations; placeholder segments
    # ("⟪untranslated⟫") are treated as untranslated and re-submitted.
    existing = {s["seg_id"]: s["translation"] for s in segments
                if s.get("translation") and not common.is_untranslated(s["translation"])}
    if settings.get("use_translation_memory", True) and provider_id != "mock":
        try:
            from . import memory
            hits = memory.lookup_many(source_lang, target_lang,
                                      [segment.text for segment in seg_objs if not segment.meta.get("context_sensitive")], provider_id)
            for segment in seg_objs:
                if segment.seg_id not in existing and segment.text in hits and not segment.meta.get("context_sensitive"):
                    existing[segment.seg_id] = hits[segment.text]
        except Exception:
            pass  # translation memory is an optimization, never a prerequisite
    task_manager.update(task_id, done_segments=len(existing), total_segments=len(seg_objs))

    done = [0]
    token_budget = int(job.get("token_budget") or 0)
    saved_usage = job.get("usage") or {}
    used_tokens = [saved_usage.get("prompt_tokens", 0) +
                   saved_usage.get("completion_tokens", 0)]
    usage_lock = threading.Lock()
    if token_budget > 0:
        settings = dict(settings)
        settings["concurrency_batches"] = 1  # bound overshoot to one in-flight batch

    def on_progress(d: int, total: int, snapshot: dict | None = None):
        done[0] = d
        task_manager.update(task_id, done_segments=d, total_segments=total)
        # Persist every completed batch. A fast first batch followed by a
        # stalled request must survive a forced quit, too.
        if snapshot:
            def mut(j: dict):
                j_trans = j.get("translations") or {}
                j_trans.update(snapshot)
                j["translations"] = j_trans
                j["done_segments"] = max(j.get("done_segments", 0), d)
            store.update_job(task_id, mut)

    def on_usage(prompt_tokens: int, completion_tokens: int):
        with usage_lock:
            used_tokens[0] += prompt_tokens + completion_tokens
        def mut(j: dict):
            usage = j.setdefault("usage", {"prompt_tokens": 0, "completion_tokens": 0})
            usage["prompt_tokens"] += prompt_tokens
            usage["completion_tokens"] += completion_tokens
        try:
            store.update_job(task_id, mut)
        except OSError:
            pass  # the in-memory count still enforces the budget

    def on_parts(parts: dict):
        def update(job):
            job.setdefault("translation_parts", {}).update(parts)
        store.update_job(task_id, update)

    def budget_reached() -> bool:
        if token_budget <= 0:
            return False
        with usage_lock:
            return used_tokens[0] >= token_budget

    translations: dict[str, str] = dict(existing)
    try:
        if provider_id == "mock":
            translations = _mock_translate(seg_objs, existing, on_progress)
        else:
            translations = translator.translate_segments(
                seg_objs, provider_id, source_lang, target_lang, settings,
                existing=existing, progress_cb=on_progress,
                cancel_check=lambda: task_manager.is_cancel_requested(task_id) or budget_reached(),
                usage_cb=on_usage, existing_parts=job.get("translation_parts"),
                checkpoint_cb=on_parts, provider_snapshot=job.get("provider_snapshot"))
    except translator.TranslationCancelled as e:
        # Cancellation must not lose work: merge any batch results that were
        # already complete at the time of cancellation, then persist.
        partial = getattr(e, "partial", None) or {}
        if partial:
            translations.update(partial)
        reached_budget = budget_reached()
        stopped_status = ("paused" if task_manager.is_shutdown_requested() or reached_budget
                          else "cancelled")
        error = "Token budget reached; increase it and resume" if reached_budget else None
        task_manager.persist_status(task_id, status=stopped_status, error=error,
                                    updated_at=_now())
        task_manager.update(task_id, status=stopped_status)
        _checkpoint_translations(task_id, translations)
        _apply_translations(task_id, ext, translations)
        return
    except FormatAdapterError as e:
        # Adapter-level failure (corrupt file, missing native dep): this is
        # not a transient outage and resume won't help. Mark as failed with
        # a precise, user-readable message so the UI can show it verbatim.
        msg = str(e)[:500]
        task_manager.persist_status(task_id, status="failed", error=msg, updated_at=_now())
        task_manager.update(task_id, status="failed", error=msg)
        return
    except Exception as e:
        msg = str(e)[:500]
        # Preserve any completed partial results for resume.
        partial = getattr(e, "partial", None)
        if isinstance(partial, dict) and partial:
            job = store.load_job(task_id)
            if job is not None:
                job_trans = job.get("translations") or {}
                job_trans.update(partial)
                _checkpoint_translations(task_id, job_trans)
        task_manager.persist_status(task_id, status="failed", error=msg, updated_at=_now())
        task_manager.update(task_id, status="failed", error=msg)
        return

    _checkpoint_translations(task_id, translations)
    report = _apply_translations(task_id, ext, translations)
    status = "done"
    warnings = list(store.load_job(task_id).get("warnings") or [])
    if report.warnings:
        warnings += report.warnings
    # Persist untranslated count: "complete" != "everything translated";
    # the UI uses this to offer a "Resume translation" option.
    job_now = store.load_job(task_id)
    un_count = untranslated_count(job_now)
    warnings = [w for w in warnings if not w.startswith("Still has ")]
    if un_count:
        warnings.append(f"Still has {un_count} segments that could not be translated (model returned nothing or batch failed); click 'Resume translation' to fill the gap")
    job_now["untranslated_count"] = un_count
    store.save_job(task_id, job_now)
    task_manager.persist_status(
        task_id, status=status, error=None, updated_at=_now(), warnings=warnings,
        written=report.written, overflow=report.overflow)
    task_manager.update(task_id, status="done")
    # After translation completes, auto-generate high-fidelity page images
    # (LibreOffice) for the side-by-side view.
    if ext != ".pdf":
        from . import renderer
        renderer.auto_render_after_done(task_id, ext)


def _mock_translate(seg_objs, existing, on_progress) -> dict[str, str]:
    out = dict(existing)
    for seg in seg_objs:
        if seg.seg_id not in out:
            out[seg.seg_id] = translator.mock_translate_text(seg.text)
            on_progress(1, len(seg_objs), dict(out))
    on_progress(len(seg_objs), len(seg_objs), dict(out))
    return out


def _checkpoint_translations(task_id: str, translations: dict[str, str]):
    # The completed model response must survive a format writer or disk failure.
    def update(job):
        job["translations"] = dict(translations)
        for segment in job.get("segments", []):
            segment["translation"] = translations.get(segment["seg_id"])
        job["done_segments"] = sum(bool(value) and not common.is_untranslated(value)
                                   for value in translations.values())
        complete = {key for key, value in translations.items()
                    if value and not common.is_untranslated(value)}
        job["translation_parts"] = {key: value for key, value in job.get("translation_parts", {}).items()
                                    if key.split("#p", 1)[0] not in complete}
    saved = store.update_job(task_id, update)
    if saved:
        task_manager.update(task_id, done_segments=saved["done_segments"],
                            total_segments=saved.get("segment_count", 0))


@store.task_operation
def _apply_translations(task_id: str, ext: str, translations: dict[str, str]) -> "common.WriteReport":
    """Invoke the format writer to write the translated file and store the
    translations dict in job.json."""
    from ..formats import common as fmt_common
    tdir = store.task_dir(task_id)
    job = store.load_job(task_id)

    clean = {k: v for k, v in translations.items() if v and not common.is_untranslated(v)}
    src = tdir / f"original{ext}"
    dst = tdir / f"translated{ext}"
    _assert_output_unchanged(job)
    previous_hash = output_transaction.file_hash(dst)
    pending = tdir / f".translated-{uuid.uuid4().hex}{ext}"
    options = {"target_lang": job.get("target_lang"), "translate_notes": store.load_settings().get("translate_notes", True)}
    options.update(job.get("format_options") or {})
    handler = fmt_common.get_format_handler(ext)
    try:
        if job.get("ocr_scanned"):
            from ..formats import pdf_fmt
            report = pdf_fmt.write_scanned_bilingual(src, pending, clean,
                                                     job.get("segments") or [], options)
        else:
            report = handler.write_back(src, pending, clean, options)
        _validate_output(pending, ext, src)
        with pending.open("r+b") as handle:
            os.fsync(handle.fileno())
        if output_transaction.file_hash(dst) != previous_hash:
            versions = tdir / "versions"
            versions.mkdir(exist_ok=True)
            pending.replace(versions / f"app-conflict-{uuid.uuid4().hex[:8]}{ext}")
            raise OutputConflictError("The output changed while the app was writing. "
                                      "Both the external file and the app candidate were preserved.")
        digest = _file_hash(pending)
        new_job = copy.deepcopy(job)
        new_job["translations"] = translations
        for segment in new_job.get("segments", []):
            segment["translation"] = translations.get(segment["seg_id"])
        new_job["status"] = job.get("status") if job.get("status") in ("cancelled", "paused") else "done"
        new_job["overflow"] = report.overflow
        new_job["warnings"] = list(dict.fromkeys((job.get("warnings") or []) + report.warnings))
        new_job["translated_sha256"] = digest
        new_job["revision"] = int(job.get("revision", 0)) + 1
        new_job["content_version"] = new_job["revision"]
        new_job["updated_at"] = _now()
        output_transaction.commit(task_id, pending, dst, job, new_job)
        job = new_job
    finally:
        pending.unlink(missing_ok=True)
    if store.load_settings().get("use_translation_memory", True) and job.get("provider_id") != "mock":
        try:
            from . import memory
            pairs = [(s["text"], clean[s["seg_id"]]) for s in job.get("segments", [])
                     if s.get("translatable") and s["seg_id"] in clean and not s.get("meta", {}).get("context_sensitive")]
            memory.remember_many(job.get("source_lang") or "auto",
                                 job.get("target_lang") or "zh-CN",
                                 job.get("provider_id") or "", pairs)
        except Exception as exc:
            # A memory cache failure cannot invalidate an already-saved document.
            import logging
            logging.getLogger(__name__).warning("translation memory update failed: %s", exc)
    return report


def revise_segment(task_id: str, seg_id: str, new_text: str,
                   expected_revision: int | None = None) -> None:
    revise_segments(task_id, {seg_id: new_text}, expected_revision)


@store.task_operation
def revise_segments(task_id: str, revisions: dict[str, str], expected_revision: int | None = None) -> dict:
    """Validate and commit the entire draft snapshot in one recoverable write."""
    import re
    if not revisions or any(not re.fullmatch(r"s[0-9]{6}(#p[0-9]+)?", key) for key in revisions):
        raise ValueError("Invalid segment ID or empty revision batch")
    job = store.load_job(task_id)
    if not job:
        raise ValueError("Task does not exist")
    _require_idle(job)
    if job.get("migration_issue"):
        raise ValueError(job["migration_issue"])
    _check_revision(job, expected_revision)
    segments = {s["seg_id"]: s for s in job.get("segments", [])}
    if not revisions.keys() <= segments.keys():
        raise ValueError("Segment ID not found in this task")
    _assert_output_unchanged(job)
    translations = dict(job.get("translations") or {})
    translations.update(revisions)
    _apply_translations(task_id, job["ext"], translations)
    if store.load_settings().get("use_translation_memory", True) and job.get("provider_id") != "mock":
        try:
            from . import memory
            for seg_id, text in revisions.items():
                source = segments[seg_id]
                if not source.get("meta", {}).get("context_sensitive"):
                    memory.remember(job.get("source_lang") or "auto", job.get("target_lang") or "zh-CN",
                                    source["text"], job.get("provider_id") or "", text, verified=True)
        except Exception:
            pass
    if job["ext"] != ".pdf":
        from . import renderer
        (store.task_dir(task_id) / "previews" / "render.json").unlink(missing_ok=True)
        renderer.auto_render_after_done(task_id, job["ext"])
    return store.load_job(task_id)
