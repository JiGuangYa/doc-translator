"""Persistent storage: providers / settings / task job.json, all routed through
temp files for atomic replacement.

From v0.2.0, provider api_key is no longer persisted to providers.json;
it is managed via app.secrets_store (system keyring or Fernet-encrypted file).
Legacy XOR ciphertext is migrated one-shot at lifespan startup by
secrets_store.migrate_legacy_xor.
"""
import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

from . import config
from .secrets_store import get_secret, set_secret, delete_secret, list_secret_names

_lock = threading.RLock()  # Provider deletion also saves defaults under this lock.
_task_locks: dict[str, threading.RLock] = {}
_task_locks_guard = threading.Lock()


def task_lock(task_id: str):
    """Serialize a task's lifecycle and commits; never acquire under _lock."""
    with _task_locks_guard:
        return _task_locks.setdefault(task_id, threading.RLock())


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            tmp = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


# ---------- providers ----------

def _new_provider_id() -> str:
    import uuid
    return "p_" + uuid.uuid4().hex[:8]


def _stored_secret_ids() -> set[str]:
    """Set of provider ids that have an existing encrypted secret."""
    try:
        return set(list_secret_names())
    except Exception:
        return set()


def list_providers() -> list[dict]:
    data = _read_json(config.PROVIDERS_FILE, {"providers": []})
    secret_ids = _stored_secret_ids()
    out = []
    for p in data.get("providers", []):
        # Strip any leftover api_key_enc fields (prevent leaking into API response)
        clean = {k: v for k, v in p.items() if k not in ("api_key_enc", "api_key")}
        # has_api_key reflects whether a real key exists in secrets_store
        clean["has_api_key"] = p["id"] in secret_ids
        out.append(clean)
    return out


def get_provider(provider_id: str) -> dict | None:
    for p in list_providers():
        if p["id"] == provider_id:
            return p
    return None


def get_provider_secret(provider_id: str) -> str:
    """For service-side internal use only: retrieve api_key from secrets_store."""
    return get_secret(provider_id) or ""


def save_provider(payload: dict, provider_id: str | None = None) -> dict:
    with _lock:
        data = _read_json(config.PROVIDERS_FILE, {"providers": []})
        providers = data.setdefault("providers", [])
        if provider_id is None:
            pid = _new_provider_id()
            rec = {
                "id": pid,
                "name": payload["name"],
                "base_url": payload["base_url"].rstrip("/"),
                "model": payload["model"],
                "enabled": True,
            }
            providers.append(rec)
        else:
            pid = provider_id
            rec = next((p for p in providers if p["id"] == pid), None)
            if rec is None:
                raise KeyError(f"provider {provider_id} not found")
            for k in ("name", "base_url", "model"):
                if payload.get(k):
                    rec[k] = payload[k].rstrip("/") if k == "base_url" else payload[k]
        # api_key is stored separately in secrets_store; not written back to providers.json
        api_key = payload.get("api_key", "")
        if api_key:
            set_secret(pid, api_key)
        # Historical field cleanup: old XOR ciphertext no longer needed
        for p in providers:
            p.pop("api_key_enc", None)
        _atomic_write_json(config.PROVIDERS_FILE, data)
        return get_provider(pid)


def delete_provider(provider_id: str) -> bool:
    with _lock:
        data = _read_json(config.PROVIDERS_FILE, {"providers": []})
        before = len(data.get("providers", []))
        data["providers"] = [p for p in data.get("providers", []) if p["id"] != provider_id]
        if len(data["providers"]) == before:
            return False
        _atomic_write_json(config.PROVIDERS_FILE, data)
        settings = load_settings()
        changed = False
        for key in ("translation_provider_id", "assistant_provider_id"):
            if settings.get(key) == provider_id:
                settings[key] = None
                changed = True
        if changed:
            save_settings(settings)
    # Outside the lock: clear the encrypted secret
    try:
        delete_secret(provider_id)
    except Exception:
        pass
    return True


# ---------- settings ----------

DEFAULT_SETTINGS = {
    "translation_provider_id": None,
    "assistant_provider_id": None,
    "source_lang": "auto",
    "target_lang": "zh-CN",
    "translate_notes": True,          # whether to translate pptx speaker notes
    "batch_max_chars": config.DEFAULT_BATCH_MAX_CHARS,
    "batch_max_segments": config.DEFAULT_BATCH_MAX_SEGMENTS,
    "concurrency_batches": config.DEFAULT_CONCURRENCY_BATCHES,
}


# Safe ranges for batch parameters (unified clamping across API/persist/load):
# oversized batches cause a single LLM response to be truncated and the whole batch lost.
_SETTING_CLAMPS = {
    "batch_max_chars": (200, 20000),
    "batch_max_segments": (1, 100),
    "concurrency_batches": (1, 10),
}


def load_settings() -> dict:
    stored = _read_json(config.SETTINGS_FILE, {})
    merged = dict(DEFAULT_SETTINGS)
    merged.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
    for k, (lo, hi) in _SETTING_CLAMPS.items():
        if isinstance(merged.get(k), int):
            merged[k] = max(lo, min(hi, merged[k]))
    return merged


def save_settings(update: dict) -> dict:
    with _lock:
        current = load_settings()
        # Second line of defense: clamp batch parameters before persisting,
        # regardless of whether the caller already validated.
        clamps = _SETTING_CLAMPS
        for k in DEFAULT_SETTINGS:
            if k in update and update[k] is not None or (k in update and k.endswith("_id")):
                if k in update:
                    if k in clamps and isinstance(update[k], int):
                        lo, hi = clamps[k]
                        current[k] = max(lo, min(hi, update[k]))
                    else:
                        current[k] = update[k]
        _atomic_write_json(config.SETTINGS_FILE, current)
        return current


# ---------- task job.json ----------

def task_dir(task_id: str) -> Path:
    return config.TASKS_DIR / task_id


def document_path(job: dict, variant: str) -> Path:
    """The job record commits an immutable output; legacy files stay readable."""
    if variant not in ("original", "translated"):
        raise ValueError("Invalid document variant")
    output = job.get("output_file") if variant == "translated" else None
    if output:
        if not re.fullmatch(r"outputs/[0-9a-f]{32}\.(pdf|docx|pptx|xlsx)", output):
            raise ValueError("Invalid saved output path")
        return task_dir(job["task_id"]) / output
    return task_dir(job["task_id"]) / f"{variant}{job['ext']}"


def recover_task_files() -> None:
    """Reclaim uncommitted/old generations only at startup, before any readers.

    A crash before job.json is replaced leaves an orphan output, never a
    partially committed revision. Keep unknown/corrupt tasks untouched.
    """
    for job in list_jobs():
        with task_lock(job["task_id"]):
            current = document_path(job, "translated")
            if job.get("output_file") and not current.is_file():
                continue
            folder = task_dir(job["task_id"])
            for output in (folder / "outputs").glob("*"):
                if output.is_file() and re.fullmatch(r"[0-9a-f]{32}\.(pdf|docx|pptx|xlsx)", output.name) and output != current:
                    output.unlink(missing_ok=True)
            version = f"v{job.get('content_version', 0)}"
            for cached in (folder / "previews").glob("v*"):
                if cached.is_dir() and re.fullmatch(r"v[0-9]+", cached.name) and cached.name != version:
                    shutil.rmtree(cached)


def load_job(task_id: str) -> dict | None:
    job = _read_json(task_dir(task_id) / "job.json", None)
    return job


def save_job(task_id: str, job: dict) -> None:
    with task_lock(task_id), _lock:
        _atomic_write_json(task_dir(task_id) / "job.json", job)


def update_job(task_id: str, mutator) -> dict | None:
    """Atomic load -> mutate -> save performed entirely inside the lock.

    When each business path independently loads the entire job and overwrites
    it wholesale, two concurrent writers will stomp each other's changes
    (translations, status, etc.). All read-modify-write should go through
    this interface. mutator(job) modifies in place; returns None if task
    does not exist.
    """
    with task_lock(task_id), _lock:
        path = task_dir(task_id) / "job.json"
        job = _read_json(path, None)
        if not job:
            return None
        mutator(job)
        _atomic_write_json(path, job)
        return job


def list_jobs() -> list[dict]:
    """Scan the tasks directory as the single source of truth, sorted by
    creation time descending."""
    jobs = []
    if config.TASKS_DIR.exists():
        for d in config.TASKS_DIR.iterdir():
            if d.is_dir():
                job = _read_json(d / "job.json", None)
                if job:
                    jobs.append(job)
    jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return jobs


def delete_task(task_id: str) -> bool:
    from .services import task_manager
    with task_lock(task_id):
        if task_manager.is_active(task_id):
            raise ValueError("Task is currently translating; cancel it and wait before deleting")
        d = task_dir(task_id)
        if not d.exists():
            return False
        shutil.rmtree(d)
        return True


_TERMINAL_STATUSES = {"done", "cancelled", "failed"}


def gc_old_tasks(ttl_days: int) -> list[str]:
    """Delete task directories in a terminal state older than TTL
    (including original/translated/page images), and return the list of
    removed task_ids.

    Each task's original+translated+page PNGs can reach tens of MB with no
    other reclamation path, so disk usage grows without bound on long-running
    deployments. Intermediate states (translating/pending_confirm) are not touched.
    """
    import time as _time
    if ttl_days <= 0:
        return []
    cutoff = _time.time() - ttl_days * 86400
    removed: list[str] = []
    for job in list_jobs():
        if job.get("status") not in _TERMINAL_STATUSES:
            continue
        ts = job.get("updated_at") or job.get("created_at") or ""
        try:
            t = _time.mktime(_time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue  # Prefer to keep tasks with malformed timestamps
        if t < cutoff and delete_task(job["task_id"]):
            removed.append(job["task_id"])
    return removed
