"""Preview APIs: PDF page rendering, XLSX two-column HTML."""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response

from .. import store
from ..api.tasks import _check_task, _check_variant
from ..services import renderer

router = APIRouter()


def _file(task_id: str, variant: str) -> tuple[dict, Path]:
    job = _check_task(task_id)
    variant = _check_variant(variant)
    path = store.task_dir(task_id) / f"{variant}{job['ext']}"
    if not path.exists():
        raise HTTPException(404, f"{variant} file does not exist")
    return job, path


# Note: /pages MUST be declared before /{page}, otherwise "pages" gets swallowed by
# the {page} path parameter and returns 422.
@router.get("/api/tasks/{task_id}/preview/pdf/pages")
def pdf_page_count(task_id: str, variant: str = "original"):
    job, path = _file(task_id, variant)
    import fitz
    with fitz.open(path) as doc:
        return {"pages": doc.page_count}


@router.get("/api/tasks/{task_id}/preview/pdf/{page}")
def pdf_page(task_id: str, page: int, variant: str = "original"):
    """Render a specific PDF page to PNG, cached under previews/."""
    job, path = _file(task_id, variant)
    if job["ext"] != ".pdf":
        raise HTTPException(400, "Only PDF supports page-image preview")
    stamp = path.stat()
    cache = store.task_dir(task_id) / "previews" / f"v{job.get('revision', 0)}" / f"{variant}_{stamp.st_size}_{stamp.st_mtime_ns}_p{page}.png"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        import fitz
        with fitz.open(path) as doc:
            if not 1 <= page <= doc.page_count:
                raise HTTPException(404, f"Page out of range 1-{doc.page_count}")
            pix = doc[page - 1].get_pixmap(dpi=110)
            pix.save(cache)
    return Response(content=cache.read_bytes(), media_type="image/png")


@router.get("/api/tasks/{task_id}/preview/render")
def render_status_api(task_id: str):
    """High-fidelity render status (LibreOffice page-image compare)."""
    _check_task(task_id)
    return renderer.render_status(task_id)


@router.post("/api/tasks/{task_id}/preview/render")
def render_start_api(task_id: str):
    """Trigger a high-fidelity render in the background."""
    job = _check_task(task_id)
    return renderer.start_render(task_id, job["ext"])


@router.get("/api/tasks/{task_id}/preview/render/page/{page}")
def render_page_api(task_id: str, page: int, variant: str = "original"):
    """Ready high-fidelity page image as PNG."""
    if variant not in ("original", "translated"):
        raise HTTPException(400, "variant must be 'original' or 'translated'")
    png = renderer.page_png_path(task_id, page, variant)
    if not png:
        st = renderer.render_status(task_id)
        detail = st.get("error") or f"Page {page} not generated (status {st['status']})"
        raise HTTPException(404, detail)
    return Response(content=png.read_bytes(), media_type="image/png")


@router.get("/api/tasks/{task_id}/preview/xlsx")
def xlsx_preview(task_id: str):
    from ..formats.xlsx_preview import render_xlsx_dual_html
    job_o, orig = _file(task_id, "original")
    _, trans_path = _file(task_id, "translated")
    html = render_xlsx_dual_html(orig, trans_path)
    return HTMLResponse(html)


@router.get("/api/tasks/{task_id}/info")
def file_info(task_id: str):
    """Summary info used by the frontend Compare view on initialization."""
    job = _check_task(task_id)
    info = {
        "ext": job["ext"], "content_version": job.get("revision", 0), "revision": job.get("revision", 0),
        "filename": job["filename"],
        "has_translated": (store.task_dir(task_id) / f"translated{job['ext']}").exists(),
        "render_available": renderer.available(),
    }
    if job["ext"] == ".pdf":
        try:
            import fitz
            p_orig = store.task_dir(task_id) / "original.pdf"
            with fitz.open(p_orig) as doc:
                info["pages"] = doc.page_count
        except Exception:
            info["pages"] = 0
    if job["ext"] == ".pdf":
        info["original_pages"] = info.get("pages", 0)
        info["translated_pages"] = 0
        translated = store.task_dir(task_id) / "translated.pdf"
        if translated.exists():
            with fitz.open(translated) as document:
                info["translated_pages"] = len(document)
        info["pages"] = max(info["original_pages"], info["translated_pages"])
    return info
