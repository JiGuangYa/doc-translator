"""Exercise an actual companion process across parent loss and queue recovery.

Only the model call is replaced by deterministic local text. The HTTP server,
thread queue, document writer, on-disk state and native parent watcher are real.
"""

import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from docx import Document

from app import config, store
from app.services import pipeline
from tests.test_process_lifecycle import running, wait_until


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Desktop companion process tests require POSIX")

def free_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def test_parent_loss_saves_batches_and_restart_resumes_queue(tmp_path, monkeypatch):
    data = tmp_path / "engine-data"
    monkeypatch.setattr(config, "TASKS_DIR", data / "tasks")
    monkeypatch.setattr(config, "SETTINGS_FILE", data / "config/settings.json")
    monkeypatch.setattr(config, "PROVIDERS_FILE", data / "config/providers.json")
    store.save_settings({"use_translation_memory": False, "batch_max_segments": 1,
                         "batch_max_chars": 200, "concurrency_batches": 1})
    store._atomic_write_json(config.PROVIDERS_FILE, {"providers": [{
        "id": "p_offline", "name": "Offline fixture", "base_url": "https://example.invalid/v1",
        "model": "fixture", "enabled": True}]})
    identifiers = ["1" * 32, "2" * 32]
    for index, identifier in enumerate(identifiers):
        source = tmp_path / f"{index}.docx"
        document = Document()
        paragraphs = (["First task paragraph one", "First task paragraph two", "First task paragraph three"]
                      if index == 0 else ["Queued task paragraph"])
        for text in paragraphs:
            document.add_paragraph(text)
        document.save(source)
        job = pipeline.parse_upload(identifier, source.name, source, {})
        job.update(status="queued", provider_id="p_offline", source_lang="en", target_lang="fr",
                   created_at=f"2026-10-02 10:00:0{index}")
        store.save_job(identifier, job)

    entry = Path(__file__).resolve().parents[1] / "macos/engine_launcher.py"
    marker, calls = tmp_path / "inflight", tmp_path / "model-calls.jsonl"
    wrapper = tmp_path / "engine.py"
    wrapper.write_text('''import json, os, runpy, time
from pathlib import Path
from types import SimpleNamespace
from app import secrets_store
from app.services import renderer
from app.services.llm import client
secrets_store._BACKEND = 'fernet'
renderer.auto_render_after_done = lambda *args: None
def completion(provider_id, **kwargs):
    prompt = kwargs['messages'][-1]['content']
    payload = json.loads(prompt.split('<user_content>\\n', 1)[1].split('\\n</user_content>', 1)[0])
    with open(os.environ['TEST_CALLS'], 'a') as file:
        file.write(json.dumps(payload) + '\\n')
        file.flush()
        os.fsync(file.fileno())
    if os.environ.get('TEST_SLOW') == '1' and 'First task paragraph two' in payload.values():
        Path(os.environ['TEST_MARKER']).write_text('inflight')
        time.sleep(1.2)
    callback = kwargs.get('_usage_callback')
    if callback: callback(10, 5)
    result = json.dumps({key:'FR: ' + text for key,text in payload.items()})
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=result))])
client.chat_completion_with_metrics = completion
runpy.run_path(os.environ['TEST_ENTRY'], run_name='__main__')
''')
    port = free_port()
    environment = dict(os.environ, DOC_TRANSLATOR_DATA_DIR=str(data), DOC_TRANSLATOR_DESKTOP="1",
        DOC_TRANSLATOR_PORT=str(port), TEST_ENTRY=str(entry), TEST_MARKER=str(marker),
        TEST_CALLS=str(calls), TEST_SLOW="1", PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    pid_file = tmp_path / "engine.pid"
    owner_code = '''import json, os, subprocess, sys, time
from pathlib import Path
environment = dict(os.environ, DOC_TRANSLATOR_PARENT_PID=str(os.getpid()))
child = subprocess.Popen([sys.executable, sys.argv[1]], env=environment, stdin=subprocess.PIPE)
child.stdin.write((json.dumps({"token": "test-session-012345678901234567890123456789", "data_dir": environment["DOC_TRANSLATOR_DATA_DIR"], "port": int(environment["DOC_TRANSLATOR_PORT"])}) + "\\n").encode())
child.stdin.flush()
Path(sys.argv[2]).write_text(str(child.pid))
time.sleep(30)
'''
    log = (tmp_path / "engine.log").open("w")
    owner = subprocess.Popen([sys.executable, "-c", owner_code, str(wrapper), str(pid_file)],
                             env=environment, stdout=log, stderr=log)
    restarted = None
    try:
        wait_until(marker.exists, timeout=8)
        engine_pid = int(pid_file.read_text())
        first = store.load_job(identifiers[0])
        assert first["translations"].get("s000000") == "FR: First task paragraph one", "A fast completed batch was not saved"
        owner.kill()
        owner.wait(timeout=3)
        wait_until(lambda: not running(engine_pid), timeout=8)
        paused = store.load_job(identifiers[0])
        assert paused["status"] == "paused"
        assert paused["translations"]["s000001"] == "FR: First task paragraph two"
        assert not paused["translations"].get("s000002")
        assert store.load_job(identifiers[1])["status"] == "queued"

        environment.update(TEST_SLOW="0", DOC_TRANSLATOR_PARENT_PID="0")
        restarted = subprocess.Popen([sys.executable, str(wrapper)], env=environment, stdout=log, stderr=log, stdin=subprocess.PIPE)
        restarted.stdin.write((json.dumps({"token": "test-session-012345678901234567890123456789", "data_dir": str(data), "port": port}) + "\n").encode())
        restarted.stdin.flush()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        def request(path, body=None):
            payload = None if body is None else json.dumps(body).encode()
            query = urllib.request.Request(f"http://127.0.0.1:{port}" + path, data=payload,
                                           headers={"Content-Type": "application/json", "X-DocTranslator-Token": "test-session-012345678901234567890123456789"})
            with opener.open(query, timeout=2) as response:
                return json.load(response)
        def healthy():
            try:
                return bool(request("/healthz"))
            except (OSError, ValueError):
                return False
        wait_until(healthy, timeout=8)
        request("/api/auth/setup", {"password": "integration-only-password"})
        request(f"/api/tasks/{identifiers[0]}/start", {
            "provider_id": "p_offline", "source_lang": "en", "target_lang": "fr"})
        wait_until(lambda: all(store.load_job(task)["status"] == "done" for task in identifiers), timeout=8)
        history = [value for line in calls.read_text().splitlines() for value in json.loads(line).values()]
        assert len(history) == 4 and len(set(history)) == 4, "Restart re-sent a completed paragraph"
        finished = store.load_job(identifiers[0])
        assert finished["usage"] == {"prompt_tokens": 30, "completion_tokens": 15}
        output = Document(store.task_dir(identifiers[0]) / "translated.docx")
        assert [paragraph.text for paragraph in output.paragraphs] == [
            "FR: First task paragraph one", "FR: First task paragraph two", "FR: First task paragraph three"]
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=3)
        if restarted and restarted.poll() is None:
            restarted.terminate()
            try:
                restarted.wait(timeout=5)
            except subprocess.TimeoutExpired:
                restarted.kill()
                restarted.wait(timeout=3)
        if pid_file.exists() and running(int(pid_file.read_text())):
            os.kill(int(pid_file.read_text()), 9)
        log.close()
