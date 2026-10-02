"""Test that chat_completion_with_metrics records LLM usage."""
from types import SimpleNamespace
from app import metrics
from app.services.llm import client


def _make_fake_response(prompt_tokens=10, completion_tokens=20):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def test_records_tokens_on_success(monkeypatch):
    """Successful call must inc llm_tokens_total for both prompt and completion."""
    cl = SimpleNamespace(
        close=lambda: None,
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: _make_fake_response(7, 11)))
    )
    monkeypatch.setattr(client, "build_client", lambda pid: (cl, "m"))
    before_p = metrics.llm_tokens_total.labels(provider="p_test", kind="prompt")._value.get()
    before_c = metrics.llm_tokens_total.labels(provider="p_test", kind="completion")._value.get()
    before_calls = metrics.llm_calls_total.labels(provider="p_test", status="ok")._value.get()
    resp = client.chat_completion_with_metrics(
        "p_test", messages=[{"role": "user", "content": "hi"}], max_tokens=1
    )
    assert resp.usage.prompt_tokens == 7
    after_p = metrics.llm_tokens_total.labels(provider="p_test", kind="prompt")._value.get()
    after_c = metrics.llm_tokens_total.labels(provider="p_test", kind="completion")._value.get()
    after_calls = metrics.llm_calls_total.labels(provider="p_test", status="ok")._value.get()
    assert after_p - before_p == 7
    assert after_c - before_c == 11
    assert after_calls - before_calls == 1


def test_records_call_error(monkeypatch):
    """Failed call must inc llm_calls_total{status=error} and re-raise."""

    def boom(**kw):
        raise RuntimeError("network down")

    cl = SimpleNamespace(
        close=lambda: None,
        chat=SimpleNamespace(completions=SimpleNamespace(create=boom))
    )
    monkeypatch.setattr(client, "build_client", lambda pid: (cl, "m"))
    before = metrics.llm_calls_total.labels(provider="p_err", status="error")._value.get()
    try:
        client.chat_completion_with_metrics("p_err", messages=[{"role": "user", "content": "x"}])
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError to propagate")
    after = metrics.llm_calls_total.labels(provider="p_err", status="error")._value.get()
    assert after - before == 1


def test_no_usage_attribute(monkeypatch):
    """If the SDK returns no usage attribute, no token metrics are touched."""
    resp = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="x"))])
    cl = SimpleNamespace(
        close=lambda: None,
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: resp))
    )
    monkeypatch.setattr(client, "build_client", lambda pid: (cl, "m"))
    # Just verify it doesn't raise
    out = client.chat_completion_with_metrics(
        "p_no_usage", messages=[{"role": "user", "content": "y"}]
    )
    assert out.choices[0].message.content == "x"


def test_frozen_provider_used_and_client_closed_on_error(monkeypatch):
    import pytest
    frozen = {'name': 'Original', 'base_url': 'http://127.0.0.1:9999/v1', 'model': 'original'}
    calls = []
    closed = []
    def create(**kwargs):
        calls.append(kwargs)
        raise RuntimeError('provider offline')
    def build(pid, snapshot):
        assert pid == 'p'
        assert snapshot == frozen
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                               close=lambda: closed.append(True)), snapshot['model']
    monkeypatch.setattr(client, 'build_client', build)
    with pytest.raises(RuntimeError, match='provider offline'):
        client.chat_completion_with_metrics('p', provider_snapshot=frozen, messages=[])
    assert calls[0]['model'] == 'original'
    assert 'provider_snapshot' not in calls[0]
    assert closed == [True]
