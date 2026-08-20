"""Auth provider abstraction (spec §認証コア). `get_provider()` selects an
implementation via the AUTH_PROVIDER env var (default "dev"). The Entra ID
provider is not implemented in this stage — only the seam is left below."""

import os
from typing import Protocol

from sqlalchemy.orm import Session

from app.auth.passwords import verify_password
from app.models import User


class AuthProvider(Protocol):
    def authenticate(self, db: Session, email: str, password: str) -> User | None: ...


class DevPasswordProvider:
    """Local email+password auth against the `users` table (PBKDF2)."""

    def authenticate(self, db: Session, email: str, password: str) -> User | None:
        user = db.query(User).filter_by(email=email).first()
        if user is None:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user


# Entra ID insertion point (scope §スコープ外 for this stage): a future
# EntraProvider would implement the same AuthProvider protocol, e.g.
#
#   class EntraProvider:
#       def authenticate(self, db: Session, email: str, password: str) -> User | None:
#           ...  # validate an Entra-issued token instead of a local password
#
# and get_provider() below would add an "entra" branch.


def get_provider() -> AuthProvider:
    name = os.environ.get("AUTH_PROVIDER", "dev")
    if name == "dev":
        return DevPasswordProvider()
    raise RuntimeError(f"Unknown AUTH_PROVIDER: {name!r}")
