"""HMAC-signed bearer tokens (spec §認証コア). No server-side session: the
token payload {uid, exp} is fully self-contained and verified by signature.

`now` is injectable on both issue() and verify() so TTL-boundary tests don't
depend on wall-clock timing.
"""

import base64
import hashlib
import hmac
import json
import os
import time

TTL_SECONDS = 12 * 60 * 60  # 12h

# COMPROMISE[C-12R]: AUTH_SECRET should be a required env var in production;
# this dev fallback lets the app boot with a fixed, publicly-known secret
# when it isn't set. Faithfully reproduces the original implementation's MVP
# shortcut (see docs/superpowers/COMPROMISES.md).
_DEV_SECRET = "octg-dev-insecure-auth-secret-do-not-use-in-prod"


def _secret() -> bytes:
    return os.environ.get("AUTH_SECRET", _DEV_SECRET).encode("utf-8")


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def issue(uid: str, now: float | None = None) -> str:
    """Return a signed token for uid, expiring TTL_SECONDS after `now`."""
    if now is None:
        now = time.time()
    payload = {"uid": uid, "exp": now + TTL_SECONDS}
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64encode(payload_bytes)
    signature = hmac.new(_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64encode(signature)}"


def verify(token: str, now: float | None = None) -> str | None:
    """Return uid if the token's signature is valid and it isn't expired,
    else None. Never raises on malformed input."""
    if now is None:
        now = time.time()
    try:
        payload_b64, signature_b64 = token.split(".")
        expected_signature = hmac.new(
            _secret(), payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        actual_signature = _b64decode(signature_b64)
        if not hmac.compare_digest(expected_signature, actual_signature):
            return None
        payload = json.loads(_b64decode(payload_b64))
        exp = payload["exp"]
        uid = payload["uid"]
    except Exception:
        return None

    if now > exp:
        return None
    return uid
