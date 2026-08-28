# doc-translator

> **Local-only document translation service.** Upload a Word, PowerPoint, Excel, or PDF, translate it with **your own** LLM API, and download a file that keeps the original formatting. The two-pane web UI shows the source and the translation side by side.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue) ![License](https://img.shields.io/badge/License-MIT-green) ![Version](https://img.shields.io/badge/version-0.2.0-orange)

## What it does

- **Four formats** — `.docx`, `.pptx`, `.xlsx`, `.pdf`. The translated file keeps the original layout, fonts, and styles.
- **Bring your own LLM** — any OpenAI-compatible endpoint works: DeepSeek, Qwen, Kimi, OpenRouter, Ollama (local), or your own gateway. Just fill in `base_url`, `api_key`, and `model`.
- **High-fidelity preview** — the web UI renders pages server-side with LibreOffice (so it looks the same as in Office/WPS) and shows paragraph-level two-pane comparison with synchronized scrolling. Falls back to a browser-side renderer if LibreOffice is not installed.
- **Manual revision** — edit any paragraph in the translation and the file is rewritten on the spot.
- **Password auth + audit log** — first-run sets an admin password (PBKDF2-hashed). All sensitive actions (uploads, deletes, config changes) are recorded in `data/logs/audit.jsonl`.
- **Local only** — your documents stay on the machine in `data/`. API keys are stored encrypted (keyring with Fernet-file fallback) and never sent anywhere except the configured LLM provider.
- **Resumable** — if the connection drops or the service restarts, translation resumes from the last checkpoint. No wasted tokens.

## When to use it

Designed for **Scenario A: a single host on an internal network, used by up to 5 people**. The service binds to `127.0.0.1:8765` by default. If you put it behind a reverse proxy with TLS, enable `DOC_TRANSLATOR_REQUIRE_PUBLIC=1` and `DOC_TRANSLATOR_COOKIE_SECURE=1` (see [Configuration](#configuration)).

It is not a multi-tenant SaaS. There is exactly one admin password and a shared in-process session store. If you need per-user accounts, quotas, or external auth, fork and extend.

> **About to expose this beyond `127.0.0.1`?** Read [docs/DEPLOY.md](docs/DEPLOY.md) first. It is the single entry point for the security and reverse-proxy requirements that are otherwise spread across this README, `SECURITY.md`, and `docs/architecture.md`.

## Quick start

### Option 1: one-line install (recommended)

Requires [git](https://git-scm.com/) and [Python 3.10+](https://www.python.org/downloads/) already on the machine.

```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/ilysom0611/doc-translator/main/install.sh | bash

# Windows (PowerShell)
powershell -c "irm https://raw.githubusercontent.com/ilysom0611/doc-translator/main/install.ps1 | iex"
```

The script clones the repo to `~/doc-translator`, creates a virtualenv, and installs pinned dependencies. Then:

```bash
# Linux / macOS
cd ~/doc-translator && ./manage.sh start

# Windows
cd ~\doc-translator ; .\manage.bat start
```

The service starts in the background and writes logs to `data/server.log`. Open http://127.0.0.1:8765 in your browser.

### Option 2: run it directly

```bash
git clone https://github.com/ilysom0611/doc-translator.git
cd doc-translator
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

### First-time setup

1. Open http://127.0.0.1:8765 — the UI prompts you to **set the admin password** (min 8 chars).
2. Go to **Settings → Providers** and add an LLM. Example for DeepSeek:
   - **Name**: `DeepSeek`
   - **Base URL**: `https://api.deepseek.com/v1`
   - **Model**: `deepseek-chat`
   - **API Key**: your key
   - Click **Test connection** — the row turns green when it works.
3. Go to **Tasks**, drag a document in, pick the target language, and start.
4. When it's done, download the translated file from the right pane.

## Service management

`manage.sh` (Linux/macOS) and `manage.bat` / `manage.ps1` (Windows) wrap the same operations. Every command is **idempotent** — running it twice is safe and has no side effect when the service is already in the desired state.

| Command         | What it does                                                                                |
|-----------------|---------------------------------------------------------------------------------------------|
| `install`       | Create or repair the virtualenv and install locked dependencies. Safe to re-run.            |
| `start`         | Start in the background; perform a health check; skip if already running.                   |
| `stop`          | Graceful stop (SIGTERM, 30 s drain). No-op if not running.                                  |
| `restart`       | `stop`, then `start`.                                                                       |
| `status`        | Print whether the service is running. Exit code `3` if not (handy for shell scripts).       |
| `update`        | Pull latest code, refresh deps, restart if needed. Stashes local changes; never touches `data/`. |

Logs and PID file live in `data/server.log`, `data/server.err`, and `data/server.pid`.

## Configuration

All settings are environment variables. None are required for a default local install.

| Variable                              | Default       | Purpose                                                                                  |
|---------------------------------------|---------------|------------------------------------------------------------------------------------------|
| `HOST`                                | `127.0.0.1`   | Bind address. **Do not** change to `0.0.0.0` without TLS in front.                       |
| `PORT`                                | `8765`        | TCP port.                                                                                |
| `DOC_TRANSLATOR_COOKIE_SECURE`        | `0`           | Set to `1` / `true` to mark the session cookie `Secure` (required when serving over HTTPS). |
| `DOC_TRANSLATOR_REQUIRE_PUBLIC`       | `0`           | Set to `1` to **reject** provider `base_url` values that resolve to private/loopback IPs (SSRF guard). |
| `PYTHONUNBUFFERED`                    | `1`           | Unbuffered stdout/stderr so logs flush in real time.                                     |

The session cookie name is `dt_session`. The admin password hash is stored in `data/config/admin.json` (PBKDF2-HMAC-SHA256, 200 000 iterations, 16-byte random salt).

## Observability

| Endpoint            | Purpose                                                                        |
|---------------------|--------------------------------------------------------------------------------|
| `GET /healthz`      | Liveness — returns `200` if the process is up. Unauthenticated.                |
| `GET /readyz`       | Readiness — `200` only if the service is accepting work (not in shutdown).     |
| `GET /metrics`      | Prometheus exposition. **Unauthenticated by design** for the loopback scrape. |
| `GET /api/version`  | `{ "version": "0.2.0", "name": "doc-translator" }`                             |
| `GET /openapi.json` | OpenAPI 3 spec.                                                                |
| `GET /docs`         | Swagger UI.                                                                    |
| `GET /redoc`        | ReDoc UI.                                                                      |
| `GET /api/i18n`     | List of shipped UI language catalogs.                                          |
| `GET /api/i18n/{lang}.json` | Full key/value catalog for a language. Falls back to English if unknown. |

Exposed metrics include HTTP request count and latency, LLM call count, LLM token usage, LLM call latency, in-flight translation tasks, and process start time. The labels are intentionally low-cardinality (`method`, `path` template, `status`, `provider`) so Prometheus cardinality stays bounded.

`docker compose --profile metrics up` starts a Prometheus container that scrapes `/metrics` every 15 s with 7-day retention. See `deploy/prometheus.yml`.

## Deployment

### Bare-metal / VM (the supported default)

Use the `manage.*` scripts. They handle venv creation, dependency install, foreground/background start, and graceful stop. The `update` command stashes local edits, pulls, and restarts.

### Docker (single-host)

```bash
docker build -t doc-translator:0.2.0 .
docker compose up -d                # or: docker compose --profile metrics up -d
```

The image is `python:3.12-slim`, runs as the default user, exposes `/app/data` as a volume, and binds to `127.0.0.1:8765` **inside the container**. The compose file maps the container port to the **host's loopback** (`127.0.0.1:8765:8765`), so the service is reachable only from the host — not the LAN. To expose to the LAN, change the compose `ports:` to `"8765:8765"`, set `DOC_TRANSLATOR_REQUIRE_PUBLIC=1`, and put a reverse proxy with TLS in front. **Do not** bind directly to the LAN without TLS.

The `stop_grace_period: 35s` setting gives the FastAPI lifespan handler time to wait for in-flight translation tasks to drain (30 s timeout inside the process).

## Security model

- **API keys** are stored via the OS keyring when available (Windows Credential Manager, macOS Keychain, Secret Service on Linux). When keyring is unavailable, they fall back to a Fernet-encrypted file under `data/secrets/` whose key is derived via PBKDF2-HMAC-SHA256 (200 000 iterations) from a 16-byte random salt and a per-machine fingerprint. Keys are never written in plaintext and never logged. `providers.json` only stores the key reference (`has_api_key: true/false`), not the secret itself.
- **SSRF guard** (opt-in via `DOC_TRANSLATOR_REQUIRE_PUBLIC=1`) rejects provider `base_url` values that resolve to private, loopback, or link-local IPs.
- **Login rate limit** is 5 attempts per minute per IP (`slowapi`, in-memory storage). Exceeding it returns `429`.
- **Session cookie** is `HttpOnly`, `SameSite=Strict`. Add `Secure` via `DOC_TRANSLATOR_COOKIE_SECURE=1` when serving over HTTPS. The `Strict` setting plus the same-origin API and the Origin/Referer check in `local_guard` is what blocks cross-site writes from malicious web pages even when the service is bound to `127.0.0.1`.
- **Security headers** on every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and a conservative `Content-Security-Policy` (exempt for `/static/*`).
- **Request ID** (`X-Request-ID`) is generated on entry, returned on every response, and included in every error body and log line for correlation.
- **Global exception handler** returns uniform JSON `{detail, request_id}` and never leaks Python tracebacks to the client. Internal errors are logged at ERROR with the request_id.
- **All `/api/*` routes require an authenticated session** except `/api/auth/*` and `/api/i18n/*`. A path-prefix allow-list (`_is_exempt`) is used instead of a hard-coded path set so new public routes can be added without a router edit.

See [SECURITY.md](SECURITY.md) for the full threat model and reporting process.

## Format support

| Format | Fidelity | Notes                                                                                                  |
|--------|----------|--------------------------------------------------------------------------------------------------------|
| Word   | ★★★     | Body, tables, text boxes, headers/footers, footnotes. Inline mixed styles (e.g. bold mid-sentence) collapse to paragraph-start style. |
| PPT    | ★★★     | Text frames, tables, grouped shapes, speaker notes. Chart and SmartArt text are not auto-translated (the UI shows a banner). Long CN→EN translations may overflow text frames — shrink font manually. |
| Excel  | ★★☆     | Cell text and all cell styles preserved. **Charts and images are dropped** (openpyxl limitation). Formulas are passed through unchanged. |
| PDF    | ★★☆     | Approximate layout (block-level positioning). Fonts are substituted with the built-in CJK fallback. Scanned PDFs (no text layer) are not supported. |

## Data layout

See [docs/DEPLOY.md](docs/DEPLOY.md#data-directory-layout) for the full tree with annotations.

## Development

```bash
python -m pytest tests/ -v          # 121 tests, ~2 s
python -m ruff check app/ tests/    # Lint (E/F/W/B, line-length 100)
```

Architecture: **FastAPI** backend + **vanilla JS** single-page frontend (no build step). Core flow is in `app/services/pipeline.py` (parse → batch → LLM → write back); format adapters live in `app/formats/`. The LLM client lives in `app/services/llm/client.py`; the central metrics registry in `app/metrics.py`; the secret store in `app/secrets_store.py`; the i18n catalog in `app/i18n/`.

Contributing guide: [CONTRIBUTING.md](CONTRIBUTING.md). Architecture overview: [docs/architecture.md](docs/architecture.md). Deployment checklist: [docs/DEPLOY.md](docs/DEPLOY.md). Release history: [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE).
