"""Test app.services.ssrf_guard: opt-in blocking of base_url pointing at private IPs.

Scenario A (single-host) defaults to allow_private_endpoints=True so users can
run a local llama.cpp/Ollama on 127.0.0.1. In deployments that serve only
public providers, set DOC_TRANSLATOR_REQUIRE_PUBLIC=1 to enable the guard.
"""
import socket

from app.services import ssrf_guard


def test_allow_loopback_when_disabled(monkeypatch):
    """Default Scenario A: loopback base_url is allowed."""
    monkeypatch.delenv("DOC_TRANSLATOR_REQUIRE_PUBLIC", raising=False)
    ssrf_guard.reload_config()
    assert ssrf_guard.check_base_url("http://127.0.0.1:11434/v1") == (True, "")
    assert ssrf_guard.check_base_url("http://localhost:8080/v1") == (True, "")


def test_reject_loopback_when_enabled(monkeypatch):
    """Guard on: reject any URL that resolves to a private/loopback IP."""
    monkeypatch.setenv("DOC_TRANSLATOR_REQUIRE_PUBLIC", "1")
    ssrf_guard.reload_config()
    ok, reason = ssrf_guard.check_base_url("http://127.0.0.1:11434/v1")
    assert ok is False
    assert "loopback" in reason.lower() or "private" in reason.lower()


def test_reject_private_rfc1918(monkeypatch):
    """Guard on: reject RFC1918 ranges (10/8, 172.16/12, 192.168/16)."""
    monkeypatch.setenv("DOC_TRANSLATOR_REQUIRE_PUBLIC", "1")
    ssrf_guard.reload_config()
    ok, _ = ssrf_guard.check_base_url("http://10.0.0.5:8080/v1")
    assert ok is False
    ok, _ = ssrf_guard.check_base_url("http://192.168.1.10:8080/v1")
    assert ok is False


def test_reject_link_local(monkeypatch):
    """Guard on: reject 169.254.0.0/16 (cloud metadata)."""
    monkeypatch.setenv("DOC_TRANSLATOR_REQUIRE_PUBLIC", "1")
    ssrf_guard.reload_config()
    ok, _ = ssrf_guard.check_base_url("http://169.254.169.254/latest/meta-data/")
    assert ok is False


def test_accept_public_when_enabled(monkeypatch):
    """Guard on: still allow real public domains."""
    monkeypatch.setenv("DOC_TRANSLATOR_REQUIRE_PUBLIC", "1")
    ssrf_guard.reload_config()
    ok, _ = ssrf_guard.check_base_url("https://api.openai.com/v1")
    assert ok is True


def test_dns_resolves_to_private(monkeypatch):
    """Guard on: a public-looking hostname that resolves to a private IP is rejected."""
    monkeypatch.setenv("DOC_TRANSLATOR_REQUIRE_PUBLIC", "1")
    ssrf_guard.reload_config()

    def fake_getaddrinfo(host, port, *a, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    ok, reason = ssrf_guard.check_base_url("https://evil.example.com/v1")
    assert ok is False
    assert "127.0.0.1" in reason or "loopback" in reason.lower()


def test_invalid_scheme(monkeypatch):
    """Guard on: only http/https allowed."""
    monkeypatch.setenv("DOC_TRANSLATOR_REQUIRE_PUBLIC", "1")
    ssrf_guard.reload_config()
    ok, reason = ssrf_guard.check_base_url("file:///etc/passwd")
    assert ok is False
    assert "scheme" in reason.lower() or "http" in reason.lower()
