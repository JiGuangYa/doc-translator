# Contributing

Thanks for considering a contribution. doc-translator is a small
service, intentionally — the goal of v0.2.0 was to harden the existing
shape, not to grow it. Most useful contributions fit one of the
buckets below.

## Before you start

- **Open an issue first** for non-trivial changes. The maintainer can
  tell you whether the change fits the project's scope and which branch
  to target.
- **Small bug fixes and docs** can be sent as a pull request directly
  against `main`. Mention the issue number in the commit message.
- **Security issues** — see [SECURITY.md](SECURITY.md). **Do not** file
  a public issue.

## Development setup

Requires Python 3.10+ and git.

```bash
git clone https://github.com/ilysom0611/doc-translator.git
cd doc-translator
python -m venv .venv
. .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install ruff
```

Run the service:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
```

Run the tests:

```bash
python -m pytest tests/ -v          # 121 tests, ~3 minutes
python -m ruff check app/ tests/    # Lint
```

## Project layout

```
app/
├── main.py              FastAPI app, middleware, exception handlers
├── auth.py              PBKDF2 password hash, session store
├── secrets_store.py     Keyring + Fernet encrypted secret store
├── store.py             Provider + settings persistence
├── config.py            File paths, env var reads
├── metrics.py           Prometheus registry (centralized)
├── i18n.py              Catalog loader + lookup
├── i18n/                en.json, zh-CN.json, ...
├── api/                 FastAPI routers (auth, providers, tasks, previews)
├── services/
│   ├── pipeline.py      parse -> batch -> LLM -> write back
│   ├── task_manager.py  In-flight task counter, drain
│   ├── rate_limit.py    slowapi Limiter
│   ├── ssrf_guard.py    URL allow/deny
│   ├── renderer.py      LibreOffice / fallback previews
│   └── llm/             OpenAI-compatible client + metrics
└── formats/             docx / pptx / xlsx / pdf adapters

tests/                   pytest, TestClient fixtures
deploy/                  prometheus.yml
docs/                    architecture.md (this is here, not superpowers/)
```

## Style

- **Python 3.10+** syntax. `from __future__ import annotations` is fine
  in any file. Type hints on public functions.
- **Linting** is `ruff` with rules `E`, `F`, `W`, `B` and
  `line-length = 100`. Two per-file ignores are set in `pyproject.toml`
  (`B008` for FastAPI `Depends/File` defaults, `B023`/`B007` in
  `app/formats/*` for the closure-based flush callbacks).
- **Line length 100.** The `E501` ignore in `pyproject.toml` exists
  because Chinese comments routinely exceed 100 characters; please
  still keep code lines <= 100.
- **No new top-level dependencies** without discussion. Every runtime
  dependency adds a supply-chain risk and a wheel-compatibility problem
  for users on old glibc.

## Testing

- **Unit tests** for every new function in `app/services/`, `app/api/`,
  `app/secrets_store.py`, `app/auth.py`, `app/i18n.py`.
- **Integration tests** for every new endpoint, using the
  `TestClient` fixtures in `tests/conftest.py`.
- **No test that asserts nothing.** A test that just `assert True` or
  only checks for a 200 status is a finding in review.
- **No network in tests.** The LLM client is mocked; provider
  reachability is not tested in CI.
- **Test isolation.** The `conftest.py` autouse fixture resets the
  in-process session store, the `slowapi` storage, and the secrets
  backend between tests. New shared state needs a reset too.

## Commits and PRs

- Commit messages: short imperative subject (<= 72 chars), blank line,
  body explaining **why**. The body is the place to mention the issue
  and the trade-off.
- One logical change per commit. A PR can have multiple commits; the
  PR title and description summarise the whole thing.
- Squash-merge is fine for a PR with 1-2 commits; "Rebase and merge"
  for everything else.
- CI runs `pytest` and `ruff` on every push. The PR will not merge
  with a red CI.

## Release process

1. Bump `version` in `pyproject.toml`.
2. Add a `## [X.Y.Z] - YYYY-MM-DD` section at the top of `CHANGELOG.md`
   in Keep-a-Changelog format.
3. Tag the commit `vX.Y.Z`.
4. The Docker image and the install scripts pick up the new version
   on the next push to `main`.

## Scope of the project

doc-translator is intentionally small. The maintainer is unlikely to
merge:

- New UI frameworks (React, Vue, Svelte). The frontend is a single
  vanilla-JS file for a reason.
- New auth backends (OIDC, SAML, LDAP). The local-only single-password
  model is the product.
- New translation backends beyond OpenAI-compatible chat completion.
  If the model is not chat-completion-shaped, it does not fit.
- Per-user quotas, multi-tenant data isolation, or any other feature
  that implies the service is shared by untrusted users.

If you want any of these, please fork. The MIT license is the point.
