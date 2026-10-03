"""Concurrency-guard regression: atomic store updates, submit dedup placeholder,
and revision rejection while translating."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import config, store


def _mk_job_file(task_dir, status="pending_confirm"):
    (task_dir / "job.json").write_text(json.dumps({
        "task_id": task_dir.name, "filename": "a.docx", "ext": ".docx",
        "status": status, "translations": {"s000000": "old translation"},
    }), encoding="utf-8")


def _mk_job(tmp_path, status="pending_confirm"):
    tid = uuid.uuid4().hex
    d = tmp_path / tid
    d.mkdir(parents=True)
    _mk_job_file(d, status=status)
    return tid


def test_update_job_atomic_mutate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path)
    tid = _mk_job(tmp_path)

    def mut(job):
        job["translations"]["s000001"] = "new translation"
        job["status"] = "done"

    out = store.update_job(tid, mut)
    assert out["status"] == "done"
    # Persisted content contains both modifications and no segments are lost
    on_disk = json.loads((tmp_path / tid / "job.json").read_text(encoding="utf-8"))
    assert on_disk["translations"] == {"s000000": "old translation", "s000001": "new translation"}


def test_update_job_missing_task_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path)
    assert store.update_job("f" * 32, lambda j: None) is None


def test_try_mark_submitted_dedup():
    from app.services import task_manager
    tid = uuid.uuid4().hex
    try:
        assert task_manager.try_mark_submitted(tid) is True
        assert task_manager.try_mark_submitted(tid) is False, "duplicate submission must be blocked by the CAS placeholder"
    finally:
        task_manager.mark_finished(tid)
    assert task_manager.try_mark_submitted(tid) is True, "after the previous one finishes, another submission should be allowed"
    task_manager.mark_finished(tid)


def test_revise_rejected_while_translating(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path)
    from app.services import pipeline, task_manager
    tid = _mk_job(tmp_path, status="translating")
    task_manager.register({"task_id": tid, "segment_count": 1})
    task_manager.update(tid, status="translating")
    try:
        with pytest.raises(ValueError, match="(?i)translating"):
            pipeline.revise_segment(tid, "s000000", "edit")
    finally:
        task_manager.update(tid, status="pending_confirm")


def test_revise_api_maps_valueerror_to_400(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path)
    from app.main import app
    from app.services import task_manager
    tid = _mk_job(tmp_path, status="translating")
    with TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False) as c:
        from app import auth
        auth.set_password("test-pass-123")
        c.cookies.set(auth.cookie_name(), auth.create_session())
        # Startup recovery flips any on-disk "translating" to "failed",
        # so we must re-set the state after that.
        _mk_job_file(tmp_path / tid, status="translating")
        task_manager.register({"task_id": tid, "segment_count": 1})
        task_manager.update(tid, status="translating")
        try:
            resp = c.patch(f"/api/tasks/{tid}/segments/s000000", json={"text": "edit"})
            assert resp.status_code == 400
            assert "translating" in resp.text.lower()
        finally:
            task_manager.update(tid, status="pending_confirm")


def test_start_translation_duplicate_submit_rejected(tmp_path, monkeypatch):
    """Within the queuing window (job is already translating but the worker
    has not yet run), a second start must be rejected."""
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path)
    from app.services import pipeline, task_manager

    tid = _mk_job(tmp_path, status="translating")  # simulate a submitted-but-not-finished state
    task_manager.try_mark_submitted(tid)

    captured = []
    monkeypatch.setattr(task_manager, "submit", lambda fn, t: captured.append(fn))
    with pytest.raises(ValueError, match="(?i)already been submitted"):
        pipeline.start_translation(tid, "mock", "auto", "zh-CN")
    assert not captured, "a rejected request must not enter the thread pool"
    task_manager.mark_finished(tid)
