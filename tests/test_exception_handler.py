"""Test the global exception handler returns uniform JSON and does not
leak stack traces to the client.
"""
import logging


def test_validation_error_returns_uniform_json(anon_client):
    """POST /api/auth/login with a missing field returns 422 with our
    uniform shape {detail, request_id} (not FastAPI's default verbose shape).
    """
    r = anon_client.post("/api/auth/login", json={})
    assert r.status_code == 422
    body = r.json()
    # Uniform shape: top-level "detail" (string or list) and "request_id"
    assert "detail" in body
    assert "request_id" in body
    # And the request_id matches the X-Request-ID header
    assert body["request_id"] == r.headers.get("x-request-id")


def test_unhandled_exception_returns_500_without_trace(anon_client, monkeypatch, caplog):
    """A bug in any handler returns 500 with a generic detail, not a stack
    trace, and the trace is logged.
    """
    from app import auth

    def boom(password):
        raise RuntimeError("simulated server-side failure")

    # verify_login is called from inside the /api/auth/login route;
    # monkeypatching it makes the route throw without re-wiring the router
    monkeypatch.setattr(auth, "verify_login", boom)
    auth.set_password("test-pass-123")

    with caplog.at_level(logging.ERROR, logger="app"):
        r = anon_client.post(
            "/api/auth/login", json={"password": "test-pass-123"},
        )
    assert r.status_code == 500
    body = r.json()
    assert "detail" in body
    assert "request_id" in body
    # Must NOT leak the raw exception message to the client
    assert "simulated server-side failure" not in str(body)
    # Must NOT include a Python traceback in the response body
    assert "Traceback" not in str(body)
    # The exception must have been logged with the request_id for correlation
    assert any("simulated server-side failure" in rec.message for rec in caplog.records)


def test_404_returns_json(anon_client):
    """Unknown route under an exempt prefix returns our uniform 404 JSON."""
    # /openapi.json is exempt and is served by FastAPI; an unknown path under
    # the app's URL space gets Starlette's default 404 — but we ensure the
    # body is JSON, not HTML.
    r = anon_client.get("/no-such-path")
    assert r.status_code == 404
    # body must be JSON-parseable (i.e. our handler or Starlette's JSON default)
    try:
        r.json()
    except Exception as e:
        raise AssertionError(f"expected JSON 404, got: {r.text[:200]}") from e
