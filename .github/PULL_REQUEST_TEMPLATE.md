## What

<!-- One or two sentences. Reference the issue it closes. -->

Closes #

## Why

<!-- The motivation. Link the relevant section of README / SECURITY.md
/ architecture.md. -->

## How

<!-- The user-visible change. If it touches the API, paste the new
endpoint / new field. If it touches the SPA, paste a screenshot. -->

## Test plan

<!-- What did you run locally? The CI matrix (Ubuntu 3.10/3.12 + Windows
3.12) will run, but mention anything OS-specific you verified. -->

- [ ] `python -m pytest tests/ -q` passes locally
- [ ] `python -m ruff check app tests` passes
- [ ] New tests cover the new behavior (CONTRIBUTING.md "Testing")

## Docs

- [ ] CHANGELOG.md has a new entry under an unreleased section
- [ ] README.md / docs/ updated if user-visible behavior changed
- [ ] SECURITY.md updated if the threat model changed

## Scope

- [ ] I have read CONTRIBUTING.md "Scope of the project" and the change
      does not introduce multi-tenant, per-user accounts, new UI framework,
      or non-OpenAI-compatible backends.
