"""Copy legacy libraries into the preview, with durable plans and source verification."""
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import tempfile
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .. import config, secrets_store, store
from ..formats import common
from . import output_transaction, task_manager

_lock = threading.Lock()


def _json(path, default):
    return json.loads(path.read_text()) if path.exists() else copy.deepcopy(default)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _source_file(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or path.is_symlink():
        raise ValueError("旧资料库包含越界或符号链接文件，请先另存完整副本")
    return resolved


def _require_source_stopped(root: Path):
    lease = root / ".desktop-engine.lock"
    if lease.exists() and os.name == "posix":
        import fcntl
        with lease.open("r") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("请先退出正在使用旧资料库的应用，再导入") from error
    if Path("/usr/sbin/lsof").exists():
        logs = [p for p in (root / "backend.log", root.parent / "backend.log") if p.exists()]
        if logs:
            result = subprocess.run(["/usr/sbin/lsof", "-t", *map(str, logs)], capture_output=True,
                                    text=True, timeout=10, check=False)
            if result.stdout.strip():
                raise ValueError("旧版翻译引擎仍在运行，请退出旧应用后重试")


def inspect_library(source: str, kind: str = "auto") -> tuple[Path, Path, str]:
    selected = Path(source).expanduser().resolve()
    root = selected / "data" if (selected / "data/tasks").is_dir() else selected
    if not (root / "tasks").is_dir():
        raise ValueError("请选择包含 tasks 的旧资料库，或 DocTranslatorMac 文件夹")
    if root == config.DATA_DIR.resolve() or root in config.DATA_DIR.resolve().parents or config.DATA_DIR.resolve() in root.parents:
        raise ValueError("导入来源与当前资料库不能重叠")
    if kind == "auto":
        kind = "macbook" if root != selected or (root / ".desktop-engine.lock").exists() or root.parent.name == "DocTranslatorMac" else "native"
    if kind not in ("native", "macbook"):
        raise ValueError("Unknown legacy library type")
    support = root.parent if kind == "macbook" and root.name == "data" else selected
    _require_source_stopped(root)
    return root, support, kind


def _recover_key(root, provider_id, allow_keychain):
    try:
        records = _json(root / "secrets/providers.json", {})
    except (OSError, ValueError):
        return None
    record = records.get(provider_id)
    plaintext = None
    ciphertext = record.get("ciphertext") if isinstance(record, dict) else record
    if ciphertext:
        master = root / "secrets/.master"
        salt = root / "secrets/.salt"
        try:
            key = _source_file(root, master).read_bytes() if master.exists() else secrets_store._derive_fernet_key(_source_file(root, salt).read_bytes())
            plaintext = Fernet(key).decrypt(ciphertext.encode()).decode()
        except (OSError, ValueError, InvalidToken):
            pass
    if isinstance(record, dict) and not record.get("keyring_current", False):
        return plaintext
    stored = None
    if allow_keychain:
        try:
            stored = secrets_store.keyring.get_password("doc-translator", provider_id)
        except Exception:
            pass
    if plaintext and stored and plaintext != stored:
        return None  # The legacy record cannot prove which credential is newer.
    return stored or plaintext


def _verify_mapping(job, source, kind):
    if job.get("ocr_scanned"):
        return
    options = dict(job.get("format_options") or {})
    if job["ext"] == ".docx":
        options.setdefault("docx_version", 3 if kind == "macbook" else 1)
    handler = common.get_format_handler(job["ext"])
    if handler is None:
        raise ValueError("Unsupported legacy file type")
    expected = {s["seg_id"]: s["text"] for s in job.get("segments", [])}
    choices = [options]
    if job["ext"] == ".pptx" and "translate_notes" not in options:
        choices = [{**options, "translate_notes": flag} for flag in (True, False)]
    for candidate in choices:
        extracted = handler.extract(source, candidate)
        actual = {common.make_seg_id(i): s.text for i, s in enumerate(extracted.segments)}
        if actual == expected:
            job["format_options"] = candidate
            return
    raise ValueError("原件与旧段落编号无法一一对应；已保留译文、草稿和原件，请另译一份或检查旧数据")


def import_library(source: str, kind: str = "auto", allow_keychain: bool = True,
                   native_reading: dict | None = None) -> dict:
    with _lock:
        if task_manager.count_in_flight():
            raise ValueError("请等待当前翻译保存结束后再导入资料库")
        root, support, kind = inspect_library(source, kind)
        manifest_path = config.DATA_DIR / "import-manifest.json"
        manifest = _json(manifest_path, {"schema_version": 1, "tasks": {}, "providers": {}})
        report = {"imported": 0, "skipped": 0, "issues": [], "credential_required": [], "source_kind": kind,
                  "task_map": {}}
        source_settings = _json(root / "config/settings.json", {})
        source_providers = _json(root / "config/providers.json", {"providers": []}).get("providers", [])
        # Reconstruct a deleted provider's non-secret configuration from its task snapshot.
        known = {provider.get("id") for provider in source_providers}
        for location in (root / "tasks", root / "trash"):
            for file in location.glob("*/job.json"):
                legacy = _json(_source_file(root, file), {})
                snapshot = legacy.get("provider_snapshot") or {}
                pid = legacy.get("provider_id")
                if pid and pid != "mock" and pid not in known and snapshot.get("base_url") and snapshot.get("model"):
                    source_providers.append({**snapshot, "id": pid, "name": snapshot.get("name") or "Recovered model"})
                    known.add(pid)
        source_drafts = _json(support / "revision-drafts.json", {})
        source_reading = _json(support / "reading-state.json", {"documents": {}})
        if kind == "native" and native_reading:
            source_reading = native_reading
        identity = _digest({"root": str(root), "kind": kind})
        provider_map = {}
        for provider in source_providers:
            old_id = provider.get("id")
            if not old_id:
                continue
            key = identity + ":" + old_id + ":" + _digest(provider)
            new_id = manifest["providers"].setdefault(key, "p_" + uuid.uuid4().hex[:12])
            store._atomic_write_json(manifest_path, manifest)
            provider_map[old_id] = new_id
            with store._lock:
                current = _json(config.PROVIDERS_FILE, {"providers": []})
                exists = any(item["id"] == new_id for item in current["providers"])
                if not exists:
                    clean = {k: v for k, v in provider.items() if k not in ("api_key", "api_key_enc", "has_api_key")}
                    clean.update(id=new_id, name=provider.get("name", "Imported") + (" · MacBook" if kind == "macbook" else " · 旧版"))
                    credential = _recover_key(root, old_id, allow_keychain)
                    if credential:
                        secrets_store.set_secret(new_id, credential)
                    else:
                        report["credential_required"].append(new_id)
                        clean["credential_required"] = True
                    current["providers"].append(clean)
                    store._atomic_write_json(config.PROVIDERS_FILE, current)
        if not config.SETTINGS_FILE.exists():
            copied = {key: value for key, value in source_settings.items() if key in store.DEFAULT_SETTINGS}
            for key in ("translation_provider_id", "assistant_provider_id"):
                if key in copied:
                    copied[key] = provider_map.get(copied[key])
            copied["task_retention_days"] = 0
            store.save_settings(copied)
        # Preview client stores are imported atomically; the native app reloads
        # these only after the endpoint succeeds and editing is disabled meanwhile.
        drafts_path = config.DATA_DIR / "revision-drafts.json"
        revisions_path = config.DATA_DIR / "draft-revisions.json"
        reading_path = config.DATA_DIR / "reading-state.json"
        drafts = _json(drafts_path, {})
        draft_revisions = _json(revisions_path, {})
        if drafts.get("schema_version") == 2:
            draft_revisions = {key: value.get("revision") for key, value in drafts["tasks"].items()}
            drafts = {key: value["segments"] for key, value in drafts["tasks"].items()}
        reading = _json(reading_path, {"documents": {}})
        for deleted in (False, True):
            source_tasks = root / ("trash" if deleted else "tasks")
            for job_path in sorted(source_tasks.glob("*/job.json")):
                if not re.fullmatch(r"[a-f0-9]{32}", job_path.parent.name):
                    continue
                original_job_bytes = _source_file(root, job_path).read_bytes()
                old = json.loads(original_job_bytes)
                old_id = job_path.parent.name
                ext = old.get("ext")
                if ext not in common.SUPPORTED_EXTENSIONS:
                    report["issues"].append({"task_id": old_id, "message": "Unsupported file type"})
                    continue
                original = _source_file(root, job_path.parent / f"original{ext}")
                translated = _source_file(root, job_path.parent / old.get("output_file", f"translated{ext}"))
                original_hash, translated_hash = output_transaction.file_hash(original), output_transaction.file_hash(translated)
                fingerprint = _digest({"source": identity, "task": old, "original": original_hash,
                                       "translated": translated_hash, "drafts": source_drafts.get(old_id, {}), "trash": deleted})
                record = manifest["tasks"].get(fingerprint)
                if record and record.get("complete"):
                    report["task_map"][old_id] = record["task_id"]
                    report["skipped"] += 1
                    continue
                if not record:
                    record = {"task_id": uuid.uuid4().hex, "complete": False}
                    manifest["tasks"][fingerprint] = record
                    store._atomic_write_json(manifest_path, manifest)
                new_id = record["task_id"]
                report["task_map"][old_id] = new_id
                destination = (store._trash_dir() if deleted else config.TASKS_DIR) / new_id
                staging = config.DATA_DIR / ".imports" / new_id
                if not destination.exists():
                    if staging.exists():
                        shutil.rmtree(staging)
                    staging.mkdir(parents=True)
                    job = copy.deepcopy(old)
                    job.update(task_id=new_id, source_version=kind, import_fingerprint=fingerprint,
                               imported_from_task=old_id, imported_at=time.time(), revision=old.get("revision", old.get("content_version", 0)))
                    job["content_version"] = job["revision"]
                    job.pop("output_file", None)
                    job.pop("migration_issue", None)
                    if job.get("status") in ("translating", "queued", "parsing", "uploaded"):
                        job["status"] = "paused"  # Import never starts a billable task.
                    if original.exists():
                        shutil.copy2(original, staging / f"original{ext}")
                    if translated.exists():
                        shutil.copy2(translated, staging / f"translated{ext}")
                        job["translated_sha256"] = translated_hash
                    try:
                        _verify_mapping(job, original, kind)
                    except Exception as error:
                        job["migration_issue"] = str(error)
                        report["issues"].append({"task_id": new_id, "message": str(error)})
                    if kind == "macbook":
                        job["segments"].sort(key=lambda segment: segment["seg_id"])
                    old_provider = old.get("provider_id")
                    job["provider_id"] = provider_map.get(old_provider, old_provider if old_provider == "mock" else None)
                    if job.get("provider_snapshot"):
                        job["provider_snapshot"]["id"] = job["provider_id"]
                    job["requires_snapshot_confirmation"] = bool(job.get("translations")) and not job.get("provider_snapshot")
                    job["history_usage_unknown"] = old.get("history_usage_unknown", "usage" not in old)
                    job.setdefault("source_warnings", list(job.get("warnings") or []))
                    store._atomic_write_json(staging / "job.json", job)
                    if deleted:
                        store._atomic_write_json(staging / "trash.json", {"deleted_at": time.time()})
                    if (job_path.read_bytes() != original_job_bytes or output_transaction.file_hash(original) != original_hash or
                            output_transaction.file_hash(translated) != translated_hash or
                            output_transaction.file_hash(staging / f"original{ext}") != original_hash or
                            output_transaction.file_hash(staging / f"translated{ext}") != translated_hash):
                        raise ValueError("复制期间旧资料库发生变化，已停止；请退出旧应用后重试")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    staging.replace(destination)
                else:
                    job = _json(destination / "job.json", {})
                    if job.get("import_fingerprint") != fingerprint:
                        raise ValueError("Migration destination belongs to another document")
                if old_id in source_drafts:
                    if new_id not in drafts:
                        drafts[new_id] = source_drafts[old_id]
                        draft_revisions[new_id] = job["revision"]
                if old_id in source_reading.get("documents", {}):
                    reading.setdefault("documents", {}).setdefault(new_id, source_reading["documents"][old_id])
                if source_reading.get("lastTaskID") == old_id and not reading.get("lastTaskID"):
                    reading["lastTaskID"] = new_id
                store._atomic_write_json(drafts_path, {"schema_version": 2, "tasks": {
                    key: {"segments": values, "revision": draft_revisions.get(key)} for key, values in drafts.items()}})
                store._atomic_write_json(reading_path, reading)
                record["complete"] = True
                store._atomic_write_json(manifest_path, manifest)
                report["imported"] += 1
        old_terms = _json(root / "config/glossary.json", {}).get("entries", [])
        terms = {entry["source"]: entry for entry in store.load_glossary()}
        for entry in old_terms:
            if isinstance(entry, dict) and entry.get("source") and entry.get("target"):
                terms.setdefault(entry["source"], entry)
        if terms:
            store.save_glossary(list(terms.values()))
        old_memory = root / "translation_memory.sqlite3"
        if old_memory.exists():
            import sqlite3
            from contextlib import closing
            from . import memory
            try:
                # Even SQLite mode=ro may create WAL/SHM beside the source.
                # Read a checked copy, including committed WAL records.
                with tempfile.TemporaryDirectory(prefix="memory-import-", dir=config.DATA_DIR) as temporary:
                    copied = Path(temporary) / old_memory.name
                    for suffix in ("", "-wal", "-shm"):
                        source_file = _source_file(root, Path(str(old_memory) + suffix))
                        if source_file.exists():
                            digest = output_transaction.file_hash(source_file)
                            destination_file = Path(str(copied) + suffix)
                            shutil.copy2(source_file, destination_file)
                            if digest != output_transaction.file_hash(destination_file) or digest != output_transaction.file_hash(source_file):
                                raise ValueError("翻译记忆在导入时发生变化，请退出来源应用后重试")
                    with closing(sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)) as before, closing(memory._connect()) as after, after:
                        for row in before.execute("SELECT source_lang,target_lang,source_text,provider_id,translation,verified,updated_at FROM translations"):
                            values = list(row)
                            values[3] = provider_map.get(values[3], "legacy:" + identity + ":" + values[3])
                            after.execute("INSERT OR IGNORE INTO translations VALUES (?,?,?,?,?,?,?)", values)
            except (OSError, sqlite3.Error) as error:
                report["issues"].append({"task_id": "", "message": "翻译记忆导入失败：" + str(error)})
        report["report_id"] = uuid.uuid4().hex
        store._atomic_write_json(config.DATA_DIR / "migration-reports" / (report["report_id"] + ".json"), report)
        return report
