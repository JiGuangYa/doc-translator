# Changelog

All notable changes to **doc-translator** are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/), and the project
adheres to [Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-08-27

### Security (C1 hardening)

- **P0#1 — API key storage rewritten.** The XOR-with-hardcoded-key scheme
  in `app/utils.py` is gone. Provider API keys now live in
  `app/secrets_store.py`, which prefers the OS keyring (macOS Keychain,
  Windows Credential Manager, Linux Secret Service) and falls back to a
  Fernet-encrypted JSON file at `data/secrets/providers.json` keyed by
  PBKDF2-SHA256 (200k iterations) over a per-machine fingerprint. The
  on-disk file is `chmod 0600`. `providers.json` no longer contains any
  ciphertext at all — only non-sensitive metadata. A one-shot
  `migrate_legacy_xor()` runs in the FastAPI lifespan startup to move
  pre-v0.2.0 XOR ciphertexts into the new store and rewrite the legacy
  file in place (with a `.bak.<ts>` backup).
- **P0#2 — Login rate limit.** `app/services/rate_limit.py` exposes a
  slowapi `Limiter` keyed by remote address. `POST /api/auth/login` is
  capped at **5 attempts per minute per IP**; the 6th attempt within the
  window returns `429` with a `Retry-After: 60` header. `/api/auth/setup`
  is intentionally not limited (one-time operation).
- **P0#3 — Cookie `Secure` opt-in.** `_set_session_cookie()` now consults
  the env var `DOC_TRANSLATOR_COOKIE_SECURE` (1/true/yes/on). Default
  off (Scenario A is HTTP loopback). When the service is reverse-proxied
  behind HTTPS termination, set the env var to 1 and the cookie will
  refuse to traverse plain HTTP.
- **P0#4 — SSRF guard for `base_url`.** New `app/services/ssrf_guard.py`
  validates the provider's `base_url` at both write time (provider
  create/update) and at every LLM call (DNS rebinding protection inside
  `build_client()`). When enabled via `DOC_TRANSLATOR_REQUIRE_PUBLIC=1`,
  any URL whose host is a literal private/loopback/link-local IP — or
  whose hostname DNS-resolves to one — is rejected. Default off so
  Scenario A users can run a local llama.cpp/Ollama on 127.0.0.1.

### Compatibility

- **All 81 existing+new tests pass.** No breaking changes to API
  consumer-visible behavior other than the above.
- The legacy `api_key_enc` field in `providers.json` is migrated and
  removed at first startup. Operators who deploy v0.2.0 over a previous
  install do not need to take any action.

### Known limitations

- Rate limiter uses `slowapi.MemoryStorage`; per-process only. For
  multi-process deployments (gunicorn workers, etc.) switch to
  `limits.aio.storage.RedisStorage` in `app/services/rate_limit.py`.
- Secret store's Fernet fallback is bound to the machine fingerprint.
  Restoring `data/secrets/` on a different host will not decrypt. For
  multi-host deployments, provision a shared keyring backend (e.g.
  HashiCorp Vault) or persist the master key out-of-band.

### Observability & deploy (C2 hardening)

- **P0#5 — System endpoints.** `GET /healthz` (liveness), `GET /readyz`
  (readiness: probes `data/` is writable), and `GET /metrics`
  (Prometheus text exposition) are now anonymous. Intended for
  in-cluster scrapers / orchestrators. `docker-compose.yml` wires the
  healthcheck to `/healthz`.
- **P0#6 — Graceful shutdown.** `app/services/task_manager.py` exposes
  `count_in_flight()` and `wait_for_drain(timeout_s)`. The FastAPI
  lifespan flips a `_shutting_down` flag after `yield` and waits up to
  30 seconds for in-flight translations to finish before letting
  uvicorn exit. Uvicorn already forwards SIGTERM into this path, so
  no separate signal handler is needed.
- **LLM usage tracking.** New wrapper
  `client.chat_completion_with_metrics(provider_id, **kwargs)` records
  `llm_calls_total{provider,status}`, `llm_tokens_total{provider,kind}`,
  and `llm_request_duration_seconds{provider}`. All three translator
  call sites (`_translate_batch_with_retry`, the first-round refill,
  and the second-round small-group refill) now go through it.
- **HTTP metrics middleware.** Every served request increments
  `http_requests_total{method,route,status}` and observes a latency
  histogram. The route is the FastAPI template
  (`/api/tasks/{task_id}`), not the raw URL — keeps cardinality
  bounded.
- **Security headers + request ID.** `SecurityHeadersMiddleware` adds
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer` to all non-static responses, plus
  `Strict-Transport-Security` when the request was over HTTPS.
  `RequestIDMiddleware` echoes or generates `X-Request-ID` for log
  correlation.
- **Deployment assets.** `Dockerfile` (python:3.12-slim, loopback bind,
  `/app/data` volume, uvicorn CMD so SIGTERM reaches FastAPI lifespan),
  `docker-compose.yml` (loopback port mapping, healthcheck, 35s stop
  grace period, optional `--profile metrics` Prometheus sidecar), and
  `deploy/prometheus.yml` (15s scrape, 7d retention).

### Compatibility

- All 105 tests pass. No breaking changes to API consumer-visible
  behavior.

### Quality, i18n, and docs (C3 hardening)

- **OpenAPI and version endpoint.** `GET /openapi.json`, `GET /docs`
  (Swagger UI), and `GET /redoc` are re-enabled and the version is
  read from `pyproject.toml` via `importlib.metadata` with a fallback.
  New `GET /api/version` returns `{name, version}` for the SPA to
  show in the footer. 5 new tests pin the spec content and the
  route shapes.
- **Uniform error responses.** A single global exception handler
  replaces FastAPI's default. `RequestValidationError` returns the
  uniform shape `{detail, request_id}`; unhandled exceptions return
  `500` with a generic `detail` and the `request_id` for log
  correlation. Python tracebacks are never sent to the client;
  they are logged at `ERROR` with the `request_id`. The 404 path
  also returns JSON, not Starlette's HTML default. 3 new tests pin
  each behavior.
- **Server-side i18n framework.** `app/i18n.py` resolves a dotted
  key through exact -> language family (e.g. `zh-TW` -> `zh`) ->
  default (`en`). Missing keys return the literal `"{key}"` so the
  UI shows the developer identifier rather than going blank.
  Catalogs are JSON files under `app/i18n/`. Two languages ship:
  - `en.json` — default, English (~50 keys covering nav, auth,
    providers, settings, tasks, common, health).
  - `zh-CN.json` — Simplified Chinese with the same keys.
  New anonymous endpoints: `GET /api/i18n` (list of shipped
  catalogs) and `GET /api/i18n/<lang>.json` (full catalog, falls
  back to English for unknown languages). 8 new tests pin the
  resolution order and the endpoints.
- **English README rewrite.** The README is now English-first
  (was Chinese-first) to match the chosen language for the OpenAPI
  spec, error shape, and UI catalogs. New sections document the
  scenario model (Scenario A: single-host, 5-user), env-var
  configuration, observability endpoints, deployment paths
  (bare-metal and Docker with explicit guidance against binding
  0.0.0.0 without TLS), the security model, and the on-disk data
  layout.
- **Governance docs.**
  - `SECURITY.md` — threat model, what is and is not covered,
    vulnerability reporting process (email / GitHub Security
    Advisory), operator hardening checklist, cryptographic details
    (PBKDF2-SHA256 200k iters, Fernet AES-128-CBC + HMAC-SHA256),
    and the dependency policy (pinned versions, `pip-audit` in CI,
    `pymupdf` / `Pillow` held below the glibc-2.17 drop).
  - `CONTRIBUTING.md` — development setup, project layout, style
    (`ruff` E/F/W/B, line-length 100), testing rules (every public
    function, no network in tests, autouse isolation fixture),
    commit and release conventions, and explicit scope limits (no
    multi-tenant, no new UI framework, no new auth backends).
  - `docs/architecture.md` — runtime topology, request lifecycle
    through the LIFO middleware chain, secret store diagram,
    metrics label policy (route templates, not resolved paths),
    i18n resolution order, graceful shutdown sequence, pipeline
    data path, and the module dependency matrix.

### Compatibility

- All 121 tests pass. No breaking changes to API consumer-visible
  behavior.
- A new public endpoint path prefix (`/api/i18n/*`) is added; if
  you have a reverse proxy that filters by URL, allow this prefix.
- `GET /api/version` and the `OpenAPI` documentation are
  re-enabled; if you have a reverse proxy that hides these, the
  defaults are now visible.

### Summary of 0.2.0

This release is an **enterprise hardening** of the 0.1.x shape. No
new user-facing features, no new translation formats, no new
translation backends. The scope was strictly: encrypt the API keys,
guard the SSRF surface, rate-limit login, opt-in-secure the cookie,
add observability, drain on shutdown, ship a deploy story, ship a
security story, and ship English docs.

| Area            | 0.1.x                          | 0.2.0                                                                  |
|-----------------|--------------------------------|------------------------------------------------------------------------|
| API key storage | XOR with hardcoded key         | OS keyring + Fernet file fallback (PBKDF2-SHA256 200k iters)           |
| Login limit     | none                           | 5/min per IP (slowapi)                                                 |
| Cookie `Secure` | always off                     | opt-in via `DOC_TRANSLATOR_COOKIE_SECURE=1`                            |
| SSRF guard      | none                           | opt-in via `DOC_TRANSLATOR_REQUIRE_PUBLIC=1`                           |
| Security headers| none                           | CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, HSTS-over-HTTPS |
| Request ID      | none                           | `X-Request-ID` echoed or generated, in response and logs               |
| Error responses | FastAPI default                | uniform `{detail, request_id}`; no traceback leakage                    |
| Liveness        | none                           | `/healthz`, `/readyz`, `/metrics` (Prometheus)                         |
| Shutdown        | kill on SIGTERM                | drain in-flight tasks, 30s timeout                                     |
| Metrics         | none                           | HTTP, LLM (count, tokens, latency), tasks (in-flight, total)            |
| OpenAPI docs    | disabled                       | `/openapi.json`, `/docs`, `/redoc`, `/api/version`                      |
| i18n            | hard-coded Chinese             | server-side catalogs, en + zh-CN shipped, fetchable by SPA             |
| README          | Chinese-first                  | English-first; scenario model, config, deploy, security, data layout   |
| Governance      | LICENSE only                   | SECURITY.md, CONTRIBUTING.md, docs/architecture.md                     |
| Container image | none                           | `python:3.12-slim`, loopback bind, volume for `data/`                  |

### Upgrade notes

- The migration is automatic: on first start of v0.2.0, the lifespan
  handler detects legacy `api_key_enc` rows in `providers.json` and
  moves them into the new secret store, rewriting the legacy file
  in place with a `.bak.<timestamp>` sibling. No operator action
  required.
- If you deploy behind a reverse proxy with TLS, set
  `DOC_TRANSLATOR_COOKIE_SECURE=1` and `DOC_TRANSLATOR_REQUIRE_PUBLIC=1`
  before starting the service. The defaults remain off so the
  default local install (Scenario A) works without configuration.
- If you scrape Prometheus, point the scraper at `/metrics` and use
  the `deploy/prometheus.yml` reference. Labels are route templates,
  not resolved URLs — do not scrape a label that does not exist.
- The rate limiter is in-process (slowapi MemoryStorage). For a
  multi-worker deployment, swap to a Redis-backed `limits` storage
  in `app/services/rate_limit.py`. This is called out in
  `SECURITY.md` under known limitations.
