"""UI i18n catalogs (server-side fallback + on-the-wire for SPA).

Resolution order at lookup time:
  1. Exact lang (e.g. "zh-CN")
  2. Language family (e.g. "zh")
  3. Default language ("en")

Missing keys are returned as "{key}" so the UI shows the developer-facing
identifier rather than a blank label — easier to spot gaps in
translation.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

_DEFAULT_LANG = "en"
_LOCALES_DIR = Path(__file__).resolve().parent / "i18n"

_catalogs: dict[str, dict] = {}
_fallback: dict = {}


def _load(lang: str) -> dict | None:
    path = _LOCALES_DIR / f"{lang}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _catalog(lang: str) -> dict:
    if lang not in _catalogs:
        cat = _load(lang)
        _catalogs[lang] = cat or {}
    return _catalogs[lang]


def available_languages() -> list[dict]:
    """Return [{code, name}] for every shipped catalog."""
    out = []
    for p in sorted(_LOCALES_DIR.glob("*.json")):
        code = p.stem
        cat = _catalog(code)
        out.append({"code": code, "name": cat.get("_name", code)})
    return out


def get_catalog(lang: str) -> dict:
    """Return the full key/value catalog for a language (or the fallback
    if the language is not shipped)."""
    cat = _catalog(lang)
    if cat:
        return cat
    return _catalog(_DEFAULT_LANG)


def t(key: str, lang: str | None = None) -> str:
    """Translate a dotted key. Falls back through language family → default.

    Examples:
      t("auth.login.error", "zh-CN")
      t("nav.settings")
    """
    lang = lang or _DEFAULT_LANG
    for candidate in (lang, lang.split("-", 1)[0], _DEFAULT_LANG):
        cat = _catalog(candidate)
        v = cat.get(key)
        if v is not None:
            return v
    return "{" + key + "}"


def resolve_lang(requested: str | None) -> str:
    """Validate and normalize a client-supplied language code.

    Returns the shipped catalog code closest to the requested one, or
    the default language if no family match is found.
    """
    if not requested:
        return _DEFAULT_LANG
    requested = requested.strip()
    if _catalog(requested):
        return requested
    family = requested.split("-", 1)[0]
    for p in _LOCALES_DIR.glob("*.json"):
        if p.stem == family or p.stem.startswith(family + "-"):
            return p.stem
    return _DEFAULT_LANG
