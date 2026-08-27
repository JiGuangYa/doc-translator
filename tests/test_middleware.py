"""Test middleware: request ID, security headers, metrics labels, and
the /healthz, /readyz, /metrics system endpoints.
"""
from app import metrics


def test_request_id_header_present(anon_client):
    """Every response includes an X-Request-ID header."""
    r = anon_client.get("/healthz")
    assert "x-request-id" in {k.lower() for k in r.headers.keys()}
    rid = r.headers["x-request-id"]
    assert len(rid) >= 8  # either inbound or freshly generated


def test_request_id_propagates(anon_client):
    """An inbound X-Request-ID is echoed back."""
    rid = "my-trace-12345"
    r = anon_client.get("/healthz", headers={"X-Request-ID": rid})
    assert r.headers["x-request-id"] == rid


def test_security_headers_on_api(anon_client):
    """API responses include nosniff, frame-options, referrer-policy."""
    r = anon_client.get("/healthz")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("referrer-policy") == "no-referrer"


def test_security_headers_present_on_index(anon_client):
    """The SPA index page is a document — security headers apply."""
    r = anon_client.get("/")
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("x-content-type-options") == "nosniff"


def test_healthz_anonymous_ok(anon_client):
    r = anon_client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_readyz_anonymous_ok(anon_client, tmp_path, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    r = anon_client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_metrics_endpoint_anonymous(anon_client):
    r = anon_client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    # core metrics are present
    assert "process_start_time_seconds" in body
    # hitting /healthz at least once already incremented the http counter
    assert "http_requests_total" in body


def test_metrics_increments_after_request(anon_client):
    """A new HTTP request must produce a new sample for the matched route."""
    before = metrics.http_requests_total.labels(
        method="GET", path="/healthz", status="200"
    )._value.get()
    anon_client.get("/healthz")
    after = metrics.http_requests_total.labels(
        method="GET", path="/healthz", status="200"
    )._value.get()
    assert after >= before + 1
