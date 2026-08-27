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
from pathlib import Path

from .. import config, store

# Only one soffice process runs at a time (concurrent conversions can lock the profile)
_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}

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


def available() -> bool:
    return soffice_path() is not None


# ---- status ----

def _profile_dir(task_tag: str) -> Path:
    return Path(config.DATA_DIR) / "_lo_profile" / task_tag


def _write_status_file(f: Path, cur: dict):
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False), "utf-8")
    tmp.replace(f)


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
            return json.loads(f.read_text("utf-8"))
        except Exception:
            pass
    return {"status": "none", "pages": 0, "error": None}


def start_render(task_id: str, ext: str) -> dict:
    """Render both versions of the task in a background thread; if a render
    is already running or finished, just return the current status."""
    st = render_status(task_id)
    if st["status"] in ("ready", "rendering"):
        return st
    t = _threads.get(task_id)
    if t and t.is_alive():
        return st
    _write_status(task_id, status="rendering", pages=0)
    th = threading.Thread(target=_render_task, args=(task_id, ext), daemon=True,
                          name=f"render-{task_id[:8]}")
    _threads[task_id] = th
    th.start()
    return {"status": "rendering", "pages": 0, "error": None}


def _write_status(task_id: str, **kw):
    f = _status_file(task_id)
    cur = {}
    try:
        cur = json.loads(f.read_text("utf-8"))
    except Exception:
        pass
    cur.update(kw)
    _write_status_file(f, cur)


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
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.communicate()   # reap the process to avoid zombies
        raise RuntimeError(f"LibreOffice conversion timed out (>{timeout}s); process tree was force-killed") from None
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
    tdir = store.task_dir(task_id)
    prev_dir = tdir / "previews"
    work = tdir / "previews" / "_render_work"
    profile = _profile_dir(task_id)
    try:
        soffice = soffice_path()
        if not soffice:
            raise RuntimeError("LibreOffice is not installed on the server")
        with _lock:
            pdfs = {}
            for variant in ("original", "translated"):
                src = tdir / f"{variant}{ext}"
                if not src.exists():
                    continue
                pdfs[variant] = _convert_to_pdf(soffice, src, work, task_id)

            import fitz
            pages = 0
            for variant, pdf in pdfs.items():
                out = prev_dir / ("render_" if variant == "original" else "render_tran_")
                with fitz.open(pdf) as doc:
                    if variant == "original":
                        pages = doc.page_count
                    for i, page in enumerate(doc):
                        pix = page.get_pixmap(dpi=DPI)
                        pix.save(prev_dir / f"{out.name}p{i + 1}.png")
            _write_status(task_id, status="ready", pages=pages)
    except Exception as e:
        _write_status(task_id, status="failed", pages=0, error=str(e)[:300])
    finally:
        shutil.rmtree(work, ignore_errors=True)
        # Each LibreOffice profile can reach tens of MB; there is no other
        # cleanup path, so it must be deleted after use.
        shutil.rmtree(profile, ignore_errors=True)


def page_png_path(task_id: str, page: int, variant: str) -> Path | None:
    """Cached path of a ready page image. variant: original|translated."""
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
