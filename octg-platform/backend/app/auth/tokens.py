"""HMAC-signed bearer tokens.

Format: base64url(json payload) + "." + base64url(hmac_sha256(payload, secret))
Payload: {"uid": <user id>, "exp": <unix seconds>}

Why not JWT: the only consumer of these tokens is this same process, so the
interoperability JWT buys is unused, while its flexibility (alg negotiation)
is exactly the part with the famous foot-guns. A fixed-algorithm signed blob
is the same idea with no negotiable surface. When Entra ID arrives its ID
token is validated by the PROVIDER (app.auth.provider) and then exchanged for
one of these session tokens -- the token layer does not change.

MVP-COMPROMISE[C-12]: AUTH_SECRET falls back to a hard-coded dev value, so a
deployment that forgets to set it issues forgeable tokens. See
MVP_COMPROMISES.md C-12.
"""

import base64
import hashlib
import hmac
import json
import os
import time

_DEV_SECRET = "octg-dev-secret-do-not-deploy"
TOKEN_TTL_SECONDS = 12 * 3600


def _secret() -> bytes:
    return os.environ.get("AUTH_SECRET", _DEV_SECRET).encode()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(user_id: str, now: float | None = None) -> str:
    payload = json.dumps(
        {"uid": user_id, "exp": int((now or time.time()) + TOKEN_TTL_SECONDS)},
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(_secret(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(signature)}"


def verify_token(token: str, now: float | None = None) -> str | None:
    """The user id, or None for anything invalid. One return path for every
    failure mode on purpose -- distinguishing "expired" from "forged" in the
    response would hand an attacker an oracle and buys the UI nothing (both
    end at the login screen)."""
    try:
        payload_b64, signature_b64 = token.split(".", 1)
        payload = _unb64(payload_b64)
        expected = hmac.new(_secret(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(signature_b64), expected):
            return None
        claims = json.loads(payload)
        if claims["exp"] < (now or time.time()):
            return None
        return str(claims["uid"])
    except (ValueError, KeyError, TypeError):
        return None
