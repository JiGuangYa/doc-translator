"""Centralized Prometheus metrics registry.

All Counter/Histogram/Gauge instances live here so call-sites can
`from app import metrics` and emit without re-declaring.

Naming follows the Prometheus convention:
  _total suffix for counters
  _seconds suffix for time-based histograms
  No suffix for gauges.

The registry is the default global registry — `generate_latest()` from
the ASGI /metrics endpoint serializes everything in one shot.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ---------- HTTP ----------

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests served.",
    labelnames=("method", "path", "status"),
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds.",
    labelnames=("method", "path"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)

# ---------- LLM ----------

llm_calls_total = Counter(
    "llm_calls_total",
    "Total LLM API calls (one per chat.completions.create).",
    labelnames=("provider", "status"),  # status: ok / error
)

llm_tokens_total = Counter(
    "llm_tokens_total",
    "LLM tokens consumed, by kind.",
    labelnames=("provider", "kind"),  # kind: prompt / completion
)

llm_request_duration_seconds = Histogram(
    "llm_request_duration_seconds",
    "LLM API call latency in seconds.",
    labelnames=("provider",),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)

# ---------- Tasks ----------

tasks_in_flight = Gauge(
    "tasks_in_flight",
    "Number of translation tasks currently in non-terminal state.",
)
tasks_total = Counter(
    "tasks_total",
    "Translation tasks created, by terminal status.",
    labelnames=("status",),  # done / failed / cancelled
)

# ---------- App lifecycle ----------

process_start_time_seconds = Gauge(
    "process_start_time_seconds",
    "Unix timestamp of process start (set once at module import).",
)
process_start_time_seconds.set(__import__("time").time())
