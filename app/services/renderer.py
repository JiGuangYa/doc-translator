"""LibreOffice high-fidelity rendering: office docs -> PDF -> page PNGs.

The browser-side docx-preview is only an approximation; to make the
side-by-side view match what Office/WPS renders, the server-side
LibreOffice converts the original/translated document to PDF and then
emits per-page images (works for docx/pptx/xlsx).
"""
import json
import os
import shutil
import threading
import time
from pathlib import Path

from .. import config, store
from . import process_runner

# Only one soffice process runs at a time (concurrent conversions can lock the profile)
_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}
_state_lock = threading.Lock()
_stopping = threading.Event()
_cancel_events: dict[str, threading.Event] = {}

CONVERT_TIMEOUT = 300   # per-file conversion timeout (seconds)
DPI = 110


def soffice_path() -> str | None:
    """Locate the soffice executable; returns None if not found."""
    p = shutil.which("soffice") or shutil.which("libreoffice")
    if p:
        return p
    for cand in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice", "/opt/libreoffice/program/soffice",
    ):
        if Path(cand).exists():
            return cand
    return None


def genoffice_path() -> str | None:
    """Find GenOffice's bundled CLI without requiring a PATH installation."""
    candidates = [
        os.environ.get("GENOFFICE_CLI", ""),
        shutil.which("genoffice") or "",
        str(Path.home() / "Applications/GenOffice.app/Contents/Resources/cli/genoffice"),
        "/Applications/GenOffice.app/Contents/Resources/cli/genoffice",
    ]
    return next((p for p in candidates if p and Path(p).is_file() and os.access(p, os.X_OK)), None)


def available() -> bool:
    return genoffice_path() is not None or soffice_path() is not None


# ---- status ----

def _profile_dir(task_tag: str) -> Path:
    return Path(config.DATA_DIR) / "_lo_profile" / task_tag


def _write_status_file(f: Path, cur: dict):
    store._atomic_write_json(f, cur)


def reset_shutdown() -> None:
    _stopping.clear()


def cancel_render(task_id: str) -> None:
    with _state_lock:
        event = _cancel_events.get(task_id)
        thread = _threads.get(task_id)
        if event:
            event.set()
    if thread and thread is not threading.current_thread():
        thread.join(timeout=3)


def shutdown() -> None:
    _stopping.set()
    with _state_lock:
        for event in _cancel_events.values():
            event.set()
        threads = list(_threads.values())
    deadline = time.monotonic() + 3
    for thread in threads:
        thread.join(timeout=max(0, deadline - time.monotonic()))


def recover_stale() -> None:
    """After a restart, all rendering threads are gone, so any rendering
    left in the 'rendering' state will never make progress and the UI will
    spin forever. Reset such entries to 'none' at startup (so the user can
    re-trigger)."""
    if not config.TASKS_DIR.exists():
        return
    for d in config.TASKS_DIR.iterdir():
        f = d / "previews" / "render.json"
        if not f.exists():
            continue
        try:
            st = json.loads(f.read_text("utf-8"))
        except Exception:
            continue
        if st.get("status") == "rendering":
            st.update(status="none", pages=0, error=None)
            try:
                _write_status_file(f, st)
            except OSError:
                pass


def _status_file(task_id: str) -> Path:
    return store.task_dir(task_id) / "previews" / "render.json"


def render_status(task_id: str) -> dict:
    """Render status: none / rendering / ready / failed / unavailable."""
    if not available():
        return {"status": "unavailable", "pages": 0,
                "error": "GenOffice or LibreOffice is not installed on the server"}
    f = _status_file(task_id)
    if f.exists():
        try:
            status = json.loads(f.read_text("utf-8"))
            if status.get("status") == "ready":
                if "original_pages" not in status or "translated_pages" not in status:
                    # Earlier caches stored only the original page count. Rebuild
                    # them once so existing tasks gain access to overflow pages.
                    return {"status": "none", "pages": 0, "error": None}
                job = store.load_job(task_id)
                if job:
                    for variant in ("original", "translated"):
                        document = store.task_dir(task_id) / f"{variant}{job['ext']}"
                        if document.exists() and document.stat().st_mtime > f.stat().st_mtime:
                            return {"status": "none", "pages": 0, "error": None}
            return status
        except Exception:
            pass
    return {"status": "none", "pages": 0, "error": None}


def start_render(task_id: str, ext: str) -> dict:
    """Render both versions of the task in a background thread; if a render
    is already running or finished, just return the current status."""
    with _state_lock:
        st = render_status(task_id)
        if st["status"] == "unavailable":
            return st
        if _stopping.is_set():
            return {"status": "none", "pages": 0, "error": "Preview paused while closing"}
        if st["status"] == "ready":
            translated = store.task_dir(task_id) / f"translated{ext}"
            if not translated.exists() or page_png_path(task_id, 1, "translated"):
                return st
        thread = _threads.get(task_id)
        if thread and thread.is_alive():
            return st
        event = threading.Event()
        _cancel_events[task_id] = event
        _write_status(task_id, status="rendering", pages=0)
        thread = threading.Thread(target=_render_task, args=(task_id, ext, event), daemon=True,
                                  name=f"render-{task_id[:8]}")
        _threads[task_id] = thread
        thread.start()
    return {"status": "rendering", "pages": 0, "error": None}


def _write_status(task_id: str, **kw):
    if not (store.task_dir(task_id) / "job.json").exists():
        return
    f = _status_file(task_id)
    cur = {}
    try:
        cur = json.loads(f.read_text("utf-8"))
    except Exception:
        pass
    cur.update(kw)
    cur["content_version"] = (store.load_job(task_id) or {}).get("revision", 0)
    _write_status_file(f, cur)


# ---- conversion and rasterization ----

def _run_soffice(cmd: list[str], timeout: int = CONVERT_TIMEOUT,
                 cancel_event=None) -> tuple[int, bytes, bytes]:
    result = process_runner.run(cmd, timeout=timeout, label="LibreOffice conversion",
                                cancel_event=cancel_event)
    return result.returncode, result.stdout.encode(), result.stderr.encode()


def _convert_to_pdf(soffice: str, src: Path, outdir: Path, task_tag: str, cancel_event=None) -> Path:
    """Headless soffice -> PDF; returns the generated PDF path."""
    outdir.mkdir(parents=True, exist_ok=True)
    # Each task uses its own profile to avoid conflicts with other LibreOffice
    # processes (deleted after use, see _render_task).
    profile = _profile_dir(task_tag)
    profile.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        soffice, "--headless", "--norestore", "--nolockcheck",
        f"-env:UserInstallation={profile.as_uri()}",
        "--convert-to", "pdf", "--outdir", str(outdir), str(src),
    ]
    code, out, err = _run_soffice(cmd, cancel_event=cancel_event)
    pdf = outdir / (src.stem + ".pdf")
    if code != 0 or not pdf.exists():
        msg = (err or out or b"").decode("utf-8", "replace")[-300:]
        raise RuntimeError(f"LibreOffice conversion failed: {msg}")
    return pdf


def _render_task(task_id: str, ext: str, cancel_event=None):
    tdir = store.task_dir(task_id)
    prev_dir = tdir / "previews"
    work = tdir / "previews" / "_render_work"
    profile = _profile_dir(task_id)
    cancel_event = cancel_event or threading.Event()
    acquired = False

    def check_cancelled():
        if _stopping.is_set() or cancel_event.is_set() or not (tdir / "job.json").exists():
            raise process_runner.ProcessCancelled("Preview cancelled")

    def versions():
        return {variant: (path.stat().st_size, path.stat().st_mtime_ns)
                for variant in ("original", "translated")
                if (path := tdir / f"{variant}{ext}").exists()}

    try:
        genoffice = genoffice_path()
        soffice = soffice_path() if not genoffice else None
        if not genoffice and not soffice:
            raise RuntimeError("GenOffice or LibreOffice is not installed")
        while not acquired:
            check_cancelled()
            acquired = _lock.acquire(timeout=0.1)
        try:
            check_cancelled()
            before = versions()
            page_counts = {"original": 0, "translated": 0}
            if genoffice:
                for variant in ("original", "translated"):
                    check_cancelled()
                    src = tdir / f"{variant}{ext}"
                    if not src.exists():
                        continue
                    outdir = work / variant
                    outdir.mkdir(parents=True, exist_ok=True)
                    proc = process_runner.run(
                        [genoffice, "render", str(src), "--out", str(outdir), "--json"],
                        timeout=CONVERT_TIMEOUT, label="GenOffice rendering", cancel_event=cancel_event,
                    )
                    try:
                        result = json.loads(proc.stdout.strip().splitlines()[-1])
                    except (ValueError, IndexError):
                        result = {}
                    if proc.returncode or result.get("status") != "ok":
                        detail = result.get("message") or proc.stderr[-300:] or proc.stdout[-300:]
                        raise RuntimeError(f"GenOffice rendering failed: {detail}")
                    files = result.get("detail", {}).get("files", [])
                    page_counts[variant] = len(files)
                    prefix = "render_" if variant == "original" else "render_tran_"
                    for item in files:
                        check_cancelled()
                        page = int(item["page"])
                        shutil.copy2(item["path"], prev_dir / f"{prefix}p{page}.png")
            else:
                pdfs = {}
                for variant in ("original", "translated"):
                    src = tdir / f"{variant}{ext}"
                    if src.exists():
                        pdfs[variant] = _convert_to_pdf(soffice, src, work, task_id, cancel_event)
                import fitz
                for variant, pdf in pdfs.items():
                    out = prev_dir / ("render_" if variant == "original" else "render_tran_")
                    with fitz.open(pdf) as doc:
                        page_counts[variant] = doc.page_count
                        for i, page in enumerate(doc):
                            check_cancelled()
                            pix = page.get_pixmap(dpi=DPI)
                            pix.save(prev_dir / f"{out.name}p{i + 1}.png")
            check_cancelled()
            if versions() != before:
                _write_status(task_id, status="none", pages=0, error=None)
            else:
                _write_status(task_id, status="ready", pages=max(page_counts.values()),
                              original_pages=page_counts["original"],
                              translated_pages=page_counts["translated"], error=None)
        finally:
            _lock.release()
    except process_runner.ProcessCancelled:
        _write_status(task_id, status="none", pages=0, error=None)
    except Exception as e:
        _write_status(task_id, status="failed", pages=0, error=str(e)[:300])
    finally:
        shutil.rmtree(work, ignore_errors=True)
        # Each LibreOffice profile can reach tens of MB; there is no other
        # cleanup path, so it must be deleted after use.
        shutil.rmtree(profile, ignore_errors=True)
        with store.task_lock(task_id):
            if not (tdir / "job.json").exists():
                shutil.rmtree(tdir, ignore_errors=True)
        with _state_lock:
            if _threads.get(task_id) is threading.current_thread():
                _threads.pop(task_id, None)
                _cancel_events.pop(task_id, None)


def page_png_path(task_id: str, page: int, variant: str) -> Path | None:
    """Cached path of a ready page image. variant: original|translated."""
    if render_status(task_id).get("status") != "ready":
        return None
    prefix = "render_" if variant == "original" else "render_tran_"
    p = store.task_dir(task_id) / "previews" / f"{prefix}p{page}.png"
    return p if p.exists() else None


def auto_render_after_done(task_id: str, ext: str):
    """Automatically trigger one high-fidelity render after translation
    completes (failures are silenced and do not affect the main flow)."""
    try:
        start_render(task_id, ext)
    except Exception:
        pass
