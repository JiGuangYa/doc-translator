"""OpenAI-compatible client factory + metrics instrumentation."""
import time
import re
from urllib.parse import urlsplit, urlunsplit
from openai import OpenAI, DefaultHttpxClient

from ... import store
from ... import metrics
from .. import ssrf_guard


def normalize_chat_base_url(raw_url: str) -> str:
    """Accept a Chat Completions endpoint where the SDK expects its base URL.

    Some clients ask for the full ``/chat/completions`` URL; the OpenAI SDK
    appends that suffix itself. Keep any provider-specific prefix before it.
    """
    url = raw_url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    path = parts.path.rstrip("/")
    endpoint = re.search(r"/chat/completions(?:/|$)", path)
    if endpoint:
        path = path[:endpoint.start()]
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def build_client(provider_id: str, provider_snapshot: dict | None = None) -> tuple[OpenAI, str]:
    """Return (client, model). Raises ValueError if the provider does not exist."""
    provider = provider_snapshot or store.get_provider(provider_id)
    if not provider:
        raise ValueError(f"Translation model provider {provider_id} does not exist")
    # Re-validate at construction time: a base_url that was safe when stored may
    # have been flipped to a malicious host by DNS rebinding.
    base_url = normalize_chat_base_url(provider["base_url"])
    ok, reason = ssrf_guard.check_base_url(base_url)
    if not ok:
        raise ValueError(f"provider base_url rejected by SSRF guard: {reason}")
    api_key = store.get_provider_secret(provider_id)
    return _new_client(base_url, api_key), provider["model"]


def _new_client(base_url: str, api_key: str, timeout: float = 120) -> OpenAI:
    """Create a client without following redirects to a different host."""
    # Disable HTTP redirects at the transport layer: an attacker who controls
    # a provider's response can otherwise return 302 Location: http://169.254.169.254/
    # to pivot the SSRF guard into cloud metadata. The pinned OpenAI SDK exposes
    # the underlying httpx client via `http_client`; setting follow_redirects=False
    # there blocks the redirect chain entirely.
    http_client = DefaultHttpxClient(follow_redirects=False, timeout=timeout)
    return OpenAI(
        base_url=base_url,
        api_key=api_key or "sk-no-key",
        timeout=timeout,
        max_retries=0,  # Backoff retry is controlled by translator.
        http_client=http_client,
    )


def chat_completion_with_metrics(provider_id: str, **kwargs):
    """Wrap OpenAI chat.completions.create with Prometheus metrics.

    Records:
      - llm_calls_total{provider, status}    (ok / error)
      - llm_tokens_total{provider, kind}     (prompt / completion)
      - llm_request_duration_seconds{provider}

    The caller can pass any keyword arguments the OpenAI SDK accepts.
    Returns the response object on success, re-raises on failure.
    """
    usage_callback = kwargs.pop("_usage_callback", None)
    snapshot = kwargs.pop("_provider_snapshot", None)
    cl, model = build_client(provider_id, snapshot) if snapshot else build_client(provider_id)
    kwargs.setdefault("model", model)
    start = time.perf_counter()
    try:
        resp = cl.chat.completions.create(**kwargs)
    except Exception:
        elapsed = time.perf_counter() - start
        metrics.llm_calls_total.labels(provider=provider_id, status="error").inc()
        metrics.llm_request_duration_seconds.labels(provider=provider_id).observe(elapsed)
        if callable(getattr(cl, "close", None)):
            cl.close()
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
        if usage_callback:
            try:
                usage_callback(prompt or 0, completion or 0)
            except Exception:
                # A local accounting failure must not retry a successful paid call.
                import logging
                logging.getLogger(__name__).exception("Task token accounting failed")
    if callable(getattr(cl, "close", None)):
        cl.close()
    return resp


def test_provider(provider_id: str) -> dict:
    """Send a minimal request to test connectivity; returns {ok, latency_ms or error}."""
    try:
        # Use the same metrics-instrumented entry point as real translation.
        chat_completion_with_metrics(
            provider_id,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=5,
        )
        return {"ok": True}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": _friendly_error(e)}


def test_connection(base_url: str, api_key: str, model: str) -> dict:
    """Test unsaved provider details without writing them to the secret store."""
    connection = None
    base_url = normalize_chat_base_url(base_url)
    try:
        ok, reason = ssrf_guard.check_base_url(base_url)
        if not ok:
            return {"ok": False, "error": f"base_url rejected: {reason}",
                    "normalized_base_url": base_url}
        connection = _new_client(base_url, api_key, timeout=30)
        start = time.perf_counter()
        connection.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=5,
        )
        return {"ok": True, "latency_ms": round((time.perf_counter() - start) * 1000),
                "normalized_base_url": base_url}
    except Exception as e:
        return {"ok": False, "error": _friendly_error(e), "normalized_base_url": base_url}
    finally:
        if connection is not None:
            connection.close()


def list_models(base_url: str, api_key: str) -> dict:
    """Discover provider model IDs when its OpenAI-compatible API permits it."""
    connection = None
    try:
        base_url = normalize_chat_base_url(base_url)
        ok, reason = ssrf_guard.check_base_url(base_url)
        if not ok:
            return {"ok": False, "error": f"base_url rejected: {reason}"}
        connection = _new_client(base_url, api_key, timeout=30)
        models = connection.models.list()
        ids = sorted({item.id for item in models.data if isinstance(item.id, str)})[:1000]
        return {"ok": True, "models": ids}
    except Exception as e:
        return {"ok": False, "error": _friendly_error(e)}
    finally:
        if connection is not None:
            connection.close()


def _friendly_error(e: Exception) -> str:
    msg = str(e)
    msg = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._-]+", r"\1[redacted]", msg)
    msg = re.sub(r"sk-[A-Za-z0-9_-]{8,}", "sk-[redacted]", msg)
    if "401" in msg or "Unauthorized" in msg or "Invalid API key" in msg:
        return "Invalid or unauthorized API key (401)"
    if "429" in msg or "rate limit" in msg.lower():
        return "Rate limit exceeded (429)"
    if "model" in msg.lower() and ("not found" in msg.lower() or "does not exist" in msg.lower()):
        return "Model not found — check the model name"
    if "Connect" in type(e).__name__ or "connect" in msg.lower():
        return "Cannot connect to the service — check base_url and network"
    return msg[:300]
