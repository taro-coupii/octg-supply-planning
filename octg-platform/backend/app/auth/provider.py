"""The pluggable "credential -> User row" step.

This is the Entra ID seam agreed with the user: dev runs a local
email+password check; production will validate a Microsoft Entra ID token and
match/provision the same `User` row by email. Everything downstream of this
module -- session tokens, dependencies, BU scoping -- sees only the row.

Selection is by the AUTH_PROVIDER env var ("dev" is the default and currently
the only implementation). An Entra implementation slots in as:

    class EntraProvider:
        def authenticate(self, db, *, email, password): ...  # 400: not supported
        def exchange_id_token(self, db, id_token) -> User | None:
            # validate signature/audience against the tenant's JWKS,
            # then match User by verified email claim
"""

import os
from typing import Protocol

from sqlalchemy.orm import Session

from app.auth.passwords import verify_password
from app.models import User


class AuthProvider(Protocol):
    def authenticate(
        self, db: Session, *, email: str, password: str
    ) -> User | None: ...


class DevPasswordProvider:
    """Local email+password against User.password_hash. Dev only."""

    def authenticate(
        self, db: Session, *, email: str, password: str
    ) -> User | None:
        user = db.query(User).filter(User.email == email.lower().strip()).first()
        # verify_password runs even for a missing user? No -- and deliberately
        # not: a timing oracle on user existence is irrelevant for a dev-only
        # provider, and the constant-time property that matters (hash
        # comparison) lives in verify_password itself.
        if user is None or not user.is_active:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user


def get_provider() -> AuthProvider:
    name = os.environ.get("AUTH_PROVIDER", "dev")
    if name == "dev":
        return DevPasswordProvider()
    raise RuntimeError(
        f"Unknown AUTH_PROVIDER {name!r}. Only 'dev' is implemented; "
        "see app/auth/provider.py for the Entra ID extension point."
    )
