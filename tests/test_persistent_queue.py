"""Queued work survives a desktop shutdown and can be submitted again."""

from app import config, store
from app.services import pipeline, task_manager


def test_second_task_is_queued_and_survives_shutdown(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    monkeypatch.setattr(config, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(task_manager, "_submitted", set())
    monkeypatch.setattr(task_manager, "_jobs", {})
    task_manager.reset_shutdown()
    captured = []
    monkeypatch.setattr(task_manager, "submit", lambda fn, task_id:
                        captured.append(lambda: task_manager._wrap(fn, task_id)))
    for index in (1, 2):
        task_id = f"{index:032x}"
        store.save_job(task_id, {
            "task_id": task_id, "filename": f"{index}.docx", "ext": ".docx",
            "status": "pending_confirm", "segments": [], "translations": {},
            "segment_count": 0, "warnings": [],
        })
    first, second = f"{1:032x}", f"{2:032x}"
    pipeline.start_translation(first, "mock", "en", "zh-CN")
    pipeline.start_translation(second, "mock", "en", "zh-CN")
    assert store.load_job(first)["status"] == "translating"
    assert store.load_job(second)["status"] == "queued"

    task_manager.begin_shutdown()
    captured[0]()
    captured[1]()
    assert store.load_job(first)["status"] == "paused"
    assert store.load_job(second)["status"] == "queued"
    task_manager.reset_shutdown()


def test_restart_marks_only_interrupted_task_paused(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    for index, status in ((1, "translating"), (2, "queued")):
        task_id = f"{index:032x}"
        store.save_job(task_id, {
            "task_id": task_id, "filename": "sample.docx", "ext": ".docx",
            "status": status, "segments": [],
        })
    task_manager.recover_on_startup()
    assert store.load_job(f"{1:032x}")["status"] == "paused"
    assert store.load_job(f"{2:032x}")["status"] == "queued"
