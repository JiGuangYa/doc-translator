"""Process-local desktop authentication; enabled only by the native launcher."""
import hmac

_token: str | None = None


def configure(token: str) -> None:
    global _token
    if len(token) < 32:
        raise ValueError("Desktop token must contain at least 32 characters")
    _token = token


def enabled() -> bool:
    return _token is not None


def authenticated(value: str) -> bool:
    return bool(_token and hmac.compare_digest(value, _token))
