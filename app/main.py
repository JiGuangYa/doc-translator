"""FastAPI application entry point."""
from importlib.metadata import version as _pkg_version, PackageNotFoundError

try:
    _version = _pkg_version("doc-translator")
except PackageNotFoundError:
    # Not installed as a package (dev mode): fall back to reading pyproject.toml directly.
    import tomllib
    from pathlib import Path
    _version = tomllib.loads(
        Path(__file__).resolve().parent.parent.joinpath("pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
import logging
import time
import uuid
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from . import auth, config, i18n, store
from . import secrets_store
from .api import auth as auth_api
from .api import previews, providers, tasks
from .services import renderer, task_manager
from .services.rate_limit import limiter
from . import metrics


class NoCacheStaticFiles(StaticFiles):
    """During dev iteration, disable JS/CSS caching to avoid browser-served stale scripts."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp

# Windows console GBK compatibility: force UTF-8 for file logs.
_fh = RotatingFileHandler(config.DATA_DIR / "app.log", maxBytes=2_000_000,
                          backupCount=1, encoding="utf-8")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s",
                    handlers=[_fh, logging.StreamHandler()])


# ---------- middlewares ----------

# /api/* and /static/* have exploding metric cardinality; only emit metrics for route templates.
_SKIP_METRICS_PATHS = ("/metrics", "/static")


def _route_template(request: Request) -> str:
    """Return the FastAPI route template (e.g., /api/tasks/{task_id}); fall back to a
    single bucketed value on miss so scanners hitting random URLs cannot blow up
    Prometheus label cardinality."""
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    return "/_unmatched"


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response: Response = await call_next(request)
        # Skip these for static assets (browser/CDN correctness may rely on defaults);
        # only add them for document/JSON responses.
        if request.url.path.startswith("/static"):
            return response
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        # Content-Security-Policy: lock down what the SPA can load and run.
        # The only inline script we keep is the small init block in
        # static/index.html; future refactors can move it to an external
        # file and drop 'unsafe-inline' for scripts.
        # connect-src 'self' is required for /api/* and /api/i18n/*.json fetches.
        # img-src includes data: for the favicon SVG and any data-URL preview assets.
        # object-src 'none' kills <object>/<embed>/<applet>; base-uri 'none'
        # prevents <base> tag hijacking; frame-ancestors 'none' supersedes
        # X-Frame-Options for modern browsers.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'; "
            "form-action 'self'",
        )
        # HSTS only takes effect under HTTPS; loopback HTTP is unaffected.
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path.startswith(_SKIP_METRICS_PATHS):
            return await call_next(request)
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        path = _route_template(request)
        status = str(response.status_code)
        metrics.http_requests_total.labels(
            method=request.method, path=path, status=status
        ).inc()
        metrics.http_request_duration_seconds.labels(
            method=request.method, path=path
        ).observe(elapsed)
        return response


_SHUTDOWN_TIMEOUT_S = 30  # Max seconds to wait for in-flight tasks to clear after SIGTERM.
_shutting_down = False


def is_shutting_down() -> bool:
    """Return True while refusing new translation tasks (graceful shutdown period)."""
    return _shutting_down


@asynccontextmanager
async def lifespan(app: FastAPI):
    # v0.2.0: one-time migration of legacy XOR ciphertext in providers.json to secrets_store.
    if secrets_store.migrate_legacy_xor():
        logging.info("Migrated legacy provider API keys from XOR ciphertext to encrypted key store")
    task_manager.recover_on_startup()
    renderer.recover_stale()
    removed = store.gc_old_tasks(config.TASK_TTL_DAYS)
    if removed:
        logging.info("Cleaned up %d terminal tasks older than %d days: %s",
                     len(removed), config.TASK_TTL_DAYS, ", ".join(removed[:10]))
    yield
    # Graceful shutdown: when uicorn receives SIGTERM, FastAPI shutdown is triggered;
    # this block runs after the yield above.
    global _shutting_down
    _shutting_down = True
    in_flight = task_manager.count_in_flight()
    if in_flight > 0:
        logging.info("Shutting down: waiting for %d in-flight tasks to finish (timeout %ds) ...", in_flight, _SHUTDOWN_TIMEOUT_S)
        if not task_manager.wait_for_drain(_SHUTDOWN_TIMEOUT_S):
            remaining = task_manager.count_in_flight()
            logging.warning("Shutdown timeout: %d tasks still incomplete; recover_on_startup will mark them failed on next boot", remaining)

# Order: LIFO — last-registered middleware runs first.
# Therefore registration order: Metrics (outermost) → SecurityHeaders → RequestID → local_guard.
app = FastAPI(title="doc-translator", docs_url="/docs", redoc_url="/redoc",
              openapi_url="/openapi.json",
              version="0.2.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_middleware(MetricsMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)


@app.exception_handler(RateLimitExceeded)
async def _rate_limited(request: Request, exc: RateLimitExceeded):
    """Convert slowapi's 429 into a uniform JSON response with a Retry-After header."""
    return JSONResponse(
        {"detail": "Too many requests, please try again later"},
        status_code=429,
        headers={"Retry-After": "60"},
    )


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", "-") or "-"


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """Any uncaught exception: uniform 500 + request_id; never leak stack or message to the client."""
    rid = _rid(request)
    logging.exception("unhandled exception (request_id=%s): %s", rid, exc)
    return JSONResponse(
        {"detail": "Internal server error", "request_id": rid},
        status_code=500,
    )


from fastapi.exceptions import RequestValidationError


@app.exception_handler(RequestValidationError)
async def _validation(request: Request, exc: RequestValidationError):
    """Pydantic / FastAPI validation failure: uniform 422 + raw error array + request_id."""
    return JSONResponse(
        {"detail": exc.errors(), "request_id": _rid(request)},
        status_code=422,
    )


# Local protection: binding to 127.0.0.1 only blocks the LAN, but not these two browser attacks —
#   1) DNS rebinding: a malicious domain resolves to 127.0.0.1; its page can same-origin
#      access all APIs (including tampering with provider base_url to exfiltrate API keys)
#      → validate against a Host allowlist;
#   2) Cross-site writes: any web page can POST/fetch to http://127.0.0.1:8765 to trigger
#      deletes or config changes → validate Origin/Referer on write methods.
_ALLOWED_HOSTS = {f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}", f"[::1]:{config.PORT}"}

# Auth-exempt paths: login and status probes must be anonymously reachable; all other /api/*
# require a valid session.
_AUTH_EXEMPT = {"/api/auth/status", "/api/auth/login", "/api/auth/setup",
                "/api/version", "/api/i18n", "/openapi.json", "/docs", "/redoc",
                "/healthz", "/readyz", "/metrics"}


def _is_exempt(path: str) -> bool:
    """Whether a path is in the auth-exempt list (exact or prefix match)."""
    if path in _AUTH_EXEMPT:
        return True
    # Path-prefix exemptions: /api/i18n/<lang>.json, /api/audit/*, etc.
    for prefix in ("/api/i18n/", "/api/i18n",):
        if path == prefix or path.startswith(prefix):
            return True
    return False


@app.middleware("http")
async def local_guard(request: Request, call_next):
    host = request.headers.get("host", "")
    if host and host not in _ALLOWED_HOSTS:
        return JSONResponse({"detail": "Invalid Host"}, status_code=403)
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        # Browser-initiated write requests always carry Origin/Referer; non-browser clients
        # (curl, etc.) without either header are allowed by default.
        if origin and urlsplit(origin).netloc not in _ALLOWED_HOSTS:
            return JSONResponse({"detail": "Cross-site request rejected"}, status_code=403)
    # Auth: only intercept /api/*; static assets and the index page are passed through
    # (the SPA itself redirects to login).
    # Cookie validation runs after Origin checks so cross-site writes are rejected first
    # without burning a session lookup.
    path = request.url.path
    if path.startswith("/api/") and not _is_exempt(path):
        token = request.cookies.get(auth.cookie_name())
        if not auth.validate_session(token):
            auth.audit("unauthorized", path=path,
                       ip=request.client.host if request.client else "")
            return JSONResponse({"detail": "Not signed in or session expired"}, status_code=401)
    return await call_next(request)


# ---------- system endpoints (Task 2.4) ----------

@app.get("/healthz")
def healthz():
    """Liveness: process is up and the HTTP server is serving."""
    return {"ok": True, "ts": time.time()}


@app.get("/readyz")
def readyz():
    """Readiness: data dir is writable + task manager initialized."""
    try:
        test = config.DATA_DIR / ".readyz_probe"
        test.write_text("ok")
        test.unlink()
    except OSError as e:
        return JSONResponse({"ok": False, "reason": f"data dir not writable: {e}"},
                            status_code=503)
    return {"ok": True}


@app.get("/metrics")
def prometheus_metrics():
    """Prometheus exposition (no auth — intended for in-cluster scraping)."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/version")
def api_version():
    """Service version, read once from pyproject.toml at import time."""
    return {"version": _version, "name": "doc-translator"}


@app.get("/api/i18n")
def i18n_list():
    """List shipped UI language catalogs."""
    return {"languages": i18n.available_languages(), "default": "en"}


@app.get("/api/i18n/{lang}.json")
def i18n_catalog(lang: str):
    """Serve a UI catalog; falls back to English for unknown languages."""
    code = i18n.resolve_lang(lang.removesuffix(".json"))
    return i18n.get_catalog(code)


app.include_router(auth_api.router)
app.include_router(tasks.router)
app.include_router(providers.router)
app.include_router(previews.router)


@app.get("/")
def index():
    return FileResponse(config.STATIC_DIR / "index.html")


app.mount("/static", NoCacheStaticFiles(directory=config.STATIC_DIR), name="static")
