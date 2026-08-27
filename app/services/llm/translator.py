"""Batch translation engine: batching, dedup, JSON fault tolerance, missing-filling, backoff retry, resume."""
import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import client
from .prompts import SYSTEM_PROMPT, build_user_prompt
from ... import config
from ...formats import common

logger = logging.getLogger(__name__)


# ---------- Batch length estimation ----------

def _weighted_len(text: str) -> int:
    """Token-density-weighted length: CJK characters ~1-2 tokens each, Latin ~1 token per 4 chars.

    Estimating by Python codepoints alone makes the actual tokens for Chinese batches exceed
    the budget by more than 2x, leading to truncated responses, JSON parse failures, and
    expensive batch retries.
    """
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


_SENTENCE_BOUNDARIES = set("。!?;\n.!?!;")  # Mixed punctuation coverage for CJK + Latin.


def _split_text(text: str, max_chars: int) -> list[str]:
    """Split overly long text into chunks with weighted length <= max_chars at sentence boundaries."""
    parts: list[str] = []
    buf: list[str] = []
    buf_w = 0
    boundary = -1
    for ch in text:
        buf.append(ch)
        buf_w += 2 if ord(ch) > 0x2E80 else 1
        if ch in _SENTENCE_BOUNDARIES:
            boundary = len(buf) - 1
        if buf_w >= max_chars:
            cut = boundary + 1 if boundary >= 0 else len(buf)  # No boundary: hard cut, must make progress.
            parts.append("".join(buf[:cut]))
            rest = "".join(buf[cut:])
            buf = list(rest)
            buf_w = _weighted_len(rest)
            boundary = -1
    if buf:
        parts.append("".join(buf))
    return [p for p in parts if p]


def _split_oversized(segments: list, max_chars: int) -> tuple[list, dict[str, list[str]]]:
    """When a single segment exceeds the batch char budget, split it at the sentence level into
    piece-segments (seg_id shaped like s000123#p0).

    Returns the split segment list and a {original seg_id: [piece seg_id]} map. Translations
    are reassembled in order before being written back.
    """
    out: list = []
    piece_map: dict[str, list[str]] = {}
    for s in segments:
        if _weighted_len(s.text) <= max_chars:
            out.append(s)
            continue
        texts = _split_text(s.text, max_chars)
        piece_map[s.seg_id] = []
        for i, t in enumerate(texts):
            pid = f"{s.seg_id}#p{i}"
            piece_map[s.seg_id].append(pid)
            out.append(type(s)(seg_id=pid, text=t, context=s.context, meta=dict(s.meta)))
    return out, piece_map


def _reassemble_pieces(out: dict[str, str], piece_map: dict[str, list[str]]) -> dict[str, str]:
    """Reassemble piece-translations back into the original segment. If any piece is missing,
    drop the whole segment (leaves it for a future resume to retry the full segment)."""
    if not piece_map:
        return out
    result = {k: v for k, v in out.items() if "#" not in k}
    for orig_id, pids in piece_map.items():
        parts = [out.get(pid) for pid in pids]
        if all(parts):
            result[orig_id] = "".join(parts)
    return result


class TranslationCancelled(Exception):
    """User-cancelled translation. `partial` is {seg_id: translation} completed before cancel,
    so the caller can preserve finished work."""

    def __init__(self, message: str = "Translation cancelled", partial: dict[str, str] | None = None):
        super().__init__(message)
        self.partial = partial or {}


class BatchFailures(RuntimeError):
    """Aborted after too many consecutive failed waves. `partial` is the {seg_id: translation}
    finished so far, so progress can be checkpointed for resume."""

    def __init__(self, message: str, partial: dict[str, str]):
        super().__init__(message)
        self.partial = partial


def make_batches(segments: list, max_chars: int, max_count: int) -> list[list]:
    """Greedy in-order batching: finalize a batch whenever either cap is hit. Length uses
    the token-weighted measure (see _weighted_len)."""
    batches, current, chars = [], [], 0
    for seg in segments:
        seg_len = _weighted_len(seg.text)
        if current and (chars + seg_len > max_chars or len(current) >= max_count):
            batches.append(current)
            current, chars = [], 0
        current.append(seg)
        chars += seg_len
    if current:
        batches.append(current)
    return batches


def translate_segments(segments: list, provider_id: str, source_lang: str, target_lang: str,
                       settings: dict, existing: dict[str, str] | None = None,
                       progress_cb=None, cancel_check=None) -> dict[str, str]:
    """Translate a batch of segments, returning {seg_id: translation} (including any pre-existing ones).

    - Same-source dedup: identical source text is sent for translation only once.
    - Resume: seg_ids already in `existing` are skipped.
    - progress_cb(done, total, snapshot): counts unique segments; snapshot is the cumulative
      set of new translations (with alias expansion) so the caller can periodically persist.
      On crash, the persisted portion can be used to resume.
    - cancel_check() returning True raises TranslationCancelled between batches (carries
      completed work as `partial`).
    """
    existing = dict(existing or {})
    todo = [s for s in segments if s.seg_id not in existing]
    if not todo:
        return existing

    # Dedup: pick representative segments for translation, copy results to aliases.
    from ...formats.common import dedupe_segments
    unique, alias = dedupe_segments(todo)

    max_chars = settings.get("batch_max_chars", 4000)
    # If any single segment exceeds the budget, split it at the sentence level so an oversized
    # paragraph doesn't end up alone in a batch that still overflows the context window.
    batchable, piece_map = _split_oversized(unique, max_chars)
    batches = make_batches(batchable, max_chars,
                           settings.get("batch_max_segments", 20))
    concurrency = max(1, int(settings.get("concurrency_batches", 3)))

    lock = threading.Lock()
    done_count = len(existing)
    total = len(existing) + len(todo)
    batch_outputs: list[dict] = []  # Each thread appends {seg_id: translation}; merged on read.

    def _merge_outputs_locked() -> dict[str, str]:
        merged: dict[str, str] = {}
        for rep in batch_outputs:
            merged.update(rep)
        return merged

    def _expand_aliases(out: dict[str, str]) -> dict[str, str]:
        """Copy translations to dedup aliases (collect first then merge, to avoid mutating during iteration)."""
        alias_map: dict[str, str] = {}
        for rep_id, trans in out.items():
            for other in alias.get(rep_id, []):
                alias_map[other] = trans
        out.update(alias_map)
        return out

    def partial_results() -> dict[str, str]:
        with lock:
            merged = _merge_outputs_locked()
        return _expand_aliases(merged)

    def report(delta: int):
        nonlocal done_count
        with lock:
            done_count += delta
            snap = dict(_merge_outputs_locked()) if progress_cb else None
            d, t = done_count, total
        # Run the callback outside the lock — callbacks may do disk IO (incremental persist)
        # and we don't want them to block other threads.
        if progress_cb:
            progress_cb(d, t, _expand_aliases(snap))

    def run_batch(batch) -> bool:
        """Returns success status; failures do not raise (the wave-level tally decides whether to abort)."""
        if cancel_check and cancel_check():
            raise TranslationCancelled()
        translated = _translate_batch_with_retry(batch, provider_id, source_lang, target_lang)
        if not translated:
            report(0)
            return False
        with lock:
            batch_outputs.append(translated)
        report(len(batch))
        return True

    # Wave-level concurrency: each wave runs up to `concurrency` batches in parallel; the
    # whole wave's outcomes are tallied together to decide abort.
    # A "consecutive-failure count" per thread is constantly reset by other threads' successes
    # and becomes meaningless under concurrency; wave-level tally is the only way to reliably
    # detect a sustained server-side outage and abort in time.
    failed_waves = 0
    for i in range(0, len(batches), concurrency):
        wave = batches[i:i + concurrency]
        outcomes: list[bool] = []
        cancelled = False
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            futures = [pool.submit(run_batch, b) for b in wave]
            for fut in as_completed(futures):
                try:
                    outcomes.append(fut.result())
                except TranslationCancelled:
                    cancelled = True
            # Exiting the `with` block waits for the wave to fully finish: any in-flight batch
            # in the wave completes normally and its output is recorded into batch_outputs
            # via run_batch, so cancel does not lose the wave's completed work.
        if cancelled:
            raise TranslationCancelled(partial=partial_results())
        if outcomes and not any(outcomes):
            failed_waves += 1
            logger.warning("Wave %d failed entirely (%d batches all unsuccessful)", failed_waves, len(wave))
            if failed_waves >= config.MAX_CONSECUTIVE_BATCH_FAILURES:
                raise BatchFailures(
                    f"{config.MAX_CONSECUTIVE_BATCH_FAILURES} consecutive waves of failed batches — aborted "
                    f"(completed portion preserved, retry to resume)",
                    partial_results())
        else:
            failed_waves = 0

    out = _reassemble_pieces(_merge_outputs_locked(), piece_map)
    out = _expand_aliases(out)
    # Missing segments keep the source and are marked as untranslated.
    merged = dict(existing)
    missing = 0
    for seg in todo:
        if seg.seg_id in out:
            merged[seg.seg_id] = out[seg.seg_id]
        else:
            merged[seg.seg_id] = common.mark_untranslated(seg.text)
            missing += 1
    if missing:
        logger.warning("Of %d segments in this round, %d still lack a translation (model returned empty and rescue failed); marked for resume",
                       len(todo), missing)
    return merged


def _retry_after_seconds(e: Exception) -> float | None:
    """Pull Retry-After (seconds) from a 429 response; returns None when missing or invalid."""
    hdrs = getattr(getattr(e, "response", None), "headers", None)
    try:
        v = float(hdrs.get("retry-after"))
    except (TypeError, ValueError, AttributeError):
        return None
    return v if v >= 0 else None


def _translate_batch_with_retry(batch, provider_id, source_lang, target_lang) -> dict[str, str] | None:
    """Translate a single batch with three layers of fault tolerance. Returns None when all
    layers fail (caller counts failures).

    On rate limit, prefer Retry-After; otherwise exponential backoff. A uniform ±30% jitter
    prevents thundering-herd retries across waves when the server comes back online.
    """
    payload = {s.seg_id: s.text for s in batch}
    last_error = None
    # Network / 429 backoff retry.
    for attempt in range(config.LLM_MAX_RETRIES + 1):
        try:
            resp = client.chat_completion_with_metrics(
                provider_id,
                temperature=0.1,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",
                     "content": build_user_prompt(json.dumps(payload, ensure_ascii=False),
                                                  source_lang, target_lang)},
                ],
            )
            parsed = _parse_response(resp.choices[0].message.content or "", set(payload))
            if parsed is not None:
                return _fill_missing(parsed, batch, provider_id, source_lang, target_lang)
            last_error = "Response is not valid JSON"
            # Occasional truncation can recover on retry — same backoff + jitter applies.
            time.sleep(min(config.LLM_BACKOFF_BASE * (2 ** attempt) * random.uniform(0.7, 1.3), 60))
        except TranslationCancelled:
            raise
        except Exception as e:
            last_error = str(e)[:200]
            status = getattr(getattr(e, "status_code", None), "__int__", lambda: None)()
            if status is None or status not in (429, 500, 502, 503, 504):
                break  # Non-transient errors: don't backoff-retry.
            ra = _retry_after_seconds(e)
            delay = ra if ra is not None else config.LLM_BACKOFF_BASE * (2 ** attempt)
            time.sleep(min(delay * random.uniform(0.7, 1.3), 60))
    logger.warning("Batch failed (%d segments): %s", len(batch), last_error)
    return None


def _parse_response(text: str, expected_ids: set[str]) -> dict | None:
    from ...utils import extract_json_object
    obj = extract_json_object(text)
    if not obj:
        return None
    return {k: v for k, v in obj.items() if k in expected_ids and isinstance(v, str) and v.strip()}


def _fill_missing(parsed: dict, batch, provider_id, source_lang, target_lang) -> dict[str, str]:
    """At most two rescue passes for missing ids: full-batch with missing list, then small-group retry."""
    result = dict(parsed)
    missing = [s for s in batch if s.seg_id not in result]
    if not missing:
        return result

    # Round 1: re-request the full batch including the explicit missing list.
    try:
        payload = {s.seg_id: s.text for s in batch}
        resp = client.chat_completion_with_metrics(
            provider_id,
            temperature=0.1,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(
                    json.dumps(payload, ensure_ascii=False), source_lang, target_lang,
                    missing_ids=[s.seg_id for s in missing])},
            ])
        more = _parse_response(resp.choices[0].message.content or "", set(payload))
        if more:
            result.update(more)
    except Exception:
        pass

    # Round 2: retry still-missing items in small groups (5 segments per group).
    still = [s for s in batch if s.seg_id not in result]
    if still:
        try:
            for i in range(0, len(still), 5):
                group = still[i:i + 5]
                payload = {s.seg_id: s.text for s in group}
                try:
                    resp = client.chat_completion_with_metrics(
                        provider_id,
                        temperature=0.1,
                        messages=[
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": build_user_prompt(
                                json.dumps(payload, ensure_ascii=False), source_lang, target_lang)},
                        ])
                    more = _parse_response(resp.choices[0].message.content or "", set(payload))
                    if more:
                        result.update(more)
                except Exception:
                    continue
        except Exception:
            pass
    return result


def mock_translate_text(text: str) -> str:
    """Echo mock: zero-token end-to-end validation."""
    return "【T】" + text
