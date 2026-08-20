from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.provider import get_provider
from app.auth.tokens import issue
from app.db import get_db
from app.models import User

router = APIRouter(prefix="/auth")


class LoginIn(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    email: str
    role: str
    business_unit_id: str | None


class LoginOut(BaseModel):
    token: str
    user: UserOut


# logout is intentionally not implemented: tokens are stateless and
# self-expire (spec §適用) — there is no server-side session to invalidate.


@router.post("/login", response_model=LoginOut)
def login(payload: LoginIn, db: Session = Depends(get_db)):
    provider = get_provider()
    user = provider.authenticate(db, payload.email, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = issue(user.id)
    return LoginOut(
        token=token,
        user=UserOut(email=user.email, role=user.role.value, business_unit_id=user.business_unit_id),
    )


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return UserOut(email=user.email, role=user.role.value, business_unit_id=user.business_unit_id)
