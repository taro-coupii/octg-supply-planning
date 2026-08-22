import os

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app import db as db_module
from app.auth.deps import get_current_user
from app.main import app
from app.models import User, UserRole


@pytest.fixture(autouse=True)
def _authenticated_as_admin():
    """Every pre-auth test keeps passing by running as an all-BU admin.

    Authentication is enforced at router include time (app.main.AUTH_DEPS), so
    without this override every API test would now 401. Overriding
    get_current_user -- rather than disabling auth via some env flag -- keeps
    the production dependency graph intact: enforce_customer_scope still runs,
    it just sees an unscoped admin. Auth tests that need the real dependency
    pop this override themselves (see test_auth.py).
    """
    app.dependency_overrides[get_current_user] = lambda: User(
        id="test-admin",
        email="admin@test",
        display_name="Test Admin",
        role=UserRole.ADMIN,
        business_unit_id=None,
        is_active=True,
    )
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
