"""Commit document bytes and their job metadata together, recovering after a crash."""

import hashlib
import logging
import os
import shutil
import uuid
from pathlib import Path

from .. import store

JOURNAL = ".output-transaction.json"


def file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def commit(task_id: str, pending: Path, destination: Path, old_job: dict, new_job: dict):
    """Caller holds the task lock and has validated the candidate file."""
    directory = destination.parent
    journal = directory / JOURNAL
    backup = None
    if destination.exists():
        versions = directory / "versions"
        versions.mkdir(exist_ok=True)
        backup = versions / f"translated-r{old_job.get('revision', 0)}-{uuid.uuid4().hex[:8]}{destination.suffix}"
        shutil.copy2(destination, backup)
    record = {"old_job": old_job, "new_job": new_job, "pending": pending.name,
              "destination": destination.name, "old_hash": file_hash(destination),
              "new_hash": file_hash(pending)}
    store._atomic_write_json(journal, record)
    replaced = False
    try:
        os.replace(pending, destination)
        replaced = True
        store.save_job(task_id, new_job)
    except Exception:
        # A disk-full metadata write must leave the last valid document in place.
        try:
            if replaced:
                if backup is not None:
                    os.replace(backup, destination)
                else:
                    destination.unlink(missing_ok=True)
            journal.unlink(missing_ok=True)
        except OSError:
            # The durable journal lets startup finish an interrupted commit.
            logging.getLogger(__name__).exception("Output rollback deferred for %s", task_id)
        raise
    journal.unlink(missing_ok=True)


def recover() -> list[str]:
    """Resolve only durable, validated output transactions before accepting work."""
    from .. import config
    recovered = []
    for journal in config.TASKS_DIR.glob(f"*/{JOURNAL}"):
        task_id = journal.parent.name
        with store.task_lock(task_id):
            record = store._read_json(journal, None)
            if not record:
                logging.getLogger(__name__).error("Unreadable output journal for %s", task_id)
                continue
            try:
                if any(record[key].get("task_id") != task_id for key in ("old_job", "new_job")):
                    raise ValueError("Invalid transaction task")
                if not isinstance(record.get("new_hash"), str) or len(record["new_hash"]) != 64:
                    raise ValueError("Invalid transaction hash")
                for key in ("pending", "destination"):
                    if Path(record[key]).name != record[key]:
                        raise ValueError("Invalid transaction path")
                destination = journal.parent / record["destination"]
                pending = journal.parent / record["pending"]
                current_hash = file_hash(destination)
                if current_hash == record["new_hash"]:
                    job = record["new_job"]
                elif current_hash == record["old_hash"]:
                    if pending.exists() and file_hash(pending) == record["new_hash"]:
                        os.replace(pending, destination)
                        job = record["new_job"]
                    else:
                        job = record["old_job"]
                else:
                    # The user edited the output after the interruption. Keep it
                    # and retain the app's proposed text for explicit rebuilding.
                    job = record["new_job"]
                    job["external_edit"] = True
                    job["translated_sha256"] = current_hash
                    job["warnings"] = list(dict.fromkeys((job.get("warnings") or []) + [
                        "An external edit was preserved during recovery; review output versions before rebuilding."]))
                    if pending.exists():
                        versions = journal.parent / "versions"
                        versions.mkdir(exist_ok=True)
                        pending.replace(versions / f"app-recovered-{uuid.uuid4().hex[:8]}{destination.suffix}")
                store.save_job(task_id, job)
                pending.unlink(missing_ok=True)
                journal.unlink()
                recovered.append(task_id)
            except (OSError, ValueError, KeyError):
                logging.getLogger(__name__).exception("Could not recover output for %s", task_id)
    return recovered
