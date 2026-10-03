"""Bounded, cancellable ownership of converter processes and their descendants."""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_active: set[subprocess.Popen] = set()
_stopping = False


class ProcessCancelled(RuntimeError):
    pass


def reset_shutdown() -> None:
    global _stopping
    with _lock:
        _stopping = False


def begin_shutdown() -> None:
    global _stopping
    with _lock:
        _stopping = True
        processes = list(_active)
    for process in processes:
        if process.poll() is None:
            process.terminate()


def active_count() -> int:
    with _lock:
        return len(_active)


def run(command: list[str], *, timeout: float, label: str,
        input_text: str | None = None,
        cancel_event: threading.Event | None = None) -> subprocess.CompletedProcess:
    if getattr(sys, "frozen", False):
        owner = [sys.executable, "--supervise-child"]
    else:
        owner = [sys.executable, str(Path(__file__).with_name("process_supervisor.py"))]
    with _lock:
        if _stopping or cancel_event and cancel_event.is_set():
            raise ProcessCancelled(f"{label} cancelled")
        process = subprocess.Popen(
            owner + [str(os.getpid()), "--"] + command,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=os.name != "nt")
        _active.add(process)
    deadline = time.monotonic() + timeout
    pending_input = input_text.encode("utf-8") if input_text is not None else None
    try:
        while True:
            if _stopping or cancel_event and cancel_event.is_set():
                raise ProcessCancelled(f"{label} cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"{label} timed out after {timeout:g}s")
            try:
                out, err = process.communicate(input=pending_input, timeout=min(0.1, remaining))
                return subprocess.CompletedProcess(command, process.returncode,
                    out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
            except subprocess.TimeoutExpired:
                pending_input = None
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=2)
        with _lock:
            _active.discard(process)
