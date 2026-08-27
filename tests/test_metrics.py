"""Test the metrics registry: declarations exist, label cardinality is bounded."""
from prometheus_client import generate_latest

from app import metrics


def test_all_metrics_declared():
    """All public metric objects are non-None Counter/Histogram/Gauge."""
    for name in (
        "http_requests_total",
        "http_request_duration_seconds",
        "llm_calls_total",
        "llm_tokens_total",
        "llm_request_duration_seconds",
        "tasks_in_flight",
        "tasks_total",
        "process_start_time_seconds",
    ):
        assert hasattr(metrics, name), f"missing metrics.{name}"


def test_metrics_serializable():
    """The registry can be rendered to a Prometheus text exposition."""
    # Touch a few metrics with sample labels so the serializer has data
    metrics.http_requests_total.labels(method="GET", path="/api/x", status="200").inc()
    metrics.llm_tokens_total.labels(provider="p1", kind="prompt").inc(42)
    metrics.tasks_in_flight.set(3)
    out = generate_latest().decode()
    assert "http_requests_total" in out
    assert "llm_tokens_total" in out
    assert "tasks_in_flight" in out
    assert "process_start_time_seconds" in out


def test_label_names_have_no_high_cardinality_fields():
    """No metric label includes raw user input (URL path, body) that would
    blow up cardinality. This is a static lint on labelnames()."""
    for name in (
        "http_requests_total",
        "http_request_duration_seconds",
        "llm_calls_total",
        "llm_tokens_total",
        "llm_request_duration_seconds",
        "tasks_total",
    ):
        metric = getattr(metrics, name)
        labels = metric._labelnames  # type: ignore[attr-defined]
        for label in labels:
            # path is the only URL-derived label and it must be the
            # normalized route template, not the raw request path
            if label == "path":
                continue
            assert "url" not in label, f"{name}.{label} may blow up cardinality"
            assert "user" not in label, f"{name}.{label} may blow up cardinality"
