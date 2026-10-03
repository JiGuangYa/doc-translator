"""Local exact-match translation memory, never sent to another service."""

import sqlite3
import time
from contextlib import closing

from .. import config


def _connect():
    path = config.DATA_DIR / "translation_memory.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("""CREATE TABLE IF NOT EXISTS translations (
        source_lang TEXT NOT NULL,
        target_lang TEXT NOT NULL,
        source_text TEXT NOT NULL,
        provider_id TEXT NOT NULL,
        translation TEXT NOT NULL,
        verified INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL,
        PRIMARY KEY (source_lang, target_lang, source_text, provider_id)
    )""")
    return connection


def lookup(source_lang: str, target_lang: str, source_text: str,
           provider_id: str) -> str | None:
    with closing(_connect()) as connection:
        row = connection.execute("""SELECT translation FROM translations
            WHERE source_lang=? AND target_lang=? AND source_text=?
              AND (provider_id=? OR verified=1)
            ORDER BY verified DESC, (provider_id=?) DESC, updated_at DESC LIMIT 1""",
            (source_lang, target_lang, source_text, provider_id, provider_id)).fetchone()
    return row[0] if row else None


def lookup_many(source_lang: str, target_lang: str, texts: list[str],
                provider_id: str) -> dict[str, str]:
    found = {}
    with closing(_connect()) as connection:
        for text in dict.fromkeys(texts):
            row = connection.execute("""SELECT translation FROM translations
                WHERE source_lang=? AND target_lang=? AND source_text=?
                  AND (provider_id=? OR verified=1)
                ORDER BY verified DESC, (provider_id=?) DESC, updated_at DESC LIMIT 1""",
                (source_lang, target_lang, text, provider_id, provider_id)).fetchone()
            if row:
                found[text] = row[0]
    return found


def remember(source_lang: str, target_lang: str, source_text: str,
             provider_id: str, translation: str, verified: bool = False) -> None:
    if not source_text.strip() or not translation.strip():
        return
    with closing(_connect()) as connection, connection:
        old = connection.execute("""SELECT verified FROM translations
            WHERE source_lang=? AND target_lang=? AND source_text=? AND provider_id=?""",
            (source_lang, target_lang, source_text, provider_id)).fetchone()
        if old and old[0] and not verified:
            return  # a model output must not replace a manually reviewed entry
        connection.execute("""INSERT OR REPLACE INTO translations
            (source_lang, target_lang, source_text, provider_id,
             translation, verified, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (source_lang, target_lang, source_text, provider_id,
             translation, int(verified), time.time()))


def remember_many(source_lang: str, target_lang: str, provider_id: str,
                  pairs: list[tuple[str, str]]) -> None:
    with closing(_connect()) as connection, connection:
        for source_text, translation in pairs:
            if not source_text.strip() or not translation.strip():
                continue
            old = connection.execute("""SELECT verified FROM translations
                WHERE source_lang=? AND target_lang=? AND source_text=? AND provider_id=?""",
                (source_lang, target_lang, source_text, provider_id)).fetchone()
            if old and old[0]:
                continue
            connection.execute("""INSERT OR REPLACE INTO translations
                (source_lang, target_lang, source_text, provider_id,
                 translation, verified, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?)""",
                (source_lang, target_lang, source_text, provider_id,
                 translation, time.time()))


def list_entries(limit: int = 100) -> list[dict]:
    with closing(_connect()) as connection:
        rows = connection.execute("""SELECT source_lang, target_lang, source_text,
            translation, verified, updated_at FROM translations
            ORDER BY updated_at DESC LIMIT ?""", (min(max(limit, 1), 500),)).fetchall()
    return [dict(source_lang=row[0], target_lang=row[1], source_text=row[2],
                 translation=row[3], verified=bool(row[4]), updated_at=row[5])
            for row in rows]


def count() -> int:
    with closing(_connect()) as connection:
        return connection.execute("SELECT count(*) FROM translations").fetchone()[0]


def clear() -> None:
    with closing(_connect()) as connection, connection:
        connection.execute("DELETE FROM translations")
