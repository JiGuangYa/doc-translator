"""Persistent storage: providers / settings / task job.json, all routed through
temp files for atomic replacement.

From v0.2.0, provider api_key is no longer persisted to providers.json;
it is managed via app.secrets_store (system keyring or Fernet-encrypted file).
Legacy XOR ciphertext is migrated one-shot at lifespan startup by
secrets_store.migrate_legacy_xor.
"""
import json
import os
import threading
import uuid
from functools import wraps
from pathlib import Path

from . import config
from .secrets_store import get_secret, set_secret, delete_secret, list_secret_names

_lock = threading.RLock()
_task_locks: dict[str, threading.RLock] = {}


def task_lock(task_id: str):
    with _lock:
        return _task_locks.setdefault(task_id, threading.RLock())


def task_operation(function):
    """Serialize a task's local mutations without blocking its cancellation API."""
    @wraps(function)
    def wrapped(task_id, *args, **kwargs):
        with task_lock(task_id):
            return function(task_id, *args, **kwargs)
    return wrapped


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
    finally:
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
                "input_price_per_million": payload.get("input_price_per_million"),
                "output_price_per_million": payload.get("output_price_per_million"),
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
            for k in ("input_price_per_million", "output_price_per_million"):
                if k in payload:
                    rec[k] = payload[k]
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
    "task_retention_days": 0 if config.DESKTOP_MODE else config.TASK_TTL_DAYS,
    "use_translation_memory": True,
}


# Safe ranges for batch parameters (unified clamping across API/persist/load):
# oversized batches cause a single LLM response to be truncated and the whole batch lost.
_SETTING_CLAMPS = {
    "batch_max_chars": (200, 20000),
    "batch_max_segments": (1, 100),
    "concurrency_batches": (1, 10),
    "task_retention_days": (0, 3650),
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


def load_glossary() -> list[dict]:
    data = _read_json(config.GLOSSARY_FILE, {"entries": []})
    return data.get("entries", []) if isinstance(data, dict) else []


def save_glossary(entries: list[dict]) -> list[dict]:
    clean = []
    for entry in entries[:500]:
        source = str(entry.get("source", "")).strip()
        target = str(entry.get("target", "")).strip()
        if source and target:
            clean.append({"source": source[:200], "target": target[:200]})
    _atomic_write_json(config.GLOSSARY_FILE, {"entries": clean})
    return clean


# ---------- task job.json ----------

def task_dir(task_id: str) -> Path:
    return config.TASKS_DIR / task_id


def load_job(task_id: str) -> dict | None:
    job = _read_json(task_dir(task_id) / "job.json", None)
    return job


_SUMMARY_KEYS = (
    "task_id", "filename", "ext", "status", "created_at", "updated_at",
    "segment_count", "total_chars", "skipped_count", "source_lang", "target_lang",
    "provider_id", "error", "warnings", "overflow", "revision", "usage",
    "token_budget", "input_price_per_million", "output_price_per_million",
    "ocr_scanned", "ocr_pages", "external_edit",
    "ocr_required_pages", "ocr_completed_pages", "ocr_empty_pages", "ocr_dismissed_pages", "ocr_optional_pages",
    "ocr_review_count", "archived", "provider_snapshot", "requires_snapshot_confirmation",
    "history_usage_unknown", "migration_issue",
)


def _summary_record(job: dict) -> dict:
    record = {key: job.get(key) for key in _SUMMARY_KEYS}
    translated = job.get("translations") or {}
    translatable = [segment for segment in job.get("segments", [])
                    if segment.get("translatable")]
    record["untranslated_count"] = sum(
        1 for segment in translatable
        if not (translated.get(segment["seg_id"], segment.get("translation")))
        or str(translated.get(segment["seg_id"], segment.get("translation")))
        .startswith("⟪ untranslated:"))
    record["done_segments"] = len(translatable) - record["untranslated_count"]
    record["has_translated"] = (task_dir(job["task_id"]) /
                                f"translated{job.get('ext', '')}").exists()
    return record


def _save_summary(task_id: str, job: dict) -> None:
    try:
        _atomic_write_json(task_dir(task_id) / "summary.json", _summary_record(job))
    except OSError:
        pass  # the full job is authoritative; summaries can be rebuilt


def save_job(task_id: str, job: dict) -> None:
    with _lock:
        job.setdefault("revision", job.get("content_version", 0))
        job["content_version"] = job["revision"]
        _atomic_write_json(task_dir(task_id) / "job.json", job)
        _save_summary(task_id, job)


def update_job(task_id: str, mutator) -> dict | None:
    """Atomic load -> mutate -> save performed entirely inside the lock.

    When each business path independently loads the entire job and overwrites
    it wholesale, two concurrent writers will stomp each other's changes
    (translations, status, etc.). All read-modify-write should go through
    this interface. mutator(job) modifies in place; returns None if task
    does not exist.
    """
    with _lock:
        path = task_dir(task_id) / "job.json"
        job = _read_json(path, None)
        if not job:
            return None
        mutator(job)
        _atomic_write_json(path, job)
        _save_summary(task_id, job)
        return job


def list_job_headers() -> list[dict]:
    """Read small task summaries for frequent native sidebar refreshes."""
    result = []
    if not config.TASKS_DIR.exists():
        return result
    for directory in config.TASKS_DIR.iterdir():
        if not directory.is_dir():
            continue
        job_path = directory / "job.json"
        summary_path = directory / "summary.json"
        if not job_path.exists():
            continue
        if (summary_path.exists() and
                summary_path.stat().st_mtime_ns >= job_path.stat().st_mtime_ns):
            summary = _read_json(summary_path, None)
            if summary:
                result.append(summary)
                continue
        full = _read_json(job_path, None)
        if full:
            result.append(_summary_record(full))
    return sorted(result, key=lambda item: item.get("created_at") or "", reverse=True)


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
    if task_manager.is_active(task_id):
        raise ValueError("Task is currently translating; wait before deleting")
    d = task_dir(task_id)
    if not d.exists():
        return False
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    return True


def _trash_dir() -> Path:
    # Derive from TASKS_DIR so isolated test/storage roots stay together.
    return config.TASKS_DIR.parent / "trash"


def trash_task(task_id: str) -> bool:
    """Move a task into the application's recoverable trash."""
    import time as _time
    source = task_dir(task_id)
    if not source.exists():
        return False
    destination = _trash_dir() / task_id
    if destination.exists():
        raise FileExistsError(f"Task {task_id} is already in trash")
    _trash_dir().mkdir(parents=True, exist_ok=True)
    source.replace(destination)
    _atomic_write_json(destination / "trash.json", {"deleted_at": _time.time()})
    return True


def list_trash() -> list[dict]:
    """Return recoverable task summaries without loading document contents."""
    items = []
    if not _trash_dir().exists():
        return items
    for directory in _trash_dir().iterdir():
        if not directory.is_dir():
            continue
        job = _read_json(directory / "job.json", None)
        marker = _read_json(directory / "trash.json", {})
        if job:
            items.append({"task_id": job["task_id"], "filename": job.get("filename", ""),
                          "deleted_at": marker.get("deleted_at")})
    return sorted(items, key=lambda item: item.get("deleted_at") or 0, reverse=True)


@task_operation
def restore_task(task_id: str) -> bool:
    source = _trash_dir() / task_id
    destination = task_dir(task_id)
    if not source.exists():
        return False
    if destination.exists():
        raise FileExistsError(f"Task {task_id} already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)
    (destination / "trash.json").unlink(missing_ok=True)
    return True


def gc_trash(days: int = config.TRASH_TTL_DAYS) -> list[str]:
    """Purge app trash after its recovery window expires."""
    import shutil
    import time as _time
    removed = []
    if days <= 0 or not _trash_dir().exists():
        return removed
    cutoff = _time.time() - days * 86400
    for item in list_trash():
        from .services import drafts
        if drafts.for_task(item["task_id"]):
            continue
        if (item.get("deleted_at") or _time.time()) < cutoff:
            shutil.rmtree(_trash_dir() / item["task_id"])
            removed.append(item["task_id"])
    return removed


def gc_old_previews(days: int = config.PREVIEW_CACHE_DAYS) -> list[str]:
    """Reclaim regenerable page images without deleting source or translations."""
    import shutil
    import time as _time
    removed = []
    if days <= 0:
        return removed
    cutoff = _time.time() - days * 86400
    for job in list_jobs():
        preview = task_dir(job["task_id"]) / "previews"
        if not preview.is_dir():
            continue
        newest = max((p.stat().st_mtime for p in preview.rglob("*") if p.is_file()),
                     default=preview.stat().st_mtime)
        if newest < cutoff:
            shutil.rmtree(preview)
            removed.append(job["task_id"])
    return removed


_TERMINAL_STATUSES = {"done", "cancelled", "failed"}


def gc_old_tasks(ttl_days: int) -> list[str]:
    """Move expired terminal tasks to recoverable trash and return their ids.

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
        from .services import drafts
        if job.get("archived") or drafts.for_task(job["task_id"]):
            continue
        if job.get("status") not in _TERMINAL_STATUSES:
            continue
        ts = job.get("updated_at") or job.get("created_at") or ""
        try:
            t = _time.mktime(_time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue  # Prefer to keep tasks with malformed timestamps
        t = max(t, float(job.get("imported_at") or 0))
        if t < cutoff and trash_task(job["task_id"]):
            removed.append(job["task_id"])
    return removed


def document_path(job: dict, variant: str) -> Path:
    if variant not in ("original", "translated"):
        raise ValueError("Invalid document variant")
    return task_dir(job["task_id"]) / f"{variant}{job['ext']}"
