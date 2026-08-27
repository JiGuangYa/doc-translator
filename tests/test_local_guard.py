"""Local-guard middleware regression: Host whitelist (anti-DNS-rebinding)
+ write-action Origin validation (anti-CSRF)."""
import uuid


def test_host_whitelist_blocks_rebinding(client):
    # DNS rebinding: a malicious domain resolves to 127.0.0.1 and the Host
    # header is that domain -> reject
    r = client.get("/api/tasks", headers={"Host": "evil.example.com:8765"})
    assert r.status_code == 403
    # A valid Host is allowed (task list is empty but the request reaches the handler)
    r = client.get("/api/tasks")
    assert r.status_code == 200


def test_cross_origin_write_blocked(client):
    tid = uuid.uuid4().hex  # it doesn't matter that it doesn't exist: rejection happens before the handler
    r = client.post(f"/api/tasks/{tid}/cancel",
                    headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403

    r = client.post(f"/api/tasks/{tid}/cancel",
                    headers={"Referer": "https://evil.example.com/attack"})
    assert r.status_code == 403


def test_same_origin_write_allowed(client):
    tid = uuid.uuid4().hex
    r = client.post(f"/api/tasks/{tid}/cancel",
                    headers={"Origin": "http://127.0.0.1:8765"})
    # After passing the guard the business layer returns 404 (task does not
    # exist), proving the guard did not block it
    assert r.status_code == 404


def test_non_browser_write_without_origin_allowed(client):
    """Non-browser clients like curl send no Origin header, so they are
    allowed (a reasonable trade-off for local tooling)."""
    tid = uuid.uuid4().hex
    r = client.post(f"/api/tasks/{tid}/cancel")
    assert r.status_code == 404


def test_cross_origin_get_not_blocked(client):
    """Read actions are not subject to Origin validation (no side effects;
    leak prevention is handled by the same-origin policy and Host whitelist)."""
    tid = uuid.uuid4().hex
    r = client.get(f"/api/tasks/{tid}", headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 404  # reaches the handler: task does not exist
