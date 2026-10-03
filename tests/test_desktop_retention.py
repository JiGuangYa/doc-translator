"""Desktop retention and recoverable deletion behavior."""

import json
import time

from app import config, store


def test_retention_update_keeps_unmentioned_provider_selection(client, tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    settings_file.write_text(json.dumps({"translation_provider_id": "p_existing"}))
    response = client.put("/api/settings", json={"task_retention_days": 0})
    assert response.status_code == 200
    settings = response.json()["settings"]
    assert settings["task_retention_days"] == 0
    assert settings["translation_provider_id"] == "p_existing"


def test_delete_moves_task_to_recoverable_trash(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    task_id = "a" * 32
    store.save_job(task_id, {"task_id": task_id, "filename": "review.pdf",
                             "ext": ".pdf", "status": "done", "segments": []})
    (store.task_dir(task_id) / "original.pdf").write_bytes(b"original")
    response = client.delete(f"/api/tasks/{task_id}")
    assert response.status_code == 200
    assert store.load_job(task_id) is None
    assert (tmp_path / "trash" / task_id / "original.pdf").read_bytes() == b"original"
    assert client.get("/api/trash").json()["tasks"][0]["task_id"] == task_id

    restored = client.post(f"/api/trash/{task_id}/restore")
    assert restored.status_code == 200
    assert (store.task_dir(task_id) / "original.pdf").read_bytes() == b"original"


def test_preview_cleanup_never_removes_documents(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    task_id = "b" * 32
    store.save_job(task_id, {"task_id": task_id, "filename": "report.docx",
                             "ext": ".docx", "status": "done", "segments": []})
    original = store.task_dir(task_id) / "original.docx"
    original.write_bytes(b"original")
    preview = store.task_dir(task_id) / "previews"
    preview.mkdir()
    page = preview / "render_p1.png"
    page.write_bytes(b"generated")
    old = time.time() - 10 * 86400
    import os
    os.utime(page, (old, old))
    assert store.gc_old_previews(7) == [task_id]
    assert original.read_bytes() == b"original"
    assert not preview.exists()
