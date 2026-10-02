"""Test app.secrets_store: encryption at rest with keyring + Fernet fallback."""
import pytest


@pytest.fixture(autouse=True)
def reset_secret_backend(monkeypatch):
    monkeypatch.setattr("app.secrets_store._BACKEND", None)


class _FakeKeyring:
    """In-memory keyring replacement.

    Set on ``app.secrets_store.keyring`` via monkeypatch so the test does
    not depend on the real ``keyring`` module being importable in the test
    environment (some CI images lack libsecret/SecretService, in which
    case the import would fail before any patching).
    """

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, name: str) -> str | None:
        return self.store.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.store[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        self.store.pop((service, name), None)


class _BoomKeyring:
    """Pretend the keyring backend is unavailable (raises on every call)."""

    def _boom(self, *_a, **_k):
        raise RuntimeError("no keyring backend")

    get_password = _boom
    set_password = _boom
    delete_password = _boom


@pytest.fixture
def fake_keyring(monkeypatch):
    from app import secrets_store
    fake = _FakeKeyring()
    monkeypatch.setattr(secrets_store, "keyring", fake)
    return fake.store


def test_roundtrip_via_keyring(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setattr("app.secrets_store._data_dir", lambda: tmp_path)
    from app import secrets_store
    secrets_store.set_secret("deepseek_key", "sk-abc123")
    assert secrets_store.get_secret("deepseek_key") == "sk-abc123"


def test_list_secret_names(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setattr("app.secrets_store._data_dir", lambda: tmp_path)
    from app import secrets_store
    secrets_store.set_secret("a", "1")
    secrets_store.set_secret("b", "2")
    assert set(secrets_store.list_secret_names()) == {"a", "b"}


def test_delete_secret(fake_keyring, tmp_path, monkeypatch):
    monkeypatch.setattr("app.secrets_store._data_dir", lambda: tmp_path)
    from app import secrets_store
    secrets_store.set_secret("x", "y")
    secrets_store.delete_secret("x")
    assert secrets_store.get_secret("x") is None


def test_fernet_fallback_when_keyring_unavailable(tmp_path, monkeypatch):
    """When keyring raises on first call, fall back to Fernet file."""
    from app import secrets_store
    monkeypatch.setattr(secrets_store, "keyring", _BoomKeyring())
    monkeypatch.setattr("app.secrets_store._data_dir", lambda: tmp_path)
    secrets_store.set_secret("k", "value-via-fernet")
    assert secrets_store.get_secret("k") == "value-via-fernet"
    # Verify file persisted
    secret_file = tmp_path / "secrets" / "providers.json"
    assert secret_file.exists()
    # Verify it's not plaintext
    raw = secret_file.read_bytes()
    assert b"value-via-fernet" not in raw


def test_provider_api_key_not_in_plaintext(fake_keyring, tmp_path, monkeypatch):
    """After saving a provider, its api_key must not appear in providers.json plaintext."""
    from app import config, secrets_store
    # PROVIDERS_FILE is frozen as a Path at module load time, so it must be replaced as a whole
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PROVIDERS_FILE", cfg / "providers.json")
    monkeypatch.setattr(config, "SETTINGS_FILE", cfg / "settings.json")
    secrets_store._BACKEND = None
    monkeypatch.setattr(secrets_store, "_data_dir", lambda: tmp_path / "secrets")
    from app import store
    rec = store.save_provider({
        "name": "test",
        "base_url": "https://api.example.com",
        "model": "gpt-4",
        "api_key": "sk-very-secret",
    })
    pid = rec["id"]
    raw = (cfg / "providers.json").read_text()
    assert "sk-very-secret" not in raw, f"api key leaked into providers.json: {raw[:200]}"
    assert secrets_store.get_secret(pid) == "sk-very-secret"


def test_failed_keyring_update_uses_latest_encrypted_key_after_restart(fake_keyring, tmp_path, monkeypatch):
    from app import secrets_store as secrets
    monkeypatch.setattr(secrets, '_data_dir', lambda: tmp_path)
    secrets.set_secret('p', 'old-test-value')
    def denied(*args):
        raise RuntimeError('write denied')
    monkeypatch.setattr(secrets.keyring, 'set_password', denied)
    secrets.set_secret('p', 'new-test-value')
    assert fake_keyring[(secrets.KEYRING_SERVICE, 'p')] == 'old-test-value'
    assert secrets.get_secret('p') == 'new-test-value'
    monkeypatch.setattr(secrets, '_BACKEND', None)
    assert secrets.get_secret('p') == 'new-test-value'
    assert b'new-test-value' not in (tmp_path / 'secrets/providers.json').read_bytes()


def test_missing_keyring_item_falls_back_and_legacy_ciphertext_still_reads(fake_keyring, tmp_path, monkeypatch):
    import json
    from app import secrets_store as secrets
    monkeypatch.setattr(secrets, '_data_dir', lambda: tmp_path)
    secrets.set_secret('p', 'retained-test-value')
    fake_keyring.clear()
    assert secrets.get_secret('p') == 'retained-test-value'
    path = tmp_path / 'secrets/providers.json'
    entries = json.loads(path.read_text())
    entries['p'] = entries['p']['ciphertext']  # The pre-0.1.3 file layout.
    path.write_text(json.dumps(entries))
    assert secrets.get_secret('p') == 'retained-test-value'


def test_failed_encrypted_replace_preserves_previous_keys(tmp_path, monkeypatch):
    from pathlib import Path
    from app import secrets_store as secrets
    monkeypatch.setattr(secrets, '_data_dir', lambda: tmp_path)
    monkeypatch.setattr(secrets, 'keyring', _BoomKeyring())
    secrets.set_secret('p', 'old-test-value')
    secrets.set_secret('other', 'other-test-value')
    before = (tmp_path / 'secrets/providers.json').read_bytes()
    original_replace = Path.replace
    def failed_replace(source, target):
        if target.name == 'providers.json':
            raise OSError('disk full')
        return original_replace(source, target)
    monkeypatch.setattr(Path, 'replace', failed_replace)
    with pytest.raises(OSError, match='disk full'):
        secrets.set_secret('p', 'new-test-value')
    assert (tmp_path / 'secrets/providers.json').read_bytes() == before
    assert secrets.get_secret('p') == 'old-test-value'
    assert secrets.get_secret('other') == 'other-test-value'
    assert not list((tmp_path / 'secrets').glob('tmp*'))


def test_corrupt_secret_store_is_not_silently_replaced(fake_keyring, tmp_path, monkeypatch):
    import json
    from app import secrets_store as secrets
    monkeypatch.setattr(secrets, '_data_dir', lambda: tmp_path)
    secrets.set_secret('p', 'old-test-value')
    path = tmp_path / 'secrets/providers.json'
    path.write_bytes(b'corrupt original bytes')
    with pytest.raises(json.JSONDecodeError):
        secrets.set_secret('p', 'new-test-value')
    assert path.read_bytes() == b'corrupt original bytes'
    assert fake_keyring[(secrets.KEYRING_SERVICE, 'p')] == 'old-test-value'


def test_failed_keyring_preference_update_keeps_committed_fallback(fake_keyring, tmp_path, monkeypatch):
    from app import secrets_store as secrets
    monkeypatch.setattr(secrets, '_data_dir', lambda: tmp_path)
    original = secrets._write_secrets
    def fail_preference(path, records):
        if records['p']['keyring_current']:
            raise OSError('disk full')
        original(path, records)
    monkeypatch.setattr(secrets, '_write_secrets', fail_preference)
    secrets.set_secret('p', 'new-test-value')
    monkeypatch.setattr(secrets, '_BACKEND', None)
    assert secrets.get_secret('p') == 'new-test-value'
