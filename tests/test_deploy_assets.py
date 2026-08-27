"""Test deployment assets exist and have basic structural sanity."""
from pathlib import Path


def test_dockerfile_present():
    p = Path("Dockerfile")
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    # multi-stage or single-stage must reference requirements.txt
    assert "requirements.txt" in text
    # must EXPOSE the bind port
    assert "EXPOSE 8765" in text
    # must run via uvicorn
    assert "uvicorn" in text
    # data must be a volume
    assert 'VOLUME ["/app/data"]' in text or "VOLUME" in text


def test_compose_present():
    p = Path("docker-compose.yml")
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    # binds only to host loopback (Scenario A)
    assert "127.0.0.1:8765:8765" in text
    # optional metrics profile
    assert "metrics" in text
    # healthcheck present
    assert "healthcheck" in text
    # graceful stop
    assert "stop_grace_period" in text


def test_prometheus_config_present():
    p = Path("deploy/prometheus.yml")
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert "scrape_configs" in text
    assert "/metrics" in text
    assert "doc-translator" in text


def test_dockerfile_binds_loopback():
    """The Dockerfile CMD must bind to 127.0.0.1, not 0.0.0.0."""
    text = Path("Dockerfile").read_text(encoding="utf-8")
    # Must NOT bind to all interfaces
    assert "--host 0.0.0.0" not in text
    # Must bind to 127.0.0.1
    assert "127.0.0.1" in text
