"""Login rate limiter.

Single-process in-memory limiter using slowapi. Each remote address is
allowed at most 5 failed-or-successful login attempts per minute. Beyond
that the endpoint returns 429 with a Retry-After header.

The limiter is wired into the FastAPI app via app.state.limiter in main.py
and reused as a dependency on the login endpoint.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

# 5 attempts / minute / IP — high enough that a forgetful human won't
# hit it, low enough that online brute force is impractical against a
# PBKDF2-200k password hash.
limiter = Limiter(key_func=get_remote_address)
