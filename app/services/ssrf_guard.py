"""SSRF guard for provider base_url.

Scenario A (single-host internal network) keeps the guard DISABLED by default
so users can run a local llama.cpp/Ollama on 127.0.0.1. For deployments that
only serve public providers, set the env var DOC_TRANSLATOR_REQUIRE_PUBLIC=1.

When enabled, a base_url is rejected if:
  - its scheme is not http(s)
  - its host is a literal private/loopback/link-local IP
  - its host is a name that DNS-resolves to a private/loopback/link-local IP

Returns (ok: bool, reason: str). reason is "" on success.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

# Reading the env at module load is fine — but we also expose reload_config()
# for tests that mutate the env mid-run.
_ENABLED: bool = os.environ.get("DOC_TRANSLATOR_REQUIRE_PUBLIC", "").strip() in ("1", "true", "yes", "on")


def reload_config() -> None:
    global _ENABLED
    _ENABLED = os.environ.get("DOC_TRANSLATOR_REQUIRE_PUBLIC", "").strip() in ("1", "true", "yes", "on")


def is_enabled() -> bool:
    return _ENABLED


def _is_private_ip(addr: str) -> tuple[bool, str]:
    """Return (is_private, label). label is the human reason if private."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False, ""
    if ip.is_loopback:
        return True, "loopback"
    if ip.is_private:
        return True, "private (RFC1918)"
    if ip.is_link_local:
        return True, "link-local (169.254/16)"
    if ip.is_multicast:
        return True, "multicast"
    if ip.is_reserved:
        return True, "reserved"
    if ip.is_unspecified:
        return True, "unspecified"
    return False, ""


def check_base_url(url: str) -> tuple[bool, str]:
    """Validate a provider base_url against SSRF policy.

    Returns (ok, reason). reason is "" on success.
    If the guard is disabled, returns (True, "") for any well-formed http(s) URL.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False, "URL cannot be parsed"
    if parts.scheme not in ("http", "https"):
        return False, f"Unsupported scheme: {parts.scheme!r} (only http/https allowed)"
    host = parts.hostname or ""
    if not host:
        return False, "URL is missing a host"

    if not _ENABLED:
        return True, ""

    # Literal IP: short-circuit.
    try:
        ipaddress.ip_address(host)
        is_priv, label = _is_private_ip(host)
        if is_priv:
            return False, f"Target {host} is a {label} address — SSRF guard is enabled"
        return True, ""
    except ValueError:
        pass  # not a literal IP — must DNS-resolve

    # DNS resolve and check every returned address (covers DNS rebinding).
    try:
        infos = socket.getaddrinfo(host, parts.port or 443)
    except socket.gaierror as e:
        return False, f"DNS resolution failed: {e}"

    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0]
        is_priv, label = _is_private_ip(addr)
        if is_priv:
            return False, f"{host} resolves to {label} address {addr} — SSRF guard is enabled"
    return True, ""
