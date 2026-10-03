"""Authentication and audit: password sign-in, session cookie, JSONL audit log.

Threat model: the service binds to 127.0.0.1, but other users on the same
machine or malicious web pages must not be able to operate tasks or read
provider config (which includes API keys). A single admin password plus an
HttpOnly session cookie (SameSite=Strict) is used; cross-site write requests
are additionally covered by an Origin check in main.py.
"""
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time

from . import config

logger = logging.getLogger(__name__)

AUTH_FILE = config.CONFIG_DIR / "auth.json"
AUDIT_FILE = config.DATA_DIR / "logs" / "audit.jsonl"

PBKDF2_ITERATIONS = 200_000
SESSION_TTL = 7 * 86400        # session validity (seconds), sliding renewal
_COOKIE_NAME = "dt_session"

_lock = threading.Lock()
_sessions: dict[str, float] = {}   # token -> expiration timestamp; invalidated on restart, requires re-sign-in


# ---------- password hash ----------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def _load_auth() -> dict:
    try:
        return json.loads(AUTH_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def is_configured() -> bool:
    """Whether the admin password has been set (False before first-time use;
    the front-end guides the user to the settings page)."""
    auth = _load_auth()
    return bool(auth.get("password_hash"))


# ---------- session ----------

def create_session() -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _sessions[token] = time.time() + SESSION_TTL
        # Opportunistically clean up expired sessions to prevent slow memory
        # growth during long-running operation.
        now = time.time()
        for t in [t for t, exp in _sessions.items() if exp < now]:
            del _sessions[t]
    return token


def validate_session(token: str | None) -> bool:
    if not token:
        return False
    with _lock:
        exp = _sessions.get(token)
        if not exp or exp < time.time():
            _sessions.pop(token, None)
            return False
        _sessions[token] = time.time() + SESSION_TTL   # sliding renewal
        return True


def drop_session(token: str | None) -> None:
    if token:
        with _lock:
            _sessions.pop(token, None)


def set_password(password: str) -> None:
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"password_hash": hash_password(password)}, indent=2),
                   encoding="utf-8")
    tmp.replace(AUTH_FILE)


def verify_login(password: str) -> bool:
    stored = _load_auth().get("password_hash", "")
    return bool(stored) and verify_password(password, stored)


def cookie_name() -> str:
    return _COOKIE_NAME


# ---------- audit log ----------

# Keys whose values are sensitive: a caller passing these by name in the
# audit() kwargs will have the value replaced with "[REDACTED]" before
# it touches the on-disk JSONL. The list is intentionally conservative;
# match the names that already exist in call sites (api_key, password,
# token, secret, api_key_enc) plus a few obvious neighbours.
_REDACT_KEYS = frozenset({
    "password", "passwd", "pwd",
    "api_key", "apikey", "key",
    "token", "session", "session_token", "auth",
    "secret", "credential", "credentials",
    "api_key_enc",  # legacy XOR ciphertext
    "base_url",  # some gateways embed bearer tokens in query strings
})


def _redact(key: str, value) -> object:
    if key.lower() in _REDACT_KEYS and value is not None:
        return "[REDACTED]"
    return value


def audit(event: str, **detail) -> None:
    """Append one audit record to data/logs/audit.jsonl. Failures are only
    logged to the application log and do not affect the main flow.

    Sensitive keyword names (see ``_REDACT_KEYS``) are replaced with
    ``"[REDACTED]"`` so a future caller that accidentally passes
    ``password=...`` does not leak it to the JSONL. The redaction is
    case-insensitive on the key name.
    """
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event}
    rec.update({k: _redact(k, v) for k, v in detail.items() if v is not None})
    line = json.dumps(rec, ensure_ascii=False)
    try:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        logger.warning("Audit log write failed: %s", e)
