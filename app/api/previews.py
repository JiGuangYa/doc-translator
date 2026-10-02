"""Preview APIs: PDF page rendering, XLSX two-column HTML."""
from pathlib import Path
import tempfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response

from .. import store
from ..api.tasks import _check_task, _check_variant
from ..services import renderer

router = APIRouter()


def _file(task_id: str, variant: str) -> tuple[dict, Path]:
    job = _check_task(task_id)
    variant = _check_variant(variant)
    path = store.document_path(job, variant)
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
    with store.task_lock(task_id):
        return _pdf_page(task_id, page, variant)


def _pdf_page(task_id: str, page: int, variant: str):
    job, path = _file(task_id, variant)
    if job["ext"] != ".pdf":
        raise HTTPException(400, "Only PDF supports page-image preview")
    cache = store.task_dir(task_id) / "previews" / f"v{job.get('content_version', 0)}" / f"{variant}_p{page}.png"
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        import fitz
        with fitz.open(path) as doc:
            if not 1 <= page <= doc.page_count:
                raise HTTPException(404, f"Page out of range 1-{doc.page_count}")
            pix = doc[page - 1].get_pixmap(dpi=110)
            # A crash must not leave a truncated PNG that looks cached forever.
            with tempfile.NamedTemporaryFile(dir=cache.parent, suffix=".png", delete=False) as stream:
                temporary = Path(stream.name)
            try:
                pix.save(temporary)
                temporary.replace(cache)
            finally:
                temporary.unlink(missing_ok=True)
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
    with store.task_lock(task_id):
        return _render_page(task_id, page, variant)


def _render_page(task_id: str, page: int, variant: str):
    _check_task(task_id)
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
        "content_version": job.get("content_version", 0),
        "ext": job["ext"],
        "filename": job["filename"],
        "has_translated": store.document_path(job, "translated").exists(),
        "render_available": renderer.available(),
    }
    if job["ext"] == ".pdf":
        import fitz
        for variant in ("original", "translated"):
            path = store.document_path(job, variant)
            info[f"{variant}_pages"] = 0
            if path.exists():
                with fitz.open(path) as doc:
                    info[f"{variant}_pages"] = doc.page_count
        info["pages"] = max(info["original_pages"], info["translated_pages"])
    return info
