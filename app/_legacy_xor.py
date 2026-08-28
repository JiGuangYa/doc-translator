"""Private XOR codec used only by the v0.1.x -> v0.2.0 legacy migration.

In v0.1.x, provider API keys were stored in ``data/config/providers.json``
as a hex-encoded XOR cipher with a hard-coded key. In v0.2.0 the API key
moved to the encrypted secret store (OS keyring or Fernet file fallback).
The codec lives here so that the secrets_store module does not expose
the XOR helpers as part of its public surface, and so the obvious import
``from app.utils import obfuscate_secret`` does not work for new code
(it would not, because the helpers were removed from app/utils.py).

This module is intentionally **not** part of ``app.utils``. The XOR scheme
is not a security boundary; it was a reversible obfuscation. Anyone
inspecting this file can see the key on disk. The migration runs exactly
once at the first start of v0.2.0 (see ``secrets_store.migrate_legacy_xor``);
operators who want to drop this file after migration are free to do so.
"""
from __future__ import annotations

_XOR_KEY = b"doc-translator-local-obfuscation"


def deobfuscate_legacy_xor(enc: str) -> str:
    """Reverse the v0.1.x XOR-with-hardcoded-key obfuscation.

    Returns the empty string on malformed input (the migration log
    records the failure separately). Raises only on catastrophic
    decode errors (e.g. ``bytes.fromhex`` blowing up on a non-string
    argument).
    """
    if not enc:
        return ""
    data = bytes.fromhex(enc)
    return bytes(b ^ _XOR_KEY[i % len(_XOR_KEY)] for i, b in enumerate(data)).decode("utf-8")
