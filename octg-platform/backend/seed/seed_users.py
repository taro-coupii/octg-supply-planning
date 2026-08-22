"""Provision dev logins. Idempotent: matches by email, updates in place.

Usage:
    DATABASE_URL="sqlite:///./dev.db" python -m seed.seed_users

Creates one all-BU admin plus one planner per Business Unit found in the
database. Passwords are dev-only (this whole file is dev-only -- an
Entra-backed deployment provisions users from the tenant instead):

    admin@octg.dev                      admin / "octg-dev"
    planner+<bu-slug>@octg.dev          planner scoped to that BU / "octg-dev"
"""

import re

from app.auth.passwords import hash_password
from app.db import SessionLocal
from app.models import BusinessUnit, User, UserRole

DEV_PASSWORD = "octg-dev"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def upsert(db, *, email, display_name, role, business_unit_id=None):
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(email=email)
        db.add(user)
    user.display_name = display_name
    user.role = role
    user.business_unit_id = business_unit_id
    user.password_hash = hash_password(DEV_PASSWORD)
    user.is_active = True
    return user


def main() -> None:
    db = SessionLocal()
    try:
        # Planners are provisioned per Business Unit; a re-seed that removed a
        # BU leaves its planner pointing at a dangling id, and an unscoped (or
        # mis-scoped) planner must not survive. Delete rather than deactivate:
        # these are dev logins, and a dead BU's login has nothing to protect.
        live_bu_ids = {bu.id for bu in db.query(BusinessUnit).all()}
        for user in db.query(User).filter(User.role == UserRole.PLANNER).all():
            if user.business_unit_id not in live_bu_ids:
                print(f"removing stale planner {user.email} (BU gone)")
                db.delete(user)
        db.flush()

        upsert(
            db,
            email="admin@octg.dev",
            display_name="Dev Admin",
            role=UserRole.ADMIN,
        )
        for bu in db.query(BusinessUnit).order_by(BusinessUnit.name).all():
            upsert(
                db,
                email=f"planner+{_slug(bu.name)}@octg.dev",
                display_name=f"{bu.name} Planner",
                role=UserRole.PLANNER,
                business_unit_id=bu.id,
            )
        db.commit()
        for u in db.query(User).order_by(User.email).all():
            print(f"{u.email:40s} {u.role.value:8s} bu={u.business_unit_id}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
