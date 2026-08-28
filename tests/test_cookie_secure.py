"""Test that session cookies respect DOC_TRANSLATOR_COOKIE_SECURE."""


def test_cookie_secure_off_by_default(anon_client, monkeypatch):
    """Scenario A default: Secure flag not set (HTTP loopback)."""
    monkeypatch.delenv("DOC_TRANSLATOR_COOKIE_SECURE", raising=False)
    # init password
    r = anon_client.post("/api/auth/setup", json={"password": "test-pass-123"})
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "Secure" not in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie.lower() or "samesite=strict" in set_cookie.lower()


def test_cookie_secure_on_when_env_set(anon_client, monkeypatch):
    """When DOC_TRANSLATOR_COOKIE_SECURE=1, the Secure flag must appear."""
    monkeypatch.setenv("DOC_TRANSLATOR_COOKIE_SECURE", "1")
    r = anon_client.post("/api/auth/setup", json={"password": "test-pass-123"})
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "Secure" in set_cookie


def test_login_cookie_respects_flag(anon_client, monkeypatch):
    """Login endpoint must also emit Secure when env is set."""
    from app import auth
    auth.set_password("test-pass-123")
    monkeypatch.setenv("DOC_TRANSLATOR_COOKIE_SECURE", "1")
    r = anon_client.post("/api/auth/login", json={"password": "test-pass-123"})
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    assert "Secure" in set_cookie
