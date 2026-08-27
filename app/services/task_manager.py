"""Task runtime: thread pool, state machine, progress, cancel flag.

State machine: uploaded → parsing → pending_confirm → translating → done | failed | cancelled
Tasks still in a running state after a service restart are marked failed (resume supported).
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .. import store

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="task")
_jobs: dict[str, dict] = {}          # task_id -> runtime state (in-memory)
_submitted: set[str] = set()         # Submitted but not yet finished — prevents duplicate submission.
_lock = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def register(job: dict) -> None:
    """Register a task after upload and parse complete."""
    with _lock:
        _jobs[job["task_id"]] = {
            "status": job.get("status", "pending_confirm"),
            "done_segments": 0,
            "total_segments": job.get("segment_count", 0),
            "current_batch": 0,
            "total_batches": 0,
            "error": None,
            "cancel_requested": False,
        }


def recover_on_startup() -> None:
    """Mark tasks in an intermediate state in the store as failed (interrupted by service restart)."""
    for job in store.list_jobs():
        if job.get("status") in ("translating", "parsing", "uploaded"):
            job["status"] = "failed"
            job["error"] = "Service restart interrupted the task. You can restart translation (resume supported)."
            store.save_job(job["task_id"], job)


def get_runtime(task_id: str) -> dict | None:
    with _lock:
        rt = _jobs.get(task_id)
        if rt:
            return {k: v for k, v in rt.items() if k != "cancel_requested"}
        return None


def update(task_id: str, **fields) -> None:
    with _lock:
        rt = _jobs.setdefault(task_id, {})
        rt.update(fields)


def request_cancel(task_id: str) -> bool:
    with _lock:
        rt = _jobs.get(task_id)
        if not rt or rt.get("status") != "translating":
            return False
        rt["cancel_requested"] = True
        return True


def is_cancel_requested(task_id: str) -> bool:
    with _lock:
        return bool(_jobs.get(task_id, {}).get("cancel_requested"))


def try_mark_submitted(task_id: str) -> bool:
    """CAS-style placeholder: returns False if the task is already submitted and unfinished.

    start_translation's "check job state, then submit" has a check-then-act window:
    while a task is queued in the thread pool, job.json already says translating but the
    worker hasn't started, so double-clicking "Start translation" passes the check twice,
    burns tokens twice, and clobbers results.
    """
    with _lock:
        if task_id in _submitted:
            return False
        _submitted.add(task_id)
        return True


def mark_finished(task_id: str) -> None:
    with _lock:
        _submitted.discard(task_id)


def submit(fn, task_id: str) -> None:
    """Submit a task to the thread pool."""
    _executor.submit(_wrap, fn, task_id)


def _wrap(fn, task_id: str):
    try:
        fn()
    except Exception as e:  # Safety net: any uncaught exception becomes a failed task.
        update(task_id, status="failed", error=str(e)[:500])
        job = store.load_job(task_id)
        if job:
            job["status"] = "failed"
            job["error"] = str(e)[:500]
            job["updated_at"] = _now()
            store.save_job(task_id, job)


def persist_status(task_id: str, **fields) -> None:
    """Atomically write status/progress back to job.json (read-modify-write under a lock,
    so concurrent writers don't clobber each other)."""
    def mut(job: dict):
        job.update(fields)
        job["updated_at"] = _now()
    store.update_job(task_id, mut)


def count_in_flight() -> int:
    """Number of tasks currently executing or queued."""
    with _lock:
        return len(_submitted)


def wait_for_drain(timeout_s: float) -> bool:
    """Wait until all tasks finish; returns True if drained within timeout, False if it timed out.

    Used for graceful shutdown: after SIGTERM, refuse new tasks and wait for in-flight
    batches to flush to disk.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if count_in_flight() == 0:
            return True
        time.sleep(0.5)
    return count_in_flight() == 0
