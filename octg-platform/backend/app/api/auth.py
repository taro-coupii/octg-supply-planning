"""Login and session endpoints.

The only router included WITHOUT the auth dependencies (see app.main): /login
must be reachable logged-out, and /me carries its own get_current_user.

There is no /logout on purpose: the server keeps no session state to destroy
(tokens are self-expiring, TTL in app.auth.tokens), so logout is the client
discarding its token. An endpoint that pretended otherwise would imply
server-side revocation that does not exist -- the "don't fake what you don't
have" principle applied to auth.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.provider import get_provider
from app.auth.tokens import issue_token
from app.db import get_db
from app.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    business_unit_id: str | None
    business_unit_name: str | None


class LoginOut(BaseModel):
    token: str
    user: UserOut


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role.value,
        business_unit_id=user.business_unit_id,
        business_unit_name=(
            user.business_unit.name if user.business_unit else None
        ),
    )


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = get_provider().authenticate(
        db, email=body.email, password=body.password
    )
    if user is None:
        # One message for every failure (unknown email, wrong password,
        # deactivated account): anything finer is an account-enumeration
        # oracle on a screen that gains nothing from precision.
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return LoginOut(token=issue_token(user.id), user=_user_out(user))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return _user_out(user)
