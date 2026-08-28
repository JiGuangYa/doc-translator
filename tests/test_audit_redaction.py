"""Test the audit redaction list in app.auth.audit().

The audit JSONL is world-readable on a misconfigured box; it must never
contain a cleartext API key, password, or session token, even if a
future caller (or a bug in a router) passes one as a kwarg.
"""
import json
import re

from app import auth


def _read_audit_lines(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_audit_redacts_api_key(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUDIT_FILE", tmp_path / "audit.jsonl")
    auth.audit("login_ok", ip="127.0.0.1", api_key="sk-1234567890abcdef")
    lines = _read_audit_lines(tmp_path / "audit.jsonl")
    assert len(lines) == 1
    assert lines[0]["api_key"] == "[REDACTED]"
    # The cleartext must not appear anywhere in the file.
    assert "sk-1234567890abcdef" not in (tmp_path / "audit.jsonl").read_text()


def test_audit_redacts_password(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUDIT_FILE", tmp_path / "audit.jsonl")
    auth.audit("login_fail", ip="127.0.0.1", password="hunter2")
    assert "[REDACTED]" in (tmp_path / "audit.jsonl").read_text()
    assert "hunter2" not in (tmp_path / "audit.jsonl").read_text()


def test_audit_redacts_token(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUDIT_FILE", tmp_path / "audit.jsonl")
    auth.audit("admin_op", session_token="abc.def.ghi")
    content = (tmp_path / "audit.jsonl").read_text()
    assert "[REDACTED]" in content
    assert "abc.def.ghi" not in content


def test_audit_redaction_is_case_insensitive(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUDIT_FILE", tmp_path / "audit.jsonl")
    auth.audit("op", PASSWORD="upper-case-key-name")
    content = (tmp_path / "audit.jsonl").read_text()
    assert "[REDACTED]" in content
    assert "upper-case-key-name" not in content


def test_audit_does_not_redact_benign_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "AUDIT_FILE", tmp_path / "audit.jsonl")
    auth.audit("settings_update", keys=["source_lang", "target_lang"], ip="127.0.0.1")
    lines = _read_audit_lines(tmp_path / "audit.jsonl")
    assert lines[0]["keys"] == ["source_lang", "target_lang"]
    assert lines[0]["ip"] == "127.0.0.1"


def test_audit_drops_none_values(monkeypatch, tmp_path):
    """None kwargs are dropped, as before the redaction layer (auth.AUDIT
    consumers should not see 'password: null' as a redaction sentinel)."""
    monkeypatch.setattr(auth, "AUDIT_FILE", tmp_path / "audit.jsonl")
    auth.audit("op", password=None, ip="127.0.0.1")
    lines = _read_audit_lines(tmp_path / "audit.jsonl")
    assert "password" not in lines[0]
