"""Frequent task-list reads stay small and recover from stale summary files."""

import json
import os
import time

from app import config, store


def test_summary_tracks_progress_and_falls_back_to_full_job(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    task_id = "e" * 32
    store.save_job(task_id, {
        "task_id": task_id, "filename": "report.docx", "ext": ".docx",
        "status": "translating", "created_at": "2026-01-01 12:00:00",
        "segments": [{"seg_id": "s000000", "text": "One", "translatable": True},
                     {"seg_id": "s000001", "text": "Two", "translatable": True}],
        "translations": {}, "segment_count": 2,
    })
    summary = store.list_job_headers()[0]
    assert summary["untranslated_count"] == 2
    assert "segments" not in summary
    store.update_job(task_id, lambda job: job["translations"].update({"s000000": "一"}))
    assert store.list_job_headers()[0]["untranslated_count"] == 1

    # Simulate a crash between writing authoritative job.json and summary.json.
    job_path = store.task_dir(task_id) / "job.json"
    full = json.loads(job_path.read_text())
    full["status"] = "done"
    job_path.write_text(json.dumps(full))
    future = time.time() + 2
    os.utime(job_path, (future, future))
    assert store.list_job_headers()[0]["status"] == "done"
