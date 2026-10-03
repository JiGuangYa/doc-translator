"""Encrypted secret storage with keyring (preferred) and Fernet (fallback).

API keys for LLM providers are stored encrypted at rest. On systems with a
working keyring backend (macOS Keychain, Windows Credential Manager, Linux
Secret Service), the OS keyring is used directly. Otherwise we fall back to
a Fernet-encrypted JSON file under data/secrets/, with a master key derived
from a machine fingerprint via PBKDF2.
"""
from __future__ import annotations

import base64
import json
import os
import platform
import secrets
import socket
import tempfile
from pathlib import Path
from typing import Optional

import threading

import keyring
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import logging

logger = logging.getLogger(__name__)

KEYRING_SERVICE = os.environ.get("DOC_TRANSLATOR_KEYRING_SERVICE", "doc-translator")
_BACKEND: str | None = None  # "keyring" | "fernet"

# Single lock for all secret-store mutations: prevents the
# _detect_backend() / _fernet_* TOCTOU race that could lose concurrent
# provider writes or write ciphertext to the wrong slot.
_lock = threading.RLock()


def _write_secrets(path: Path, data: dict) -> None:
    """An interrupted write must leave the previous encrypted store intact."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            os.chmod(temporary, 0o600)
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _secret_record(name: str):
    path = _secrets_dir() / "providers.json"
    if not path.exists():
        return None
    return json.loads(path.read_text()).get(name)


def _data_dir() -> Path:
    from app.config import DATA_DIR
    return DATA_DIR


def _secrets_dir() -> Path:
    d = _data_dir() / "secrets"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _machine_fingerprint() -> bytes:
    """Stable per-machine identifier; not a secret, just salts the key."""
    parts = [platform.node(), platform.machine(), str(socket.gethostname())]
    try:
        with open("/etc/machine-id") as f:
            parts.append(f.read().strip())
    except OSError:
        pass
    return "|".join(parts).encode("utf-8")


def _derive_fernet_key(salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=200_000,
    )
    return base64.urlsafe_b64encode(kdf.derive(_machine_fingerprint()))


def _load_or_create_fernet() -> Fernet:
    secrets_dir = _secrets_dir()
    salt_path = secrets_dir / ".salt"
    key_path = secrets_dir / ".master"
    if not salt_path.exists():
        salt_path.write_bytes(secrets.token_bytes(16))
        try:
            os.chmod(salt_path, 0o600)
        except OSError:
            pass
    salt = salt_path.read_bytes()
    if not key_path.exists():
        key_path.write_bytes(_derive_fernet_key(salt))
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
    return Fernet(key_path.read_bytes())


def _detect_backend() -> str:
    # Backend detection is racy on its own: two threads may both see
    # _BACKEND == None and both call keyring.get_password, with one
    # succeeding and the other raising mid-flight. The lock here, plus
    # the lock in set_secret, ensures only one probe runs at a time and
    # _BACKEND is settled before any reader sees it.
    global _BACKEND
    with _lock:
        if _BACKEND is not None:
            return _BACKEND
        try:
            keyring.get_password(KEYRING_SERVICE, "__probe__")
            _BACKEND = "keyring"
        except Exception:
            _BACKEND = "fernet"
        return _BACKEND


def _fernet_get(name: str) -> Optional[str]:
    # Reads are not strictly racy (we are not mutating state), but
    # serialize them anyway so a concurrent writer cannot delete the
    # entry between our load and decrypt.
    with _lock:
        path = _secrets_dir() / "providers.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        enc = data.get(name)
        if isinstance(enc, dict):
            enc = enc.get("ciphertext")
        if not enc:
            return None
        try:
            return _load_or_create_fernet().decrypt(enc.encode()).decode()
        except InvalidToken:
            return None


def _fernet_set(name: str, value: str, *, keyring_current: bool = False) -> None:
    # The read-modify-write of providers.json must be atomic; otherwise
    # two simultaneous providers created from racing requests can
    # silently overwrite each other. _load_or_create_fernet is also
    # raced between first-call writers — its salt/key file creation is
    # therefore nested inside the lock to prevent two processes from
    # choosing different salts.
    with _lock:
        path = _secrets_dir() / "providers.json"
        if path.exists():
            # Refuse to overwrite an unreadable store and lose other keys.
            data = json.loads(path.read_text())
        else:
            data = {}
        data[name] = {"ciphertext": _load_or_create_fernet().encrypt(value.encode()).decode(),
                      "keyring_current": keyring_current}
        _write_secrets(path, data)


def _fernet_delete(name: str) -> None:
    with _lock:
        path = _secrets_dir() / "providers.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        data.pop(name, None)
        _write_secrets(path, data)


def _fernet_list() -> list[str]:
    with _lock:
        path = _secrets_dir() / "providers.json"
        if not path.exists():
            return []
        try:
            return list(json.loads(path.read_text()).keys())
        except (OSError, json.JSONDecodeError):
            return []


def get_secret(name: str) -> Optional[str]:
    with _lock:
        try:
            record = _secret_record(name)
        except (OSError, json.JSONDecodeError):
            record = None
        # Remember failed Keychain writes across process restarts. A readable
        # but stale Keychain item must not override the newly saved fallback.
        if isinstance(record, dict) and not record.get("keyring_current", False):
            return _fernet_get(name)
        if _detect_backend() == "keyring":
            try:
                value = keyring.get_password(KEYRING_SERVICE, name)
                if value is not None:
                    return value
            except Exception:
                pass
        return _fernet_get(name)


def set_secret(name: str, value: str) -> None:
    """Write to keyring AND mirror to fernet file so list_secret_names works.

    The keyring has no native list API. To support enumeration, we always
    maintain a fernet-encrypted shadow file. If the keyring write fails, we
    still write the fernet shadow as a fallback and log a warning so
    operators notice a degraded backend.
    """
    with _lock:
        # Commit the encrypted fallback first. If the process exits during a
        # Keychain prompt, the latest value is still recoverable on restart.
        _fernet_set(name, value, keyring_current=False)
        if _detect_backend() == "keyring":
            try:
                keyring.set_password(KEYRING_SERVICE, name, value)
            except Exception as e:
                logger.warning("keyring write failed for %r (%s); using encrypted fallback", name, type(e).__name__)
            else:
                try:
                    _fernet_set(name, value, keyring_current=True)
                except OSError:
                    # The new value is already durable in the fallback; a failed
                    # preference update must not report the whole save as failed.
                    logger.warning("keyring preference update failed for %r; retaining encrypted fallback", name)


def delete_secret(name: str) -> None:
    with _lock:
        if _detect_backend() == "keyring":
            try:
                keyring.delete_password(KEYRING_SERVICE, name)
            except Exception as e:
                logger.warning("keyring delete failed for %r (%s)", name, type(e).__name__)
        _fernet_delete(name)


def list_secret_names() -> list[str]:
    if _detect_backend() == "keyring":
        return _fernet_list()
    return _fernet_list()


def migrate_legacy_xor() -> bool:
    """Detect old providers.json with api_key_enc XOR data and migrate.

    Returns True if migration ran.
    """
    from app.config import DATA_DIR
    from app._legacy_xor import deobfuscate_legacy_xor  # private helper (P1-1)
    legacy = DATA_DIR / "config" / "providers.json"
    if not legacy.exists():
        return False
    try:
        data = json.loads(legacy.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, list):
        return False
    migrated = 0
    skipped = 0
    for p in data:
        enc = p.get("api_key_enc")
        if not enc:
            continue
        try:
            plain = deobfuscate_legacy_xor(enc)
        except Exception as e:
            skipped += 1
            logger.warning("legacy XOR ciphertext for provider %r could not be decrypted (%s: %s); "
                           "operator must re-enter the API key", p.get("id"), type(e).__name__, e)
            continue
        if plain:
            set_secret(p["id"], plain)
            p.pop("api_key_enc", None)
            migrated += 1
    if skipped:
        logger.warning("migrate_legacy_xor: %d provider(s) had undecryptable legacy ciphertext; "
                       "re-enter them through the UI", skipped)
    if migrated:
        import time
        ts = int(time.time())
        backup = legacy.with_suffix(f".json.bak.{ts}")
        backup.write_bytes(legacy.read_bytes())
        legacy.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        logger.info("migrate_legacy_xor: migrated %d legacy API key(s); backup at %s", migrated, backup)
    return migrated > 0
