"""Auth system regression: 401 interception, sign-in/logout flow, audit log
persistence, static asset pass-through."""
import json

from app import auth

PASSWORD = "test-pass-123"


def test_api_requires_auth(anon_client):
    r = anon_client.get("/api/tasks")
    assert r.status_code == 401
    # Write actions are also blocked (and the check happens after Origin
    # validation, so even a valid Origin is still blocked)
    r = anon_client.post("/api/upload", headers={"Origin": "http://127.0.0.1:8765"})
    assert r.status_code == 401


def test_static_and_index_allowed_without_auth(anon_client):
    """SPA index and static assets are not blocked; the frontend redirects
    to the sign-in page."""
    r = anon_client.get("/")
    assert r.status_code == 200
    r = anon_client.get("/static/js/router.js")
    assert r.status_code == 200


def test_status_endpoint_anonymous(anon_client):
    st = anon_client.get("/api/auth/status").json()
    assert st == {"configured": False, "authenticated": False}


def test_setup_then_login_flow(anon_client):
    # When no password is configured, signing in directly -> guided to error
    r = anon_client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 400

    # Password too short during setup
    r = anon_client.post("/api/auth/setup", json={"password": "short"})
    assert r.status_code == 400

    # Normal setup: plant the session cookie
    r = anon_client.post("/api/auth/setup", json={"password": PASSWORD})
    assert r.status_code == 200
    assert auth.cookie_name() in anon_client.cookies

    # Repeated setup after password is configured is rejected
    r = anon_client.post("/api/auth/setup", json={"password": "another-pass-1"})
    assert r.status_code == 400

    st = anon_client.get("/api/auth/status").json()
    assert st["configured"] is True and st["authenticated"] is True


def test_login_wrong_password(anon_client):
    auth.set_password(PASSWORD)
    r = anon_client.post("/api/auth/login", json={"password": "wrong-pass-1"})
    assert r.status_code == 401
    recs = _audit_records()
    assert any(rec["event"] == "login_fail" for rec in recs)


def test_login_success_grants_access(anon_client):
    auth.set_password(PASSWORD)
    r = anon_client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200
    assert anon_client.get("/api/tasks").status_code == 200
    recs = _audit_records()
    assert any(rec["event"] == "login_ok" for rec in recs)


def test_logout_revokes_session(client):
    token = client.cookies[auth.cookie_name()]
    assert client.post("/api/auth/logout").status_code == 200
    assert not auth.validate_session(token)
    # Manually replay the old cookie: session has been revoked
    client.cookies.set(auth.cookie_name(), token)
    assert client.get("/api/tasks").status_code == 401


def test_audit_trails_sensitive_actions(client):
    tid = "0" * 32
    client.delete(f"/api/tasks/{tid}")          # Even if not found, record delete attempt? Only success is logged
    client.put("/api/settings", json={"target_lang": "en"})
    recs = _audit_records()
    events = [rec["event"] for rec in recs]
    assert "settings_update" in events
    assert "task_delete" not in events          # Failed delete is not recorded


def _audit_records():
    try:
        lines = auth.AUDIT_FILE.read_text("utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(x) for x in lines if x.strip()]
