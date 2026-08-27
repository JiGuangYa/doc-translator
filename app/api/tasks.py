"""Task-related APIs: upload / start / progress / segments / download / cancel / delete."""
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import auth, config, store, utils
from ..formats.common import is_untranslated
from ..services import pipeline, task_manager

router = APIRouter()


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _check_task(task_id: str) -> dict:
    if not utils.valid_task_id(task_id):
        raise HTTPException(400, "Invalid task ID")
    job = store.load_job(task_id)
    if not job:
        raise HTTPException(404, "Task not found")
    return job


def _check_variant(variant: str) -> str:
    """Whitelist-validate the variant to prevent path traversal (e.g. variant=..\\..\\secret)."""
    if variant not in ("original", "translated"):
        raise HTTPException(400, "variant must be 'original' or 'translated'")
    return variant


def _summary(job: dict) -> dict:
    rt = task_manager.get_runtime(job["task_id"]) or {}
    return {
        "task_id": job["task_id"],
        "filename": job["filename"],
        "ext": job["ext"],
        "status": job.get("status"),
        "created_at": job.get("created_at"),
        "segment_count": job.get("segment_count"),
        "total_chars": job.get("total_chars"),
        "skipped_count": job.get("skipped_count"),
        "source_lang": job.get("source_lang"),
        "target_lang": job.get("target_lang"),
        "provider_id": job.get("provider_id"),
        "error": job.get("error"),
        "warnings": job.get("warnings") or [],
        "untranslated_count": pipeline.untranslated_count(job),
        "done_segments": rt.get("done_segments", len([s for s in (job.get("translations") or {})])),
        "total_segments": rt.get("total_segments", job.get("segment_count")),
        "has_translated": (store.task_dir(job["task_id"]) / f"translated{job['ext']}").exists(),
    }


@router.post("/api/upload")
def upload(request: Request, file: UploadFile = File(...)):
    # Synchronous def: parsing large documents is heavy CPU/IO work; run it in a thread pool
    # so the event loop is not blocked. A blocked loop freezes progress polling, cancels,
    # and other uploads for ALL tasks.
    ext = Path(file.filename or "").suffix.lower()
    if ext not in utils.SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"Unsupported file type {ext}. Supported: docx / pptx / xlsx / pdf")

    task_id = utils.new_task_id()
    tmp_path = store.task_dir(task_id) / f"original{ext}"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with tmp_path.open("wb") as f:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                f.write(chunk)
                if size > config.MAX_UPLOAD_MB * 1024 * 1024:
                    tmp_path.unlink(missing_ok=True)
                    raise HTTPException(400, f"File exceeds the {config.MAX_UPLOAD_MB} MB limit")

        job = pipeline.parse_upload(task_id, Path(file.filename).name, tmp_path, {})
    except HTTPException:
        raise
    except Exception as e:
        store.delete_task(task_id)
        raise HTTPException(400, f"Parse failed: {e}") from e
    finally:
        file.file.close()

    auth.audit("upload", filename=Path(file.filename).name if file.filename else "",
               task_id=task_id, ip=_ip(request))
    return _summary(job)


class StartBody(BaseModel):
    provider_id: str
    source_lang: str = "auto"
    target_lang: str = "zh-CN"


@router.post("/api/tasks/{task_id}/start")
def start(task_id: str, body: StartBody, request: Request):
    _check_task(task_id)
    try:
        pipeline.start_translation(task_id, body.provider_id, body.source_lang, body.target_lang)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    auth.audit("task_start", task_id=task_id, provider_id=body.provider_id,
               target_lang=body.target_lang, ip=_ip(request))
    return {"ok": True}


@router.get("/api/tasks")
def list_tasks():
    return [_summary(j) for j in store.list_jobs()]


@router.get("/api/tasks/{task_id}")
def task_detail(task_id: str):
    return _summary(_check_task(task_id))


@router.get("/api/tasks/{task_id}/progress")
def progress(task_id: str):
    job = _check_task(task_id)
    rt = task_manager.get_runtime(task_id) or {}
    return {
        "status": job.get("status") if not rt else rt.get("status", job.get("status")),
        "done_segments": rt.get("done_segments", 0),
        "total_segments": rt.get("total_segments", job.get("segment_count")),
        "error": rt.get("error") or job.get("error"),
    }


@router.get("/api/tasks/{task_id}/segments")
def segments(task_id: str, filter: str = "all"):
    job = _check_task(task_id)
    tr = job.get("translations") or {}
    out = []
    for s in job.get("segments", []):
        translation = s.get("translation") or tr.get(s["seg_id"])
        if filter == "untranslated" and (not s["translatable"] or (translation and not is_untranslated(translation))):
            continue
        out.append({
            "seg_id": s["seg_id"],
            "text": s["text"],
            "translation": translation,
            "context": s.get("context", ""),
            "translatable": s["translatable"],
        })
    return {"filename": job["filename"], "ext": job["ext"], "segments": out,
            "warnings": job.get("warnings") or [], "overflow": job.get("overflow") or []}


class ReviseBody(BaseModel):
    text: str


@router.patch("/api/tasks/{task_id}/segments/{seg_id}")
def revise_segment(task_id: str, seg_id: str, body: ReviseBody, request: Request):
    _check_task(task_id)
    # Whitelist-validate the seg_id shape so a bogus path parameter cannot
    # trigger a full file rewrite (P1-7) or an audit-log no-op.
    import re as _re
    if not _re.fullmatch(r"s[0-9]{6}(#p[0-9]+)?", seg_id or ""):
        raise HTTPException(400, "Invalid segment ID")
    try:
        pipeline.revise_segment(task_id, seg_id, body.text)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    auth.audit("segment_revise", task_id=task_id, seg_id=seg_id, ip=_ip(request))
    return {"ok": True}


@router.get("/api/tasks/{task_id}/download")
def download(task_id: str, variant: str = "translated"):
    job = _check_task(task_id)
    variant = _check_variant(variant)
    ext = job["ext"]
    path = store.task_dir(task_id) / f"{variant}{ext}"
    if not path.exists():
        raise HTTPException(404, "File does not exist (translation not finished yet?)")
    stem = utils.sanitize_filename(Path(job["filename"]).stem)
    suffix = "" if variant == "translated" else ".original"
    lang = job.get("target_lang") or ""
    out_name = f"{stem}.{lang}{suffix}{ext}" if variant == "translated" else f"{stem}{suffix}{ext}"
    return FileResponse(path, filename=out_name,
                        headers={"Content-Disposition": utils.content_disposition(out_name)})


@router.get("/api/tasks/{task_id}/file")
def raw_file(task_id: str, variant: str = "translated"):
    """Stream the raw file for direct fetch by frontend preview libraries."""
    job = _check_task(task_id)
    variant = _check_variant(variant)
    path = store.task_dir(task_id) / f"{variant}{job['ext']}"
    if not path.exists():
        raise HTTPException(404, "File does not exist")
    return FileResponse(path)


@router.post("/api/tasks/{task_id}/cancel")
def cancel(task_id: str, request: Request):
    _check_task(task_id)
    ok = task_manager.request_cancel(task_id)
    if ok:
        auth.audit("task_cancel", task_id=task_id, ip=_ip(request))
    return {"ok": ok, "message": None if ok else "Task is not translating"}


@router.delete("/api/tasks/{task_id}")
def delete(task_id: str, request: Request):
    job = _check_task(task_id)
    ok = store.delete_task(task_id)
    if ok:
        auth.audit("task_delete", task_id=task_id, filename=job.get("filename"),
                   ip=_ip(request))
    return {"ok": ok}
