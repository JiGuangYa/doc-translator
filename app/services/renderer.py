"""LibreOffice high-fidelity rendering: office docs -> PDF -> page PNGs.

The browser-side docx-preview is only an approximation; to make the
side-by-side view match what Office/WPS renders, the server-side
LibreOffice converts the original/translated document to PDF and then
emits per-page images (works for docx/pptx/xlsx).
"""
import json
import os
import shutil
import signal
import subprocess
import threading
import tempfile
from pathlib import Path

from .. import config, store

# Only one soffice process runs at a time (concurrent conversions can lock the profile)
_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}
_threads_lock = threading.Lock()

CONVERT_TIMEOUT = 300   # per-file conversion timeout (seconds)
DPI = 110
_processes: set[subprocess.Popen] = set()
_process_lock = threading.Lock()
_stopping = threading.Event()


def shutdown() -> None:
    _stopping.set()
    with _process_lock:
        for proc in list(_processes):
            _kill_tree(proc)



def soffice_path() -> str | None:
    """Locate the soffice executable; returns None if not found."""
    p = shutil.which("soffice") or shutil.which("libreoffice")
    if p:
        return p
    for cand in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/opt/homebrew/bin/soffice", "/usr/local/bin/soffice",
        "/usr/bin/soffice", "/opt/libreoffice/program/soffice",
    ):
        if Path(cand).exists():
            return cand
    return None


def available() -> bool:
    return soffice_path() is not None


# ---- status ----

def _profile_dir(task_tag: str) -> Path:
    return Path(config.DATA_DIR) / "_lo_profile" / task_tag


def _write_status_file(f: Path, cur: dict):
    store._atomic_write_json(f, cur)


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
                "error": "LibreOffice is not installed on the server"}
    f = _status_file(task_id)
    if f.exists():
        try:
            result = json.loads(f.read_text("utf-8"))
            version = (store.load_job(task_id) or {}).get("content_version", 0)
            if result.get("content_version", 0) != version:
                return {"status": "none", "pages": 0, "error": None, "content_version": version}
            return result
        except Exception:
            pass
    return {"status": "none", "pages": 0, "error": None}


def start_render(task_id: str, ext: str) -> dict:
    """Register exactly one worker per task, including its final publication."""
    with store.task_lock(task_id), _threads_lock:
        st = render_status(task_id)
        if _stopping.is_set() or st["status"] in ("ready", "unavailable"):
            return st
        job = store.load_job(task_id)
        if not job:
            return {"status": "none", "pages": 0, "error": None}
        thread = _threads.get(task_id)
        if thread and thread.is_alive():
            return {**st, "status": "rendering"}
        version = job.get("content_version", 0)
        _write_status(task_id, version, status="rendering", pages=0, error=None)
        thread = threading.Thread(target=_render_worker, args=(task_id, ext), daemon=True,
                                  name=f"render-{task_id[:8]}")
        _threads[task_id] = thread
        try:
            thread.start()
        except Exception:
            _threads.pop(task_id, None)
            _write_status(task_id, version, status="failed", pages=0, error="Unable to start preview")
            raise
        return {"status": "rendering", "pages": 0, "error": None, "content_version": version}


def _render_worker(task_id: str, ext: str):
    version = None
    try:
        version = _render_task(task_id, ext)
    finally:
        with store.task_lock(task_id):
            with _threads_lock:
                if _threads.get(task_id) is threading.current_thread():
                    _threads.pop(task_id, None)
            job = store.load_job(task_id)
            if job and version is not None and job.get("content_version", 0) != version and not _stopping.is_set():
                start_render(task_id, ext)


def _write_status(task_id: str, version: int, **kw):
    # A renderer must never resurrect a deleted task or publish an old revision.
    with store.task_lock(task_id):
        job = store.load_job(task_id)
        if not job or job.get("content_version", 0) != version:
            return
        _write_status_file(_status_file(task_id), {**kw, "content_version": version})


# ---- conversion and rasterization ----

def _kill_tree(proc: subprocess.Popen):
    """Kill the entire process tree. On Windows, soffice.exe is only the
    launcher; the real work is done by the soffice.bin child process.
    Killing only the direct child leaves an orphan holding the profile
    lock, which combined with the global serialization lock can stall
    every subsequent render task."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _run_soffice(cmd: list[str], timeout: int = CONVERT_TIMEOUT) -> tuple[int, bytes, bytes]:
    """Run soffice; on timeout, kill the whole tree and raise RuntimeError.
    Returns (returncode, stdout, stderr)."""
    kwargs = {} if os.name == "nt" else {"start_new_session": True}
    with _process_lock:
        if _stopping.is_set():
            raise RuntimeError("Application is closing")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
        _processes.add(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.communicate()   # reap the process to avoid zombies
        raise RuntimeError(f"LibreOffice conversion timed out (>{timeout}s); process tree was force-killed") from None
    finally:
        with _process_lock:
            _processes.discard(proc)
    return proc.returncode, out or b"", err or b""


def _convert_to_pdf(soffice: str, src: Path, outdir: Path, task_tag: str) -> Path:
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
    code, out, err = _run_soffice(cmd)
    pdf = outdir / (src.stem + ".pdf")
    if code != 0 or not pdf.exists():
        msg = (err or out or b"").decode("utf-8", "replace")[-300:]
        raise RuntimeError(f"LibreOffice conversion failed: {msg}")
    return pdf


def _render_task(task_id: str, ext: str):
    version = None
    work = None
    profile = _profile_dir(task_id)
    try:
        soffice = soffice_path()
        if not soffice:
            return None
        with _lock:
            if _stopping.is_set():
                return None
            # Snapshot both documents together, after any queued revision has
            # committed. All expensive conversion happens outside the task lock.
            with store.task_lock(task_id):
                job = store.load_job(task_id)
                if not job:
                    return None
                version = job.get("content_version", 0)
                root = Path(config.DATA_DIR) / "_render_work"
                root.mkdir(parents=True, exist_ok=True)
                work = Path(tempfile.mkdtemp(prefix=f"{task_id}-", dir=root))
                inputs = work / "inputs"
                inputs.mkdir()
                for variant in ("original", "translated"):
                    source = store.document_path(job, variant)
                    if source.exists():
                        shutil.copy2(source, inputs / f"{variant}{ext}")
                _write_status(task_id, version, status="rendering", pages=0, error=None)

            def superseded():
                latest = store.load_job(task_id)
                return _stopping.is_set() or not latest or latest.get("content_version", 0) != version

            images = work / "images"
            images.mkdir()
            counts = {"original": 0, "translated": 0}
            import fitz
            for variant in ("original", "translated"):
                if superseded():
                    return version
                source = inputs / f"{variant}{ext}"
                if not source.exists():
                    continue
                pdf = _convert_to_pdf(soffice, source, work / "pdfs", task_id)
                prefix = "render_" if variant == "original" else "render_tran_"
                with fitz.open(pdf) as document:
                    counts[variant] = document.page_count
                    for i, page in enumerate(document):
                        if superseded():
                            return version
                        page.get_pixmap(dpi=DPI).save(images / f"{prefix}p{i + 1}.png")
            with store.task_lock(task_id):
                if superseded():
                    return version
                destination = store.task_dir(task_id) / "previews" / f"v{version}"
                if destination.exists():
                    shutil.rmtree(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                images.replace(destination)
                _write_status(task_id, version, status="ready", pages=max(counts.values()),
                              original_pages=counts["original"], translated_pages=counts["translated"], error=None)
    except Exception as e:
        if version is not None and not _stopping.is_set():
            _write_status(task_id, version, status="failed", pages=0, error=str(e)[:300])
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(profile, ignore_errors=True)
    return version


def page_png_path(task_id: str, page: int, variant: str) -> Path | None:
    """Cached path of a ready page image. variant: original|translated."""
    prefix = "render_" if variant == "original" else "render_tran_"
    state = render_status(task_id)
    if state["status"] != "ready":
        return None
    version = state.get("content_version", 0)
    p = store.task_dir(task_id) / "previews" / f"v{version}" / f"{prefix}p{page}.png"
    return p if p.exists() else None


def auto_render_after_done(task_id: str, ext: str):
    """Automatically trigger one high-fidelity render after translation
    completes (failures are silenced and do not affect the main flow)."""
    try:
        start_render(task_id, ext)
    except Exception:
        pass
