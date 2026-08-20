"""Seed dev users (spec §seed). Idempotent upsert by email — users are wipe-
exempt in seed_minimal (intentionally left across reseeds), so business-unit
ids that get regenerated on each reseed must be re-associated here rather
than assumed stable.

Run standalone from octg-platform/backend: .venv/bin/python -m seed.seed_users
"""

from sqlalchemy.orm import Session

from app.auth.passwords import hash_password
from app.models import BusinessUnit, User, UserRole

ADMIN_EMAIL = "admin@octg.dev"
PLANNER_EMAIL = "planner@octg.dev"
DEV_PASSWORD = "octg-dev"


def _upsert(db: Session, email: str, role: UserRole, business_unit_id: str | None) -> User:
    user = db.query(User).filter_by(email=email).first()
    if user is None:
        user = User(email=email, role=role, business_unit_id=business_unit_id, password_hash="")
        db.add(user)
    else:
        user.role = role
        user.business_unit_id = business_unit_id
    user.password_hash = hash_password(DEV_PASSWORD)
    return user


def seed_users(db: Session) -> None:
    norway = db.query(BusinessUnit).filter_by(name="SCEU Norway").first()
    _upsert(db, ADMIN_EMAIL, UserRole.ADMIN, None)
    _upsert(db, PLANNER_EMAIL, UserRole.PLANNER, norway.id if norway else None)
    db.flush()


if __name__ == "__main__":
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        seed_users(session)
        session.commit()
        print("seeded users: admin@octg.dev (ADMIN), planner@octg.dev (PLANNER, SCEU Norway)")
    finally:
        session.close()
