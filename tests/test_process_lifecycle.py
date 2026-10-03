"""Real owned subprocesses must not outlive cancellation, timeout or their engine."""

import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from app import config, store
from app.services import process_runner, renderer


def wait_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate(), "Condition did not become true before its deadline"


def running(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
    return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


def tree_command(tmp_path):
    helper = tmp_path / "tree.py"
    pids = tmp_path / "pids.json"
    helper.write_text('''import json, os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"])
with open(sys.argv[1], "w") as file:
    json.dump([os.getppid(), os.getpid(), child.pid], file)
time.sleep(30)
''')
    return [sys.executable, str(helper), str(pids)], pids


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group validation")
@pytest.mark.parametrize("reason", ["timeout", "cancel"])
def test_converter_tree_is_reaped_on_timeout_or_cancel(tmp_path, reason):
    command, pids = tree_command(tmp_path)
    event = threading.Event()
    timer = threading.Timer(0.7, event.set) if reason == "cancel" else None
    if timer:
        timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="cancelled" if timer else "timed out"):
            process_runner.run(command, timeout=10 if timer else 0.7,
                               label="Test converter", cancel_event=event)
        assert time.monotonic() - started < 3
        assert pids.exists(), "The child tree must actually start for this test to be meaningful"
        wait_until(lambda: all(not running(pid) for pid in json.loads(pids.read_text())))
        assert process_runner.active_count() == 0
    finally:
        if timer:
            timer.cancel()
        if pids.exists():
            for pid in json.loads(pids.read_text()):
                if running(pid):
                    os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(os.name == "nt", reason="POSIX reparenting validation")
def test_converter_tree_exits_when_engine_is_killed(tmp_path):
    command, pids = tree_command(tmp_path)
    code = "from app.services.process_runner import run; run(" + repr(command) + ", timeout=30, label='Test converter')"
    owner = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_until(pids.exists)
        owned_pids = json.loads(pids.read_text())
        assert all(running(pid) for pid in owned_pids)
        owner.kill()
        owner.wait(timeout=3)
        wait_until(lambda: all(not running(pid) for pid in owned_pids))
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=3)
        if pids.exists():
            for pid in json.loads(pids.read_text()):
                if running(pid):
                    os.kill(pid, signal.SIGKILL)


def test_managed_stdin_stdout_and_exit_code():
    result = process_runner.run([sys.executable, "-c", "import sys; print(sys.stdin.read()); sys.exit(7)"],
                                timeout=3, label="Text conversion", input_text="中文 input")
    assert result.returncode == 7 and result.stdout.strip() == "中文 input"
    assert process_runner.active_count() == 0


def test_shutdown_cancels_active_and_waiting_previews(tmp_path, monkeypatch):
    command, pids = tree_command(tmp_path)
    import shlex
    launcher = tmp_path / "fake-converter"
    launcher.write_text("#!/bin/sh\nexec " + " ".join(shlex.quote(part) for part in command) + "\n")
    launcher.chmod(0o700)
    monkeypatch.setattr(renderer, "genoffice_path", lambda: str(launcher))
    for index in (1, 2):
        task_id = f"{index:032x}"
        store.save_job(task_id, {"task_id": task_id, "ext": ".docx", "filename": "test.docx", "status": "done"})
        (store.task_dir(task_id) / "original.docx").write_bytes(b"test")
        renderer.start_render(task_id, ".docx")
    wait_until(pids.exists)
    renderer.shutdown()
    assert not any(thread.is_alive() for thread in renderer._threads.values())
    assert process_runner.active_count() == 0
    wait_until(lambda: all(not running(pid) for pid in json.loads(pids.read_text())))
    for index in (1, 2):
        assert renderer.render_status(f"{index:032x}")["status"] == "none"


def test_preview_includes_added_translation_pages(tmp_path, monkeypatch):
    import pymupdf as fitz
    task_id = "d" * 32
    store.save_job(task_id, {"task_id": task_id, "ext": ".docx", "filename": "test.docx"})
    directory = store.task_dir(task_id)
    for variant in ("original", "translated"):
        (directory / f"{variant}.docx").write_bytes(b"fixture")
    monkeypatch.setattr(renderer, "genoffice_path", lambda: None)
    monkeypatch.setattr(renderer, "soffice_path", lambda: "fake")

    def convert(_executable, source, output, _tag, _cancel=None):
        output.mkdir(parents=True, exist_ok=True)
        destination = output / (source.stem + ".pdf")
        with fitz.open() as document:
            for _ in range(1 if source.stem == "original" else 3):
                document.new_page(width=100, height=100)
            document.save(destination)
        return destination

    monkeypatch.setattr(renderer, "_convert_to_pdf", convert)
    renderer._render_task(task_id, ".docx")
    status = renderer.render_status(task_id)
    assert status["status"] == "ready"
    assert (status["pages"], status["original_pages"], status["translated_pages"]) == (3, 1, 3)
    assert renderer.page_png_path(task_id, 3, "translated")
    assert renderer.page_png_path(task_id, 3, "original") is None


def test_input_change_during_preview_requests_fresh_render(tmp_path, monkeypatch):
    task_id = "e" * 32
    store.save_job(task_id, {"task_id": task_id, "ext": ".docx", "filename": "test.docx"})
    source = store.task_dir(task_id) / "original.docx"
    source.write_bytes(b"before")
    monkeypatch.setattr(renderer, "genoffice_path", lambda: "fake")

    def convert(*args, **kwargs):
        source.write_bytes(b"changed while rendering")
        return subprocess.CompletedProcess([], 0, json.dumps({"status": "ok", "detail": {"files": []}}), "")

    monkeypatch.setattr(process_runner, "run", convert)
    renderer._render_task(task_id, ".docx")
    assert renderer.render_status(task_id)["status"] == "none"
    assert not (config.TASKS_DIR / task_id / "previews/_render_work").exists()


def test_legacy_preview_cache_is_refreshed_for_page_counts(monkeypatch):
    task_id = "f" * 32
    store.save_job(task_id, {"task_id": task_id, "ext": ".docx", "filename": "test.docx"})
    monkeypatch.setattr(renderer, "genoffice_path", lambda: "fake")
    renderer._write_status(task_id, status="ready", pages=1)
    assert renderer.render_status(task_id)["status"] == "none"
    renderer._write_status(task_id, status="ready", pages=4, original_pages=1, translated_pages=4)
    assert renderer.render_status(task_id)["pages"] == 4
