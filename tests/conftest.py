"""Shared fixtures: auth environment isolation + signed-in/anonymous TestClient.

All tests automatically redirect the password file and audit log to a temporary
directory to avoid polluting the real data/ directory.
"""
import pytest
from fastapi.testclient import TestClient

from app import config

TEST_PASSWORD = "test-pass-123"


@pytest.fixture(autouse=True)
def _auth_isolated(tmp_path, monkeypatch):
    from app import auth
    from app import secrets_store
    from app.services import process_runner, renderer, task_manager
    process_runner.reset_shutdown()
    renderer.reset_shutdown()
    # Generic API/format tests must not leave asynchronous GUI converters behind.
    # Renderer-specific tests explicitly call start_render/_render_task themselves.
    monkeypatch.setattr(renderer, "auto_render_after_done", lambda *_: None)
    monkeypatch.setattr(task_manager, "_jobs", {})
    monkeypatch.setattr(task_manager, "_submitted", set())
    task_manager.reset_shutdown()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    config.DATA_DIR.mkdir()
    for name, relative in (("PROVIDERS_FILE", "providers.json"),
                           ("SETTINGS_FILE", "settings.json"),
                           ("GLOSSARY_FILE", "glossary.json")):
        monkeypatch.setattr(config, name, tmp_path / "config" / relative)
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path / "tasks")
    # Automated checks never access a user's Keychain, even during backend detection.
    secrets = {}
    monkeypatch.setattr(secrets_store, "_BACKEND", None)
    monkeypatch.setattr(secrets_store.keyring, "get_password", lambda service, name: secrets.get((service, name)))
    monkeypatch.setattr(secrets_store.keyring, "set_password", lambda service, name, value: secrets.__setitem__((service, name), value))
    monkeypatch.setattr(secrets_store.keyring, "delete_password", lambda service, name: secrets.pop((service, name), None))
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setattr(auth, "AUDIT_FILE", tmp_path / "logs" / "audit.jsonl")
    auth._sessions.clear()
    # Reset slowapi in-memory counters to avoid rate-limit interference between tests
    from app.services.rate_limit import limiter
    limiter._storage.reset()
    yield
    task_manager.begin_shutdown()
    process_runner.begin_shutdown()
    renderer.shutdown()
    task_manager.wait_for_drain(1)
    task_manager.reset_shutdown()
    process_runner.reset_shutdown()
    renderer.reset_shutdown()
    auth._sessions.clear()


def _make_client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TASKS_DIR", tmp_path)
    from app.main import app
    return TestClient(app, base_url="http://127.0.0.1:8765",
                      raise_server_exceptions=False)


@pytest.fixture()
def anon_client(tmp_path, monkeypatch):
    """Not signed-in client: verifies 401 interception and sign-in flow."""
    with _make_client(monkeypatch, tmp_path) as c:
        yield c


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Signed-in client: default entry point for business API tests."""
    from app import auth
    auth.set_password(TEST_PASSWORD)
    with _make_client(monkeypatch, tmp_path) as c:
        c.cookies.set(auth.cookie_name(), auth.create_session())
        yield c
