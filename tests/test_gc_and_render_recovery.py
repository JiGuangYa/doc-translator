"""GC and render-state recovery regression: profile is deleted after use,
stale rendering is reset, TTL cleanup."""
import json
import time
import uuid

from app import config, store
from app.services import renderer


def _mk_task(tasks_dir, status, updated_at):
    tid = uuid.uuid4().hex
    d = tasks_dir / tid
    d.mkdir(parents=True)
    (d / "job.json").write_text(json.dumps({
        "task_id": tid, "filename": "a.docx", "ext": ".docx",
        "status": status, "updated_at": updated_at,
    }), encoding="utf-8")
    (d / "original.docx").write_bytes(b"x")
    return tid


def test_recover_stale_resets_rendering(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path)
    tid = uuid.uuid4().hex
    f = tmp_path / tid / "previews" / "render.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"status": "rendering", "pages": 0}), encoding="utf-8")

    renderer.recover_stale()
    st = json.loads(f.read_text(encoding="utf-8"))
    assert st["status"] == "none"

    # The "ready" state is unaffected
    f2 = tmp_path / uuid.uuid4().hex / "previews" / "render.json"
    f2.parent.mkdir(parents=True)
    f2.write_text(json.dumps({"status": "ready", "pages": 3}), encoding="utf-8")
    renderer.recover_stale()
    assert json.loads(f2.read_text(encoding="utf-8"))["status"] == "ready"


def test_render_task_removes_profile(tmp_path, monkeypatch):
    """After rendering ends (including on failure), the LO profile directory
    must be deleted, otherwise long-running instances accumulate several GB."""
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    tid = _mk_task(tmp_path, "done", time.strftime("%Y-%m-%d %H:%M:%S"))

    def fake_convert(soffice, src, outdir, tag):
        profile = renderer._profile_dir(tag)
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "user").write_text("lo profile junk")
        return outdir / (src.stem + ".pdf")  # fake pdf -> fitz will fail to open, hitting the exception path

    monkeypatch.setattr(renderer, "soffice_path", lambda: "soffice-fake")
    monkeypatch.setattr(renderer, "_convert_to_pdf", fake_convert)

    renderer._render_task(tid, ".docx")
    assert not renderer._profile_dir(tid).exists(), "profile directory must be cleaned up in the finally block"
    st = renderer.render_status(tid)
    assert st["status"] == "failed"  # fake pdf causes failure, but cleanup still happens


def test_gc_old_tasks_only_terminal_and_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path)
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 40 * 86400))
    recent = time.strftime("%Y-%m-%d %H:%M:%S")

    t_done_old = _mk_task(tmp_path, "done", old)
    t_failed_old = _mk_task(tmp_path, "failed", old)
    t_done_recent = _mk_task(tmp_path, "done", recent)
    t_translating_old = _mk_task(tmp_path, "translating", old)

    removed = store.gc_old_tasks(30)
    assert t_done_old in removed and t_failed_old in removed
    assert t_done_recent not in removed
    assert t_translating_old not in removed, "in-progress tasks must not be GC'd"
    assert (tmp_path / t_translating_old).exists()

    # TTL=0 disables GC
    assert store.gc_old_tasks(0) == []
