# Architecture

This document describes the runtime architecture of **doc-translator
v0.2.0**: the request path, the data path, the trust boundaries, and
the cross-cutting concerns (secrets, metrics, i18n).

## High-level

```
+--------------+     HTTP (loopback)
|   Browser    | ---------------------------->  FastAPI (Uvicorn)
|  (vanilla JS)| <----------------------------  127.0.0.1:8765
+--------------+       JSON + cookie              |
                                                  |  HTTPS to provider
                                                  v
                                          +--------------+
                                          |   LLM API    |
                                          | (DeepSeek,   |
                                          |  OpenRouter, |
                                          |  Ollama, ...)|
                                          +--------------+
```

The browser is a single-page app served from `static/`. It talks to
the FastAPI backend over the same origin (`127.0.0.1:8765`). The
backend talks outbound to the configured LLM provider over HTTPS.
No other network paths exist.

## Request lifecycle

```
Request
  |
  v
MetricsMiddleware           record http_requests_total, observe duration
  |
  v
SecurityHeadersMiddleware  add X-Content-Type-Options, X-Frame-Options,
  |                         Referrer-Policy, Content-Security-Policy
  v
RequestIDMiddleware         generate X-Request-ID, attach to logging
  |
  v
local_guard (Depends)       reject with 401 unless session cookie is
  |                         valid AND the path is not in the exempt
  |                         prefix list (/api/auth/*, /api/i18n/*,
  |                         /healthz, /readyz, /metrics, /, /static,
  |                         /openapi.json, /docs, /redoc)
  v
Route handler               @limiter.limit where applicable
  |
  v
Response                    with X-Request-ID echoed back
```

Middleware order is LIFO: `add_middleware` adds to the front of the
stack, so the order in `app/main.py` (Metrics -> SecurityHeaders ->
RequestID -> local_guard as a dependency) means Metrics is outermost
and `local_guard` runs after the middlewares. This is intentional so
that `/healthz` and `/metrics` are still recorded even if
`local_guard` would otherwise reject them.

## Secret storage

```
+----------------------------------------------------------+
|                     set_secret(name, value)              |
+--------------------+-------------------+----------------+
                     |                   |
                     v                   v
            +-----------------+  +----------------+
            |  OS keyring     |  |  Fernet file   |
            |  (preferred)    |  |  (fallback)    |
            |                 |  |  data/secrets/ |
            +-----------------+  +----------------+
```

Both backends are written for every secret so that `list_secret_names()`
works (keyring has no list API). Reading tries keyring first, then the
Fernet file. The Fernet key is derived from a 16-byte random salt and
a per-machine fingerprint (hostname + primary MAC) via
PBKDF2-HMAC-SHA256, 200 000 iterations. The salt is in
`data/secrets/.salt`; the encrypted key (after derivation) is in
`data/secrets/.master`.

The `providers.json` file in `data/config/` stores the provider list
**without** API keys - it has `has_api_key: true|false` derived from
`list_secret_names()`. A grep over `data/config/providers.json` will
never find an API key in plaintext.

## Metrics

The Prometheus registry lives in `app/metrics.py` and is exposed at
`/metrics`. Labels are intentionally low-cardinality:

| Metric                                | Labels                                  |
|---------------------------------------|-----------------------------------------|
| `http_requests_total`                 | `method`, `path` (template), `status`   |
| `http_request_duration_seconds`       | same                                    |
| `llm_calls_total`                     | `provider`, `status` (`ok` / `error`)   |
| `llm_tokens_total`                    | `provider`, `kind` (`prompt` / `completion`) |
| `llm_request_duration_seconds`        | `provider`                              |
| `tasks_in_flight`                     | (none)                                  |
| `tasks_total`                         | `status` (`started` / `succeeded` / `failed` / `cancelled`) |
| `process_start_time_seconds`          | (none)                                  |

The `path` label is the **route template** (e.g. `/api/tasks/{task_id}`),
not the resolved URL, so cardinality is bounded by the number of
routes.

## i18n

Catalogs are JSON files under `app/i18n/`:

```
app/i18n/
|-- en.json       default (English)
+-- zh-CN.json    Simplified Chinese
```

Resolution order at lookup time:

1. Exact match (`zh-CN`)
2. Language family (`zh`)
3. Default (`en`)

Missing keys return the literal `"{key}"` so the UI surfaces the
identifier rather than going blank. Catalogs are loaded lazily on
first use and cached for the process lifetime. New languages are added
by dropping a new `xx.json` file - no code change required.

The SPA fetches `/api/i18n` (list) and `/api/i18n/<lang>.json`
(catalog) at boot. The catalog endpoint is exempt from session auth
because the UI must be able to render the login page before the user
has a session.

## Graceful shutdown

The FastAPI lifespan handler:

1. On startup: migrate legacy `api_key_enc` (XOR) to the new
   `secrets_store`, then yield control to the app.
2. On shutdown: set `_shutting_down = True`, wait up to 30 s for
   `task_manager.count_in_flight()` to reach 0, then return.

`/readyz` returns `503` once `_shutting_down` is set, so a load
balancer can drain the instance. The Docker `stop_grace_period: 35s`
gives the process enough time to drain.

## Pipeline

The translation pipeline is in `app/services/pipeline.py`:

```
upload -> format adapter parses -> segments extracted
        -> batches of N segments
        -> LLM (with metrics + retry)
        -> format adapter writes back
        -> progress.json checkpoint per batch
```

Resumability is provided by the per-batch checkpoint: if the
connection drops, the next start reads `progress.json` and skips
batches that are already complete. The pipeline uses
`chat_completion_with_metrics` (not `chat.completions.create`
directly) so all LLM calls are instrumented.

## Module boundaries

| Layer            | Imports from                                | Imported by                      |
|------------------|---------------------------------------------|----------------------------------|
| `app/main.py`    | everything (composition root)               | uvicorn                          |
| `app/api/*`      | `app/services/*`, `app/store`, `app/auth`   | `app/main.py`                    |
| `app/services/*` | `app/secrets_store`, `app/metrics`, `app/i18n` | `app/api/*`                    |
| `app/formats/*`  | only stdlib + format-specific libraries     | `app/services/pipeline.py`       |
| `app/store.py`   | `app/secrets_store`, `app/config`           | `app/api/*`, `app/main.py`       |
| `app/secrets_store.py` | `cryptography`, `keyring`, stdlib      | `app/store.py`, `app/main.py`    |
| `app/metrics.py` | `prometheus_client`                         | middlewares, `app/services/llm/`, `app/services/task_manager.py` |

The dependency direction is strictly downward. `app/formats/*` is
deliberately isolated: it has no dependency on any other module in
the project, only on its format library. This means a new format can
be added without touching the rest of the codebase.
