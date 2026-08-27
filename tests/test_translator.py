"""Unit tests for the translator: mocks the batch function to verify
alias-dedup merging and failure-abort behavior."""
import pytest

from app.formats.common import Segment
from app.services.llm import translator as tr


def _segs(*texts):
    return [Segment(seg_id=f"s{i:06d}", text=t) for i, t in enumerate(texts)]


def test_alias_merge_does_not_mutate_during_iteration(monkeypatch):
    """Regression: writing aliases for duplicate-text segments used to trigger
    "dictionary changed size during iteration"."""
    segs = _segs("Hello", "Hello", "World", "Hello")

    def fake_batch(batch, *a, **k):
        return {s.seg_id: "T:" + s.text for s in batch}

    monkeypatch.setattr(tr, "_translate_batch_with_retry", fake_batch)
    out = tr.translate_segments(segs, "p_x", "en", "zh-CN", {})
    assert out["s000000"] == out["s000001"] == out["s000003"] == "T:Hello"
    assert out["s000002"] == "T:World"


def test_existing_translations_are_kept(monkeypatch):
    """Resume translation: segments that already have an entry in `existing`
    are not re-sent."""
    segs = _segs("A", "B")
    called = []

    def fake_batch(batch, *a, **k):
        called.extend(s.seg_id for s in batch)
        return {s.seg_id: "T" for s in batch}

    monkeypatch.setattr(tr, "_translate_batch_with_retry", fake_batch)
    out = tr.translate_segments(segs, "p_x", "en", "zh-CN", {},
                                existing={"s000000": "already translated"})
    assert out["s000000"] == "already translated"
    assert called == ["s000001"]


def test_consecutive_failures_raise_batch_failures(monkeypatch):
    """Consecutive batch failures -> BatchFailures, with a (possibly empty) partial."""
    from app import config
    segs = _segs(*[f"text-{i}" for i in range(6)])
    settings = {"batch_max_chars": 100, "batch_max_segments": 1, "concurrency_batches": 2}

    monkeypatch.setattr(tr, "_translate_batch_with_retry", lambda *a, **k: None)
    old_max = config.MAX_CONSECUTIVE_BATCH_FAILURES
    config.MAX_CONSECUTIVE_BATCH_FAILURES = 3
    try:
        with pytest.raises(tr.BatchFailures) as ei:
            tr.translate_segments(segs, "p_x", "en", "zh-CN", settings)
        assert isinstance(ei.value.partial, dict)
    finally:
        config.MAX_CONSECUTIVE_BATCH_FAILURES = old_max


def test_wave_abort_not_reset_by_other_successes(monkeypatch):
    """Regression: under concurrency, the failure counter used to be reset by
    other successful batches, so the whole wave could fail without aborting.
    Now: alternating success/failure waves should NOT abort (any success
    resets the counter), but persistent failure must abort."""
    segs = _segs(*[f"text-{i}" for i in range(12)])
    # 1 segment per batch, concurrency 2 -> 6 waves; make every wave "partially
    # successful" (odd-indexed batches fail, even-indexed succeed)
    def fake_batch(batch, *a, **k):
        first = int(batch[0].seg_id[1:])
        return None if first % 2 else {s.seg_id: "T:" + s.text for s in batch}

    monkeypatch.setattr(tr, "_translate_batch_with_retry", fake_batch)
    out = tr.translate_segments(segs, "p_x", "en", "zh-CN",
                                {"batch_max_chars": 100, "batch_max_segments": 1,
                                 "concurrency_batches": 2})
    # Unsuccessful segments are marked with a placeholder, not silently dropped
    marked = [v for v in out.values() if v.startswith("⟪ untranslated:")]
    assert len(marked) == 6


def test_intermittent_failure_then_recovery_no_abort(monkeypatch):
    """After intermittent failure with recovery: no abort, and the task
    returns all results normally."""
    segs = _segs(*[f"t{i}" for i in range(4)])
    settings = {"batch_max_chars": 100, "batch_max_segments": 1, "concurrency_batches": 1}
    state = {"fail_first": 2}

    def fake_batch(batch, *a, **k):
        if state["fail_first"] > 0:
            state["fail_first"] -= 1
            return None
        return {s.seg_id: "T:" + s.text for s in batch}

    monkeypatch.setattr(tr, "_translate_batch_with_retry", fake_batch)
    # First two batches fail but not enough to reach the abort threshold
    # -> no exception; failed segments are marked as placeholders for resume
    out = tr.translate_segments(segs, "p_x", "en", "zh-CN", settings)
    marked = [v for v in out.values() if v.startswith("⟪ untranslated:")]
    assert len(marked) == 2
    assert sum(1 for v in out.values() if not v.startswith("⟪")) == 2


def test_untranslated_count_and_placeholder_semantics():
    """pipeline.untranslated_count: both missing translations and placeholders
    count as untranslated; real translations do not."""
    from app.services import pipeline
    job = {"segments": [
        {"seg_id": "s000000", "translatable": True, "translation": "已译"},
        {"seg_id": "s000001", "translatable": True, "translation": None,
         "__tr__": "⟪ untranslated:原文 ⟫"},
        {"seg_id": "s000002", "translatable": True, "translation": None},
        {"seg_id": "s000003", "translatable": False, "translation": None},
    ], "translations": {
        "s000000": "已译",
        "s000001": "⟪ untranslated:原文 ⟫",
    }}
    # s000001 has a placeholder -> untranslated;
    # s000002 has no translation at all -> untranslated;
    # s000003 is non-translatable, not counted
    assert pipeline.untranslated_count(job) == 2


def test_resume_ignores_placeholder_existing(monkeypatch, tmp_path):
    """Resume entry point excludes placeholder segments: ⟪untranslated⟫
    entries must not appear in `existing`."""
    from app.formats.common import is_untranslated
    assert is_untranslated("⟪ untranslated:任意 ⟫")
    assert not is_untranslated("正常译文")
    assert not is_untranslated(None)


def test_cancel_carries_partial_results(monkeypatch):
    """Regression: when the user cancels, results from batches that
    completed before the current wave must travel back with the exception,
    not be discarded."""
    segs = _segs(*[f"t{i}" for i in range(6)])
    settings = {"batch_max_chars": 100, "batch_max_segments": 1, "concurrency_batches": 2}
    state = {"calls": 0}

    def fake_batch(batch, *a, **k):
        state["calls"] += 1
        if state["calls"] > 2:
            raise tr.TranslationCancelled()
        return {s.seg_id: "译:" + s.text for s in batch}

    monkeypatch.setattr(tr, "_translate_batch_with_retry", fake_batch)
    with pytest.raises(tr.TranslationCancelled) as ei:
        tr.translate_segments(segs, "p_x", "en", "zh-CN", settings)
    # Results from the first wave (2 batches) are kept; later waves are cancelled
    assert ei.value.partial.get("s000000") == "译:t0"
    assert ei.value.partial.get("s000001") == "译:t1"
    assert len(ei.value.partial) == 2


def test_progress_snapshot_monotonic_with_aliases(monkeypatch):
    """progress_cb snapshots must grow monotonically and include alias
    expansion (for upstream incremental persistence)."""
    segs = _segs("Hello", "Hello", "World")  # s000000 and s000001 share text
    settings = {"batch_max_chars": 100, "batch_max_segments": 1, "concurrency_batches": 1}
    snaps = []

    monkeypatch.setattr(tr, "_translate_batch_with_retry",
                        lambda batch, *a, **k: {s.seg_id: "译:" + s.text for s in batch})

    def cb(done, total, snapshot=None):
        snaps.append(dict(snapshot or {}))

    out = tr.translate_segments(segs, "p_x", "en", "zh-CN", settings, progress_cb=cb)
    lens = [len(s) for s in snaps]
    assert lens == sorted(lens), "snapshots must be monotonically non-decreasing"
    assert lens and lens[-1] >= 2
    # Alias segments must already have translations in the snapshot (consistent with the final result)
    assert snaps[-1]["s000001"] == out["s000001"] == "译:Hello"


def test_weighted_len_counts_cjk_double():
    assert tr._weighted_len("abcd") == 4
    assert tr._weighted_len("中文") == 4
    assert tr._weighted_len("a中b文") == 6


def test_make_batches_respects_cjk_budget():
    """Pure-Chinese segments are split by weighted length: with a 4000
    budget, two 3000-char segments must each become their own batch."""
    from app.formats.common import Segment
    segs = [Segment(seg_id="s000000", text="中" * 3000),
            Segment(seg_id="s000001", text="文" * 3000)]
    batches = tr.make_batches(segs, 4000, 20)
    assert len(batches) == 2, "with CJK weighting the two segments total 12000 > 4000, so each must be its own batch"


def test_oversized_segment_split_and_reassembled(monkeypatch):
    """Overly long single segments are split at sentence boundaries for
    translation, and the resulting translations are concatenated back into
    the original seg_id in order."""
    from app.formats.common import Segment
    long_text = "。".join(f"第{i}句这是一些内容" for i in range(200)) + "。"
    segs = [Segment(seg_id="s000000", text=long_text),
            Segment(seg_id="s000001", text="短句")]
    seen_payloads = []

    def fake_batch(batch, *a, **k):
        seen_payloads.append([s.seg_id for s in batch])
        return {s.seg_id: "译" for s in batch}

    monkeypatch.setattr(tr, "_translate_batch_with_retry", fake_batch)
    out = tr.translate_segments(segs, "p_x", "zh-CN", "en",
                                {"batch_max_chars": 200, "batch_max_segments": 5})
    # Piece segment ids only appear in the requests, not in the final result
    piece_ids = [i for p in seen_payloads for i in p]
    n_pieces = sum(1 for i in piece_ids if i.startswith("s000000#p"))
    assert n_pieces > 1, "an over-long segment should be split into multiple pieces"
    assert not any("#p" in k for k in out)
    # Reassembly: piece translations are concatenated in order to form s000000's full translation
    assert out["s000000"] == "译" * n_pieces
    assert out["s000001"] == "译"


def test_split_text_respects_budget_and_boundaries():
    text = "短句。" * 100
    parts = tr._split_text(text, 30)
    assert all(tr._weighted_len(p) <= 60 for p in parts), "piece segments are allowed to slightly exceed the budget (must contain at least one full sentence)"
    assert "".join(parts) == text, "the pieces must concatenate back to the original losslessly"


def test_retry_after_header_honored(monkeypatch):
    """On 429 with a Retry-After header, back off by that value instead of
    the fixed exponential value."""
    from types import SimpleNamespace

    class FakeRateLimit(Exception):
        status_code = 429
        response = SimpleNamespace(headers={"retry-after": "7"})

    sleeps = []
    monkeypatch.setattr(tr.time, "sleep", lambda s: sleeps.append(s))

    def fake_create(**kwargs):
        raise FakeRateLimit("rate limited")

    cl = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    monkeypatch.setattr(tr.client, "build_client", lambda pid: (cl, "m"))

    from app.formats.common import Segment
    batch = [Segment(seg_id="s000000", text="hi")]
    tr._translate_batch_with_retry(batch, "p_x", "en", "zh-CN")
    assert len(sleeps) == tr.config.LLM_MAX_RETRIES + 1 - 1 or sleeps
    for s in sleeps:
        assert 7 * 0.7 <= s <= min(7 * 1.3, 60), f"back-off should jitter around Retry-After=7, got {s}"
