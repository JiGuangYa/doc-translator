"""Task-related APIs: upload / start / progress / segments / download / cancel / delete."""
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .. import auth, config, store, utils
from ..formats.common import FormatAdapterError, is_untranslated
from ..services import drafts, ocr, pipeline, renderer, task_manager

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
    untranslated = job.get("untranslated_count")
    if untranslated is None:
        untranslated = pipeline.untranslated_count(job)
    usage = job.get("usage") or {"prompt_tokens": 0, "completion_tokens": 0}
    input_price = job.get("input_price_per_million")
    output_price = job.get("output_price_per_million")
    estimate = (round((usage.get("prompt_tokens", 0) * input_price +
                       usage.get("completion_tokens", 0) * output_price) / 1_000_000, 6)
                if input_price is not None and output_price is not None and not job.get("history_usage_unknown") else None)
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
        "untranslated_count": untranslated,
        "done_segments": rt.get("done_segments", job.get("done_segments",
                            len(job.get("translations") or {}))),
        "total_segments": rt.get("total_segments", job.get("segment_count")),
        "has_translated": (store.task_dir(job["task_id"]) / f"translated{job['ext']}").exists(),
        "revision": job.get("revision", 0),
        "content_version": job.get("revision", 0),
        "archived": job.get("archived", False),
        "provider_snapshot": job.get("provider_snapshot"),
        "requires_snapshot_confirmation": job.get("requires_snapshot_confirmation", False),
        "history_usage_unknown": job.get("history_usage_unknown", False),
        "migration_issue": job.get("migration_issue"),
        "usage": usage,
        "estimated_cost_usd": estimate,
        "token_budget": job.get("token_budget", 0),
        "ocr_scanned": job.get("ocr_scanned", False),
        "ocr_pages": job.get("ocr_pages", 0),
        "ocr_required_pages": ocr.required_pages(job),
        "ocr_completed_pages": ocr.completed_pages(job),
        "ocr_empty_pages": job.get("ocr_empty_pages") or [],
        "ocr_dismissed_pages": job.get("ocr_dismissed_pages") or [],
        "ocr_optional_pages": job.get("ocr_optional_pages") or [],
        "ocr_review_count": job.get("ocr_review_count"),
        "external_edit": job.get("external_edit", False),
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
    except FormatAdapterError as e:
        if ext == ".pdf" and "no text layer" in str(e):
            job = pipeline.register_scanned_pdf(task_id, Path(file.filename).name, tmp_path)
            return _summary(job)
        store.delete_task(task_id)
        raise HTTPException(400, str(e)) from e
    except HTTPException:
        store.delete_task(task_id)
        raise
    except Exception as e:
        store.delete_task(task_id)
        raise HTTPException(400, f"Parse failed: {e}") from e
    finally:
        file.file.close()

    auth.audit("upload", filename=Path(file.filename).name if file.filename else "",
               task_id=task_id, ip=_ip(request))
    return _summary(job)


class OCRLine(BaseModel):
    page: int
    bbox: list[float]
    text: str
    confidence: float


class OCRBody(BaseModel):
    lines: list[OCRLine]


class OCRReviseBody(BaseModel):
    text: str = Field(min_length=1, max_length=100000)
    expected_revision: int | None = Field(default=None, ge=0)


def _validate_ocr_lines(job, lines, expected_page=None):
    if len(lines) > 50000:
        raise HTTPException(400, "Too many OCR lines")
    for line in lines:
        if not 1 <= line.page <= job.get("ocr_pages", 0) or expected_page is not None and line.page != expected_page:
            raise HTTPException(400, "OCR page is out of range")
        if (len(line.bbox) != 4 or any(not 0 <= value <= 1 for value in line.bbox) or
                line.bbox[2] <= line.bbox[0] or line.bbox[3] <= line.bbox[1]):
            raise HTTPException(400, "Invalid OCR bounding box")
        if not 0 <= line.confidence <= 1 or len(line.text) > 10000:
            raise HTTPException(400, "Invalid OCR text or confidence")


@router.post("/api/tasks/{task_id}/ocr")
def accept_ocr(task_id: str, body: OCRBody, request: Request):
    job = _check_task(task_id)
    if not job.get("ocr_scanned") or job.get("status") != "ocr_pending":
        raise HTTPException(409, "Task is not awaiting OCR")
    _validate_ocr_lines(job, body.lines)
    try:
        updated = pipeline.accept_ocr(task_id, [line.model_dump() for line in body.lines])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    auth.audit("ocr_accept", task_id=task_id, line_count=len(body.lines), ip=_ip(request))
    return _summary(updated)


@router.patch("/api/tasks/{task_id}/ocr/{seg_id}")
def revise_ocr(task_id: str, seg_id: str, body: OCRReviseBody, request: Request):
    _check_task(task_id)
    try:
        pipeline.revise_ocr_text(task_id, seg_id, body.text, body.expected_revision)
    except ocr.OCRRevisionConflict as error:
        return JSONResponse(status_code=409, content={"detail": str(error), "code": "revision_conflict"})
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    auth.audit("ocr_revise", task_id=task_id, seg_id=seg_id, ip=_ip(request))
    return {"ok": True}


@router.post("/api/tasks/{task_id}/ocr/prepare")
def prepare_ocr(task_id: str, request: Request):
    _check_task(task_id)
    try:
        job = ocr.prepare(task_id)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    auth.audit("ocr_prepare", task_id=task_id, ip=_ip(request))
    return _summary(job)


@router.post("/api/tasks/{task_id}/ocr/page/{page}")
def accept_ocr_page(task_id: str, page: int, body: OCRBody, request: Request):
    job = _check_task(task_id)
    _validate_ocr_lines(job, body.lines, page)
    try:
        job = ocr.accept_page(task_id, page, [line.model_dump() for line in body.lines])
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    auth.audit("ocr_page", task_id=task_id, page=page, line_count=len(body.lines), ip=_ip(request))
    return _summary(job)


@router.post("/api/tasks/{task_id}/ocr/page/{page}/confirm-empty")
def confirm_empty_ocr_page(task_id: str, page: int, request: Request):
    _check_task(task_id)
    try:
        job = ocr.confirm_empty(task_id, page)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    auth.audit("ocr_empty_confirm", task_id=task_id, page=page, ip=_ip(request))
    return _summary(job)


@router.post("/api/tasks/{task_id}/ocr/page/{page}/text")
def add_ocr_page_text(task_id: str, page: int, body: OCRReviseBody, request: Request):
    _check_task(task_id)
    try:
        job = ocr.add_page_text(task_id, page, body.text)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    auth.audit("ocr_page_text", task_id=task_id, page=page, ip=_ip(request))
    return _summary(job)


class StartBody(BaseModel):
    provider_id: str
    source_lang: str = "auto"
    target_lang: str = "zh-CN"
    max_total_tokens: int | None = None
    confirm_snapshot: bool = False


class DuplicateBody(BaseModel):
    source_lang: str = Field(default="auto", min_length=1, max_length=100)
    target_lang: str = Field(default="zh-CN", min_length=1, max_length=100)
    provider_id: str | None = None
    max_total_tokens: int = Field(default=0, ge=0, le=10_000_000)


@router.post("/api/tasks/{task_id}/duplicate")
def duplicate(task_id: str, body: DuplicateBody, request: Request):
    _check_task(task_id)
    job = pipeline.duplicate_task(task_id, body.source_lang, body.target_lang,
                                  body.provider_id, body.max_total_tokens)
    auth.audit("task_duplicate", task_id=task_id, new_task_id=job["task_id"], ip=_ip(request))
    return _summary(job)


@router.post("/api/tasks/{task_id}/start")
def start(task_id: str, body: StartBody, request: Request):
    _check_task(task_id)
    if body.max_total_tokens is not None and not 0 <= body.max_total_tokens <= 10_000_000:
        raise HTTPException(400, "max_total_tokens must be 0-10000000")
    try:
        pipeline.start_translation(task_id, body.provider_id, body.source_lang,
                                   body.target_lang, body.max_total_tokens, body.confirm_snapshot)
    except pipeline.OutputConflictError as e:
        raise HTTPException(409, str(e)) from e
    except FormatAdapterError as error:
        raise HTTPException(400, str(error)) from error
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    auth.audit("task_start", task_id=task_id, provider_id=body.provider_id,
               target_lang=body.target_lang, ip=_ip(request))
    return {"ok": True}


@router.get("/api/tasks")
def list_tasks():
    return [_summary(j) for j in store.list_job_headers()]


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
def segments(task_id: str, filter: str = "all", offset: int = 0, limit: int | None = None,
             q: str = "", sheet: str = "", review_only: bool = False, anchor_id: str = ""):
    if offset < 0 or limit is not None and not 1 <= limit <= 2000:
        raise HTTPException(400, "offset must be non-negative and limit must be 1-2000")
    job = _check_task(task_id)
    tr = job.get("translations") or {}
    draft = drafts.for_task(task_id)
    overflow = set(job.get("overflow") or [])
    # Resolve anchors against the normal reading order, never a temporary search.
    if anchor_id and limit and not q and filter == "all" and not sheet and not review_only:
        for index, segment in enumerate(job.get("segments", [])):
            if segment["seg_id"] == anchor_id:
                offset = index // limit * limit
                break
    out = []
    total = 0
    sheets = {}
    needle = q.strip().casefold()
    for s in job.get("segments", []):
        translation = tr.get(s["seg_id"], s.get("translation"))
        context = s.get("context", "")
        worksheet = context.rsplit("!", 1)[0] if job["ext"] == ".xlsx" and "!" in context else ""
        if worksheet:
            sheets[worksheet] = None
        if sheet and worksheet != sheet:
            continue
        if filter == "untranslated" and (not s["translatable"] or (translation and not is_untranslated(translation))):
            continue
        if filter == "drafts" and s["seg_id"] not in draft:
            continue
        if filter == "layout" and s["seg_id"] not in overflow:
            continue
        meta = s.get("meta") or {}
        confidence = meta.get("confidence")
        if review_only and (meta.get("reviewed") or not (
                meta.get("needs_review") or (confidence is not None and confidence < 0.75))):
            continue
        if needle and not any(needle in str(value).casefold() for value in
                              (s["text"], translation or "", context, draft.get(s["seg_id"], ""))):
            continue
        total += 1
        if limit is not None and not offset <= total - 1 < offset + limit:
            continue
        out.append({
            "seg_id": s["seg_id"],
            "text": s["text"],
            "translation": translation,
            "draft": draft.get(s["seg_id"]),
            "context": context,
            "meta": meta,
            "translatable": s["translatable"],
        })
    return {"filename": job["filename"], "ext": job["ext"], "segments": out,
            "revision": job.get("revision", 0),
            "total": total, "offset": offset, "limit": limit,
            "sheets": list(sheets),
            "warnings": job.get("warnings") or [], "overflow": job.get("overflow") or []}


class ReviseBody(BaseModel):
    text: str
    expected_revision: int | None = Field(default=None, ge=0)


class BulkReviseBody(BaseModel):
    revisions: dict[str, str]
    expected_revision: int | None = Field(default=None, ge=0)


@router.patch("/api/tasks/{task_id}/segments")
def revise_segments(task_id: str, body: BulkReviseBody, request: Request):
    _check_task(task_id)
    try:
        updated = pipeline.revise_segments(task_id, body.revisions, body.expected_revision)
    except pipeline.RevisionConflictError as error:
        return JSONResponse(status_code=409, content={"detail": str(error), "code": "revision_conflict"})
    except pipeline.OutputConflictError as error:
        return JSONResponse(status_code=409, content={"detail": str(error), "code": "output_conflict"})
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    auth.audit("segments_revise", task_id=task_id, count=len(body.revisions), ip=_ip(request))
    return {"ok": True, "revision": updated["revision"], "updated_seg_ids": list(body.revisions)}


class ArchiveBody(BaseModel):
    archived: bool


@router.patch("/api/tasks/{task_id}")
def archive(task_id: str, body: ArchiveBody):
    with store.task_lock(task_id):
        job = _check_task(task_id)
        try:
            pipeline._require_idle(job)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        updated = store.update_job(task_id, lambda job: job.update(archived=body.archived))
        return _summary(updated)


@router.patch("/api/tasks/{task_id}/segments/{seg_id}")
def revise_segment(task_id: str, seg_id: str, body: ReviseBody, request: Request):
    _check_task(task_id)
    # Whitelist-validate the seg_id shape so a bogus path parameter cannot
    # trigger a full file rewrite (P1-7) or an audit-log no-op.
    import re as _re
    if not _re.fullmatch(r"s[0-9]{6}(#p[0-9]+)?", seg_id or ""):
        raise HTTPException(400, "Invalid segment ID")
    try:
        pipeline.revise_segment(task_id, seg_id, body.text, body.expected_revision)
    except pipeline.RevisionConflictError as e:
        return JSONResponse(status_code=409, content={"detail": str(e), "code": "revision_conflict"})
    except pipeline.OutputConflictError as e:
        return JSONResponse(status_code=409, content={"detail": str(e), "code": "output_conflict"})
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    auth.audit("segment_revise", task_id=task_id, seg_id=seg_id, ip=_ip(request))
    return {"ok": True, "revision": store.load_job(task_id).get("revision", 0)}


class ResolveConflictBody(BaseModel):
    action: str
    expected_revision: int | None = Field(default=None, ge=0)


@router.post("/api/tasks/{task_id}/resolve-conflict")
def resolve_conflict(task_id: str, body: ResolveConflictBody, request: Request):
    _check_task(task_id)
    try:
        job = pipeline.resolve_output_conflict(task_id, body.action, body.expected_revision)
    except pipeline.RevisionConflictError as e:
        return JSONResponse(status_code=409, content={"detail": str(e), "code": "revision_conflict"})
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    auth.audit("output_conflict_resolve", task_id=task_id, action=body.action,
               ip=_ip(request))
    return _summary(job)


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
    with store.task_lock(task_id):
        job = _check_task(task_id)
        rt = task_manager.get_runtime(task_id) or {}
        if job.get("status") in ("translating", "queued") or rt.get("status") == "translating" or task_manager.is_active(task_id):
            raise HTTPException(409, "Cancel the running or queued translation before moving it to trash")
        renderer.cancel_render(task_id)
        ok = store.trash_task(task_id)
    if ok:
        auth.audit("task_delete", task_id=task_id, filename=job.get("filename"),
                   ip=_ip(request))
    return {"ok": ok}


@router.get("/api/trash")
def list_deleted_tasks():
    return {"tasks": store.list_trash()}


@router.post("/api/trash/{task_id}/restore")
def restore_deleted_task(task_id: str, request: Request):
    if not utils.valid_task_id(task_id):
        raise HTTPException(400, "Invalid task ID")
    try:
        ok = store.restore_task(task_id)
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not ok:
        raise HTTPException(404, "Task not found in trash")
    auth.audit("task_restore", task_id=task_id, ip=_ip(request))
    return {"ok": True}
