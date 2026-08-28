# Security

This document describes the security model of **doc-translator** v0.2.0,
how to report a vulnerability, and what is — and is not — covered by the
hardening in this release.

## Threat model

doc-translator is a **single-host web service for internal use**. The
intended deployment is a trusted LAN (≤ 5 users) on `127.0.0.1:8765`,
optionally fronted by a reverse proxy with TLS. It is **not** a public
multi-tenant SaaS, and the security model assumes that the network path
between the user's browser and the service is not adversarial.

Inside that boundary, the v0.2.0 hardening addresses:

| Threat                              | Mitigation in v0.2.0                                                                                   |
|-------------------------------------|--------------------------------------------------------------------------------------------------------|
| Plaintext API keys in `providers.json` | Encrypted secret store: OS keyring preferred, Fernet-encrypted file fallback. PBKDF2-HMAC-SHA256 (200 000 iters) for the master key. |
| SSRF via a malicious provider URL   | `app/services/ssrf_guard.py` — opt-in via `DOC_TRANSLATOR_REQUIRE_PUBLIC=1`. Rejects non-http(s), literal private/loopback/link-local, and hostnames whose DNS resolves to those. |
| Brute-force the admin password      | `slowapi` login rate limit: 5 attempts per minute per IP. Returns 429.                                |
| Session cookie leaked over HTTP     | `DOC_TRANSLATOR_COOKIE_SECURE=1` makes the cookie `Secure` (off by default for the local HTTP case).   |
| Reflected XSS / clickjacking        | Security headers: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, conservative `Content-Security-Policy`. |
| Session fixation / CSRF             | `HttpOnly` + `SameSite=Strict` cookie; same-origin API; Origin/Referer check on write methods (see `local_guard` in `app/main.py`). |
| Information leak via error pages    | Global exception handler returns uniform `{detail, request_id}` JSON. Stack traces are logged, not sent. |
| Server-side log/audit correlation    | `X-Request-ID` on every request, returned in the response and embedded in every log line.              |
| Long-running translation orphaned on restart | FastAPI lifespan handler drains in-flight tasks on SIGTERM (30 s timeout). `docker stop` gives 35 s. |
| Unauthenticated access to internals  | `/healthz`, `/readyz`, `/metrics` are open; everything else under `/api/*` requires a session (except `/api/auth/*` and `/api/i18n/*`). |

### Explicit non-goals

- **No multi-tenant isolation.** There is one admin password. If user A
  uploads a file, user B can read it. Do not deploy on a shared host
  with untrusted users.
- **No CSRF token.** Defense is the `SameSite=Strict` cookie + same-origin
  API + the Origin/Referer check on write methods. This is acceptable
  for a local-only deployment, but if you need to embed the UI in an
  iframe on another origin, you will need to relax `SameSite` to `Lax`
  (and add a CSRF token).
- **No HTTPS.** The service speaks plain HTTP. Front it with a reverse
  proxy (Caddy, nginx, traefik) for any non-loopback deployment.
- **No secrets in version control.** The `data/` directory is
  git-ignored. The Fernet-encrypted file under `data/secrets/` contains
  ciphertext, not plaintext, and is useless without the master key
  (which is itself derived from a per-machine fingerprint).
- **No vulnerability scanning of uploaded documents.** Files are passed
  to format adapters and LibreOffice for rendering. A maliciously crafted
  Office document could exploit parser bugs in those libraries. Treat
  the data directory as you would an untrusted-file drop folder.

## Reporting a vulnerability

Please **do not open a public GitHub issue** for security problems.
Email `security@ilysom0611.dev` (or open a GitHub Security Advisory via
the "Security" tab of the repository). Include:

1. A short description of the issue.
2. Steps to reproduce.
3. The expected and actual behavior.
4. The version affected (commit SHA or tag).
5. Whether you want public credit in the CHANGELOG.

You should hear back within 7 days. We will coordinate disclosure and
credit.

## Hardening checklist (operators)

Before exposing the service beyond `127.0.0.1`:

- [ ] Set `DOC_TRANSLATOR_REQUIRE_PUBLIC=1` to enable the SSRF guard.
- [ ] Set `DOC_TRANSLATOR_COOKIE_SECURE=1` to require the session cookie over HTTPS.
- [ ] Front the service with a reverse proxy that terminates TLS and sets `Strict-Transport-Security`.
- [ ] Pick an admin password of at least 12 characters from a password manager.
- [ ] Restrict the data directory (`chmod 700` on Linux; NTFS ACL on Windows).
- [ ] Restrict the process to a dedicated low-privilege user.
- [ ] Back up `data/config/admin.json` and `data/secrets/` together. The
      Fernet file is only decryptable on the same machine, so a copy
      moved to a different host will not decrypt.

## Cryptographic details

| Item                | Algorithm / parameters                                                                              |
|---------------------|-----------------------------------------------------------------------------------------------------|
| Admin password hash | PBKDF2-HMAC-SHA256, 200 000 iterations, 16-byte random salt.                                        |
| Master key (Fernet) | PBKDF2-HMAC-SHA256, 200 000 iterations, 16-byte random salt + machine fingerprint.                 |
| Fernet token        | AES-128-CBC + HMAC-SHA256 (the `cryptography` library's Fernet implementation).                     |
| Session cookie      | Random 32-byte URL-safe value, opaque to the client.                                                |
| Request ID          | `secrets.token_hex(16)` — 32 hex chars. Not a security token; used only for log correlation.        |

## Dependency policy

- All runtime dependencies are **pinned to exact versions** in
  `requirements.txt`. Upgrades are one-at-a-time with a `pip-audit`
  check in CI.
- `pymupdf` and `Pillow` are pinned below versions that drop
  `manylinux2014` (glibc 2.17) wheels, because some enterprise hosts
  (notably CentOS 7.9) cannot build from source.
- The `cryptography` library is pinned to `43.0.3`. OpenSSL CVEs are
  fixed by bumping this version.
