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
    from app.services import task_manager
    monkeypatch.setattr(task_manager, "_submitted", set())
    monkeypatch.setattr(task_manager, "_jobs", {})
    from app import auth
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "auth.json")
    monkeypatch.setattr(auth, "AUDIT_FILE", tmp_path / "logs" / "audit.jsonl")
    auth._sessions.clear()
    # Reset slowapi in-memory counters to avoid rate-limit interference between tests
    from app.services.rate_limit import limiter
    limiter._storage.reset()
    yield
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
