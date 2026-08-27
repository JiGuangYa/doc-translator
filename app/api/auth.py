"""Auth API: status query / first-time admin password setup / sign in / sign out."""
import os

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import auth
from ..services.rate_limit import limiter

router = APIRouter()

MIN_PASSWORD_LEN = 8


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _cookie_secure() -> bool:
    """Whether the session cookie should be marked Secure.

    Scenario A (single host, HTTP) defaults to off. Set the env var
    DOC_TRANSLATOR_COOKIE_SECURE=1 to enable it explicitly (for deployments
    behind a reverse proxy terminating HTTPS). When enabled, the browser
    only sends the cookie back over HTTPS.
    """
    return os.environ.get("DOC_TRANSLATOR_COOKIE_SECURE", "").strip() in ("1", "true", "yes", "on")


def _set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(auth.cookie_name(), token, httponly=True, samesite="strict",
                    max_age=auth.SESSION_TTL, path="/", secure=_cookie_secure())


@router.get("/api/auth/status")
def auth_status(request: Request):
    """Called by the frontend on startup: decides whether to go to the sign-in page
    or to the Workbench (this endpoint is anonymously reachable)."""
    token = request.cookies.get(auth.cookie_name())
    return {"configured": auth.is_configured(),
            "authenticated": auth.validate_session(token)}


class PasswordBody(BaseModel):
    password: str


@router.post("/api/auth/setup")
def setup(body: PasswordBody, request: Request):
    if auth.is_configured():
        raise HTTPException(400, "Password already set, please sign in directly")
    if len(body.password) < MIN_PASSWORD_LEN:
        raise HTTPException(400, f"Password must be at least {MIN_PASSWORD_LEN} characters")
    auth.set_password(body.password)
    token = auth.create_session()
    auth.audit("setup", ip=_client_ip(request))
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, token)
    return resp


class LoginBody(BaseModel):
    password: str


@router.post("/api/auth/login")
@limiter.limit("5/minute")
def login(request: Request, body: LoginBody):
    if not auth.is_configured():
        raise HTTPException(400, "Admin password not set, please complete setup first")
    if not auth.verify_login(body.password):
        auth.audit("login_fail", ip=_client_ip(request))
        # Uniform error message — don't give probers any extra information.
        raise HTTPException(401, "Incorrect password")
    token = auth.create_session()
    auth.audit("login_ok", ip=_client_ip(request))
    resp = JSONResponse({"ok": True})
    _set_session_cookie(resp, token)
    return resp


@router.post("/api/auth/logout")
def logout(request: Request):
    token = request.cookies.get(auth.cookie_name())
    was_valid = auth.validate_session(token)
    auth.drop_session(token)
    if was_valid:
        auth.audit("logout", ip=_client_ip(request))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.cookie_name(), path="/")
    return resp
