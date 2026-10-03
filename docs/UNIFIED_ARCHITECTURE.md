# Unified native preview 0.3.0

Base: JiGuangYa/doc-translator `af85ce5`; native translation/reading core: local `8c97b50`.

- `app/`: shared FastAPI translation service, safe format writers, OCR checkpoints, persistent queue, token accounting and glossary/memory.
- `macos/Sources/`: SwiftUI application. Paragraphs load in bounded pages of 100. Visible page images use an 80 MiB/8-image cache.
- `macos/engine_launcher.py`: private stdin bootstrap, per-launch nonce, exclusive library lease, parent EOF shutdown and supervised converter role.
- `macos/scripts/`: package build, isolated acceptance and recoverable preview-only installation.

The preview has a separate bundle ID, library and Keychain namespace. It never replaces the stable application. No real documents, credentials, migration reports or application archives belong in Git.

## Revision transaction

`PATCH /api/tasks/{id}/segments` validates every revision before changing any output. It checks the task revision and external file digest, writes/validates one candidate file, then commits using the recoverable output transaction. Single revisions call the same transaction. `revision` is canonical; responses retain `content_version` for legacy clients.

Drafts are native-owned. The version-2 `revision-drafts.json` snapshot contains each task's segment text and base revision together and is replaced atomically. The backend reads it only for paginated search/filtering. Saving clears only submitted text which is still identical; newer edits retain the new committed base revision. Unknown legacy draft versions require explicit review.

## Resume configuration

The first start stores provider metadata and prices, translation settings, languages and format options. Resume reads the stored endpoint/model/batching while retrieving the current credential by provider ID. Imported tasks with no historical configuration require confirmation. Unknown historical token/cost values stay unknown; new usage remains separately available.

## Migration

A durable manifest assigns new task/model IDs before copying. Each source job, original and current output is fingerprinted, then the staged copy is verified before publication. Existing translated bytes are copied without conversion. The manifest makes interrupted imports retryable, identical imports deduplicate, and changed source content becomes another copy. Source libraries are read only and must be stopped.

Word compatibility modes remain explicit: native legacy v1, structured v2, MacBook legacy v3. PPT note handling is recovered by comparing extracted IDs/text with stored segments. Failed mapping preserves files but blocks translation/revision; “另译一份” uses the current parser. Legacy MacBook Word documents with links, mixed styles or compound text runs block rebuilding; their copied output remains exportable, and a separate translation uses the structured writer. Preview caches are regenerated.

## Verification

Tests use isolated libraries and fake credentials. Backend pytest covers both editions; native XCTest covers drafts/providers/bookmarks/export and runs the inherited paging/import/process/Vision workflow. Packaged acceptance uses a local HTTP model and real GenOffice where installed. Automated tests never read saved API keys.
