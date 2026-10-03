"""Test that OpenAPI and version endpoints are exposed and unauthenticated."""
from pathlib import Path
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def test_openapi_json_exposed(anon_client):
    """GET /openapi.json returns the OpenAPI 3.x spec (anonymous)."""
    r = anon_client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert "openapi" in spec
    assert "paths" in spec
    # v0.2.0: it documents our auth + provider endpoints
    assert "/api/auth/login" in spec["paths"]
    assert "/api/providers" in spec["paths"]


def test_swagger_ui_exposed(anon_client):
    """GET /docs serves the Swagger UI (anonymous, internal-only)."""
    r = anon_client.get("/docs")
    assert r.status_code == 200
    # Swagger UI HTML
    assert "swagger" in r.text.lower()


def test_redoc_exposed(anon_client):
    """GET /redoc serves the ReDoc UI (anonymous)."""
    r = anon_client.get("/redoc")
    assert r.status_code == 200
    assert "redoc" in r.text.lower()


def test_version_endpoint(anon_client):
    """GET /api/version returns the running version from pyproject.toml."""
    r = anon_client.get("/api/version")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body
    # sanity: must match pyproject.toml's [project] version
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert body["version"] == pyproject["project"]["version"]


def test_openapi_anonymous_but_local_only(anon_client):
    """OpenAPI endpoints are anonymous in main.py's _AUTH_EXEMPT — but
    they should still pass through local_guard's Host check (i.e. 127.0.0.1).
    """
    r = anon_client.get("/openapi.json")
    # The TestClient is base_url=http://127.0.0.1:8765, so local_guard allows it
    assert r.status_code == 200
