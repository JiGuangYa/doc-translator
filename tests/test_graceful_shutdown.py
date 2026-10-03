"""Test graceful shutdown helpers."""
import time
import threading

from app.services import task_manager


def test_count_in_flight_initially_zero():
    """When no tasks are submitted, count_in_flight() returns 0."""
    task_manager._submitted.clear()
    assert task_manager.count_in_flight() == 0


def test_count_in_flight_tracks_submitted():
    """try_mark_submitted increments, mark_finished decrements."""
    task_manager._submitted.clear()
    assert task_manager.try_mark_submitted("test_task_a") is True
    assert task_manager.count_in_flight() == 1
    # Re-marking the same task returns False (CAS guard)
    assert task_manager.try_mark_submitted("test_task_a") is False
    task_manager.mark_finished("test_task_a")
    assert task_manager.count_in_flight() == 0


def test_wait_for_drain_returns_immediately_when_empty():
    task_manager._submitted.clear()
    start = time.monotonic()
    ok = task_manager.wait_for_drain(timeout_s=2.0)
    elapsed = time.monotonic() - start
    assert ok is True
    assert elapsed < 0.5  # no sleeping when empty


def test_wait_for_drain_returns_true_when_drained():
    """If a task finishes within the timeout, drain returns True."""
    task_manager._submitted.clear()
    task_manager.try_mark_submitted("test_task_b")

    def finish_soon():
        time.sleep(0.3)
        task_manager.mark_finished("test_task_b")

    threading.Thread(target=finish_soon, daemon=True).start()
    ok = task_manager.wait_for_drain(timeout_s=3.0)
    assert ok is True


def test_wait_for_drain_times_out():
    """If a task never finishes, drain returns False after the timeout."""
    task_manager._submitted.clear()
    task_manager.try_mark_submitted("test_task_c")
    start = time.monotonic()
    ok = task_manager.wait_for_drain(timeout_s=0.5)
    elapsed = time.monotonic() - start
    assert ok is False
    assert 0.4 < elapsed < 1.0
    # cleanup
    task_manager._submitted.discard("test_task_c")


def test_is_shutting_down_flag():
    """main.is_shutting_down() reflects the global flag."""
    from app import main
    main._shutting_down = False
    assert main.is_shutting_down() is False
    main._shutting_down = True
    assert main.is_shutting_down() is True
    main._shutting_down = False


def test_drain_waits_until_failure_state_is_saved(monkeypatch):
    from app import store
    task_id = "f" * 32
    store.save_job(task_id, {"task_id": task_id, "filename": "test.docx", "ext": ".docx", "status": "translating"})
    task_manager.try_mark_submitted(task_id)
    original_save = store.save_job
    observed = []
    def save(task, job):
        observed.append(task_manager.count_in_flight())
        original_save(task, job)
    monkeypatch.setattr(store, "save_job", save)
    task_manager.begin_shutdown()
    task_manager._wrap(lambda: (_ for _ in ()).throw(RuntimeError("conversion cancelled")), task_id)
    assert observed == [1]
    assert task_manager.count_in_flight() == 0
    assert store.load_job(task_id)["status"] == "paused"
