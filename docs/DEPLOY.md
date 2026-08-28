# Deployment & security checklist

This document is the **single entry point** for the scattered deployment
and security guidance in `README.md`, `SECURITY.md`, and `docs/architecture.md`.
Read this end to end before exposing the service beyond `127.0.0.1`.

For end-user quick start (install + first run), see [README.md](../README.md).
For the threat model and cryptographic details, see [SECURITY.md](../SECURITY.md).
For the runtime architecture, see [architecture.md](architecture.md).

## Scenarios

The project ships one default scenario. Anything beyond it is opt-in and
requires explicit configuration.

| Scenario | Bind | Audience | Required env vars |
|---|---|---|---|
| **A (default)** | `127.0.0.1:8765` | up to 5 internal users on one host | none |
| **B (proxied HTTPS)** | `127.0.0.1:8765` behind reverse proxy with TLS | small team on a trusted LAN | `DOC_TRANSLATOR_COOKIE_SECURE=1`, `DOC_TRANSLATOR_REQUIRE_PUBLIC=1` |
| **C (LAN, no TLS)** | `0.0.0.0:8765` | **NOT recommended** — no CSRF token, no HSTS, no rate limit beyond login | none of the above; accept the risk |

If you need a multi-tenant SaaS, per-user accounts, or external auth, fork
and extend — the project explicitly does not grow those features
(see `CONTRIBUTING.md` "Scope of the project").

## Configuration cheatsheet

| Variable | Default | When to set |
|---|---|---|
| `HOST` | `127.0.0.1` | Almost never. Bind to `0.0.0.0` only with a reverse proxy in front. |
| `PORT` | `8765` | Only if 8765 is taken on the host. |
| `DOC_TRANSLATOR_COOKIE_SECURE` | `0` | `1` whenever the service is reached over HTTPS. |
| `DOC_TRANSLATOR_REQUIRE_PUBLIC` | `0` | `1` when the service cannot be reached on `127.0.0.1` from the LLM (i.e. only public cloud providers). Stays `0` if you point at a local Ollama / llama.cpp. |
| `PYTHONUNBUFFERED` | `1` | Leave alone; required for live log flushing. |
| `PYTHONIOENCODING` | `utf-8` | Leave alone; required for non-ASCII logs. |

`DOC_TRANSLATOR_COOKIE_SECURE` and `DOC_TRANSLATOR_REQUIRE_PUBLIC` are the
**only** two env vars that meaningfully change the security posture. Every
other knob is a deployment convenience.

## Reverse proxy notes

The service speaks plain HTTP on `127.0.0.1:8765`. Any non-loopback
deployment must front it with a TLS-terminating reverse proxy (Caddy,
nginx, traefik). Two things have to be true for the security features
in the application to fire under that proxy:

1. The proxy must set `X-Forwarded-Proto: https` on every request, and
   uvicorn must trust the header (the bundled `manage.*` scripts pass
   `--proxy-headers`, which does this by default).
2. HSTS is emitted only when `request.url.scheme == "https"`. If the
   proxy does not pass `X-Forwarded-Proto`, the HSTS header is never
   sent. The proxy itself should set HSTS independently.

Common reverse proxy stanzas (Caddy is the shortest):

```
# Caddy
localhost, lan.example.internal {
    reverse_proxy 127.0.0.1:8765
    encode zstd gzip
}

# nginx (excerpt)
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Real-IP         $remote_addr;
}
```

## Pre-flight checklist (before exposing beyond loopback)

- [ ] Service is behind a reverse proxy that terminates TLS.
- [ ] Proxy sets `X-Forwarded-Proto` on every request.
- [ ] `DOC_TRANSLATOR_COOKIE_SECURE=1` in the service environment.
- [ ] `DOC_TRANSLATOR_REQUIRE_PUBLIC=1` in the service environment
      (skip only if you must use a local llama.cpp/Ollama on 127.0.0.1
      as the LLM provider — and in that case the SSRF guard is off
      because loopback is a legitimate target).
- [ ] Admin password is at least 12 characters (the code minimum is 8;
      12 is the operator guidance in `SECURITY.md`).
- [ ] `data/` directory is `chmod 700` on Linux or has a tight NTFS ACL
      on Windows; the process runs as a dedicated low-privilege user.
- [ ] The reverse proxy or a separate job takes daily backups of
      `data/config/admin.json` and `data/secrets/`. The Fernet fallback
      file under `data/secrets/` is bound to the host's machine
      fingerprint; restoring it on a different host will **not** decrypt.

## Local LLM providers (Ollama, llama.cpp)

If the LLM runs on the same host, the SSRF guard must stay **off**
(`DOC_TRANSLATOR_REQUIRE_PUBLIC=0`), otherwise `127.0.0.1` is rejected
at every request. The two safety nets that still apply:

- The `SameSite=Strict` cookie plus the Origin/Referer check in
  `local_guard` (see `app/main.py`) blocks cross-site writes from a
  malicious web page.
- The cookie does not need `Secure` because the service is HTTP loopback;
  leave `DOC_TRANSLATOR_COOKIE_SECURE=0`.

## Data directory layout

```
data/
├── config/
│   ├── providers.json     # provider list (api_key NOT stored here)
│   ├── settings.json      # source/target lang, batch sizes, etc.
│   └── auth.json          # PBKDF2 password hash for the admin
├── secrets/               # encrypted secret store (keyring or Fernet)
│   ├── .salt              # 16-byte random salt
│   ├── .master            # Fernet master key (encrypted)
│   └── providers.json     # encrypted secrets index
├── tasks/<task-id>/
│   ├── original.<ext>     # uploaded source file
│   ├── translated.<ext>   # final translated file
│   ├── job.json           # segments, translations, status
│   ├── progress.json      # per-batch checkpoint
│   └── previews/          # cached LibreOffice page PNGs
└── logs/
    ├── server.log         # service stdout (uvicorn)
    ├── server.err         # service stderr
    ├── app.log            # Python application log (rotating, 2 MB x 1)
    └── audit.jsonl        # one JSON object per sensitive action
```

`data/` must be excluded from version control (see `.gitignore`).

## Upgrading

`./manage.sh update` / `.\manage.ps1 update` stashes local changes,
pulls `main`, refreshes pinned dependencies in `.venv`, and restarts
the service if it was running. The data directory is **not** touched.

Breaking changes between minor versions are listed at the top of the
next [CHANGELOG.md](../CHANGELOG.md) entry. From 0.1.x to 0.2.0, the
only breaking change for operators is the move of API keys from a
`providers.json` XOR ciphertext to the encrypted secret store; the
migration is automatic on first start (a `.bak.<timestamp>` sibling of
the legacy file is written before the new store is populated).
