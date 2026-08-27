"""Core orchestration: parse -> segment -> translate -> write back."""
import shutil
import time
from pathlib import Path

from .. import store
from ..formats import common
from . import task_manager
from .llm import translator


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def parse_upload(task_id: str, filename: str, src_path: Path, options: dict) -> dict:
    """Synchronously parse the uploaded file and return a job summary
    (including segment_count, etc.)."""
    ext = src_path.suffix.lower()
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
             "translatable": True, "translation": None}
            for s in kept
        ] + [
            {"seg_id": s.seg_id, "text": s.text, "context": s.context, "meta": s.meta,
             "translatable": False, "translation": None}
            for s in segments if s.seg_id not in kept_ids
        ],
        "segment_count": len(kept),
        "total_chars": sum(len(s.text) for s in kept),
        "skipped_count": skipped,
        "warnings": result.warnings,
        "translations": {},       # incremental results for resumption
        "provider_id": None,
        "source_lang": None,
        "target_lang": None,
    }
    # Copy the original file (the upload endpoint may have already written
    # to this path, in which case skip the self-copy)
    dst = store.task_dir(task_id) / f"original{ext}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src_path.exists() and src_path.resolve() != dst.resolve():
        shutil.copy2(src_path, dst)
    store.save_job(task_id, job)
    return job


def start_translation(task_id: str, provider_id: str, source_lang: str, target_lang: str) -> None:
    """Submit a translation task to the thread pool."""
    job = store.load_job(task_id)
    if not job:
        raise ValueError("Task does not exist")
    rt = task_manager.get_runtime(task_id) or {}
    if job.get("status") == "translating" and rt.get("status") == "translating":
        raise ValueError("Task is currently translating; do not start it again")
    if job["status"] == "done" and not _has_untranslated(job):
        return  # already fully translated

    settings = store.load_settings()
    # CAS-style dedup placeholder: repeated clicks within the queueing window
    # would otherwise cause a double submission and double token burn
    if not task_manager.try_mark_submitted(task_id):
        raise ValueError("Task has already been submitted; do not start it again")

    job.update({
        "status": "translating",
        "provider_id": provider_id,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "updated_at": _now(),
    })
    store.save_job(task_id, job)
    # Synchronously mark as running: otherwise the runtime status lags behind
    # while the task is queued, creating a gap in the dedup check.
    task_manager.update(task_id, status="translating")

    def run():
        try:
            _run_translation(task_id, job["ext"], provider_id,
                             source_lang, target_lang, settings)
        finally:
            task_manager.mark_finished(task_id)

    task_manager.submit(run, task_id)


def _has_untranslated(job: dict) -> bool:
    for s in _job_segments(job):
        if not s["translatable"]:
            continue
        cur = s.get("translation") or job.get("translations", {}).get(s["seg_id"])
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
        cur = s.get("translation") or tr.get(s["seg_id"])
        if not cur or common.is_untranslated(cur):
            n += 1
    return n


def _job_segments(job: dict) -> list[dict]:
    segs = job.get("segments") or []
    # Backward compat: merge the translations dict into segments
    tr = job.get("translations") or {}
    for s in segs:
        if not s.get("translation") and tr.get(s["seg_id"]):
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

    done = [0]
    last_persist = [time.monotonic()]

    def on_progress(d: int, total: int, snapshot: dict | None = None):
        done[0] = d
        task_manager.update(task_id, done_segments=d, total_segments=total)
        # Throttled incremental flush to disk every 2 seconds: large-file
        # translation can easily take tens of minutes; without persistence,
        # a crash/restart wipes all completed work and turns resume into a
        # full retranslation.
        if snapshot and time.monotonic() - last_persist[0] > 2.0:
            last_persist[0] = time.monotonic()

            def mut(j: dict):
                j_trans = j.get("translations") or {}
                j_trans.update(snapshot)
                j["translations"] = j_trans
            store.update_job(task_id, mut)

    translations: dict[str, str] = dict(existing)
    try:
        if provider_id == "mock":
            translations = _mock_translate(seg_objs, existing, on_progress)
        else:
            translations = translator.translate_segments(
                seg_objs, provider_id, source_lang, target_lang, settings,
                existing=existing, progress_cb=on_progress,
                cancel_check=lambda: task_manager.is_cancel_requested(task_id))
    except translator.TranslationCancelled as e:
        # Cancellation must not lose work: merge any batch results that were
        # already complete at the time of cancellation, then persist.
        partial = getattr(e, "partial", None) or {}
        if partial:
            translations.update(partial)
        task_manager.persist_status(task_id, status="cancelled", error=None, updated_at=_now())
        task_manager.update(task_id, status="cancelled")
        _apply_translations(task_id, ext, translations)
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
                for s in _job_segments(job):
                    if job_trans.get(s["seg_id"]):
                        s["translation"] = job_trans[s["seg_id"]]
                store.save_job(task_id, job)
        task_manager.persist_status(task_id, status="failed", error=msg, updated_at=_now())
        task_manager.update(task_id, status="failed", error=msg)
        return

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


def _apply_translations(task_id: str, ext: str, translations: dict[str, str]) -> "common.WriteReport":
    """Invoke the format writer to write the translated file and store the
    translations dict in job.json."""
    from ..formats import common as fmt_common
    tdir = store.task_dir(task_id)
    job = store.load_job(task_id)

    clean = {k: v for k, v in translations.items() if v and not common.is_untranslated(v)}
    src = tdir / f"original{ext}"
    dst = tdir / f"translated{ext}"
    options = {"target_lang": job.get("target_lang"), "translate_notes": store.load_settings().get("translate_notes", True)}
    handler = fmt_common.get_format_handler(ext)
    report = handler.write_back(src, dst, clean, options)

    job["translations"] = translations
    job["status"] = job.get("status") if job.get("status") == "cancelled" else "done"
    if report.overflow:
        job["overflow"] = report.overflow
    all_warnings = list(job.get("warnings") or []) + report.warnings
    job["warnings"] = all_warnings
    job["updated_at"] = _now()
    store.save_job(task_id, job)
    return report


def revise_segment(task_id: str, seg_id: str, new_text: str) -> None:
    """Manually revise a single segment's translation and rewrite the file."""
    import re as _re
    if not _re.fullmatch(r"s[0-9]{6}(#p[0-9]+)?", seg_id or ""):
        raise ValueError("Invalid segment ID")
    job = store.load_job(task_id)
    if not job:
        raise ValueError("Task does not exist")
    rt = task_manager.get_runtime(task_id) or {}
    if job.get("status") == "translating" and rt.get("status") == "translating":
        # The translation thread overwrites the file wholesale; the manual
        # edit would be clobbered by the next save.
        raise ValueError("Task is currently translating; please wait for it to finish or cancel before editing")
    if not any(s["seg_id"] == seg_id for s in job.get("segments", [])):
        # No matching segment: refuse rather than silently rewriting the
        # file with unchanged translations (P1-7).
        raise ValueError("Segment ID not found in this task")
    translations = job.get("translations") or {}
    translations[seg_id] = new_text
    job["translations"] = translations
    for s in job.get("segments", []):
        if s["seg_id"] == seg_id:
            s["translation"] = new_text
    store.save_job(task_id, job)
    _apply_translations(task_id, job["ext"], translations)
    # Translation changed: invalidate high-fidelity page images and re-render in the background.
    if job["ext"] != ".pdf":
        from . import renderer
        (store.task_dir(task_id) / "previews" / "render.json").unlink(missing_ok=True)
        renderer.auto_render_after_done(task_id, job["ext"])
