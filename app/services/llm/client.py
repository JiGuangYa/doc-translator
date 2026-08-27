"""OpenAI-compatible client factory + metrics instrumentation."""
import time
import httpx
from openai import OpenAI

from ... import store
from ... import metrics
from .. import ssrf_guard


def build_client(provider_id: str) -> tuple[OpenAI, str]:
    """Return (client, model). Raises ValueError if the provider does not exist."""
    provider = store.get_provider(provider_id)
    if not provider:
        raise ValueError(f"Translation model provider {provider_id} does not exist")
    # Re-validate at construction time: a base_url that was safe when stored may
    # have been flipped to a malicious host by DNS rebinding.
    ok, reason = ssrf_guard.check_base_url(provider["base_url"])
    if not ok:
        raise ValueError(f"provider base_url rejected by SSRF guard: {reason}")
    api_key = store.get_provider_secret(provider_id)
    # Disable HTTP redirects at the transport layer: an attacker who controls
    # a provider's response can otherwise return 302 Location: http://169.254.169.254/
    # to pivot the SSRF guard into cloud metadata. The OpenAI SDK 1.x exposes
    # the underlying httpx client via `http_client`; setting follow_redirects=False
    # there blocks the redirect chain entirely.
    http_client = httpx.Client(follow_redirects=False, timeout=httpx.Timeout(120.0))
    client = OpenAI(
        base_url=provider["base_url"],
        api_key=api_key or "sk-no-key",
        timeout=120,
        max_retries=0,  # Backoff retry is controlled by translator.
        http_client=http_client,
    )
    return client, provider["model"]


def chat_completion_with_metrics(provider_id: str, **kwargs):
    """Wrap OpenAI chat.completions.create with Prometheus metrics.

    Records:
      - llm_calls_total{provider, status}    (ok / error)
      - llm_tokens_total{provider, kind}     (prompt / completion)
      - llm_request_duration_seconds{provider}

    The caller can pass any keyword arguments the OpenAI SDK accepts.
    Returns the response object on success, re-raises on failure.
    """
    cl, model = build_client(provider_id)
    kwargs.setdefault("model", model)
    start = time.perf_counter()
    try:
        resp = cl.chat.completions.create(**kwargs)
    except Exception:
        elapsed = time.perf_counter() - start
        metrics.llm_calls_total.labels(provider=provider_id, status="error").inc()
        metrics.llm_request_duration_seconds.labels(provider=provider_id).observe(elapsed)
        raise
    elapsed = time.perf_counter() - start
    metrics.llm_calls_total.labels(provider=provider_id, status="ok").inc()
    metrics.llm_request_duration_seconds.labels(provider=provider_id).observe(elapsed)
    usage = getattr(resp, "usage", None)
    if usage is not None:
        prompt = getattr(usage, "prompt_tokens", None)
        completion = getattr(usage, "completion_tokens", None)
        if isinstance(prompt, int):
            metrics.llm_tokens_total.labels(provider=provider_id, kind="prompt").inc(prompt)
        if isinstance(completion, int):
            metrics.llm_tokens_total.labels(provider=provider_id, kind="completion").inc(completion)
    return resp


def test_provider(provider_id: str) -> dict:
    """Send a minimal request to test connectivity; returns {ok, latency_ms or error}."""
    try:
        # Use the same metrics-instrumented entry point as real translation.
        chat_completion_with_metrics(
            provider_id,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=5,
            temperature=0,
        )
        return {"ok": True}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": _friendly_error(e)}


def _friendly_error(e: Exception) -> str:
    msg = str(e)
    if "401" in msg or "Unauthorized" in msg or "Invalid API key" in msg:
        return "Invalid or unauthorized API key (401)"
    if "429" in msg or "rate limit" in msg.lower():
        return "Rate limit exceeded (429)"
    if "model" in msg.lower() and ("not found" in msg.lower() or "does not exist" in msg.lower()):
        return "Model not found — check the model name"
    if "Connect" in type(e).__name__ or "connect" in msg.lower():
        return "Cannot connect to the service — check base_url and network"
    return msg[:300]
