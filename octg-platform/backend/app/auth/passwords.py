"""PBKDF2 password hashing for the dev credential store.

Stdlib only, deliberately: this hash exists ONLY for the dev login
(app.auth.provider.DevPasswordProvider). Under Entra ID no password ever
reaches this platform, so pulling in bcrypt/argon2 for a credential path that
is scheduled to disappear would be dependency weight without a beneficiary.
600k iterations matches the 2023+ OWASP guidance for PBKDF2-SHA256.
"""

import hashlib
import hmac
import secrets

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _ITERATIONS
    ).hex()
    return f"{_ALGORITHM}${_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str | None) -> bool:
    """False for a NULL/malformed hash, never an exception -- a user row
    provisioned for Entra (password_hash NULL) must simply fail dev login."""
    if not stored:
        return False
    try:
        algorithm, iterations, salt, digest = stored.split("$", 3)
        if algorithm != _ALGORITHM:
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), int(iterations)
        ).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, digest)
