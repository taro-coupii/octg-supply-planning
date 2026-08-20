import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.passwords import hash_password
from app.auth.tokens import issue
from app.db import Base, get_db
from app.main import app
from app.models import User, UserRole


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestSession()
    yield session
    session.close()


@pytest.fixture()
def client(db):
    """Authenticated-by-default TestClient (ADMIN), so the existing suites
    written before stage 7 keep passing unmodified. Use `anon_client` for
    401/exemption tests."""
    def _override():
        yield db

    app.dependency_overrides[get_db] = _override

    admin = User(
        email="admin@octg.dev",
        password_hash=hash_password("octg-dev"),
        role=UserRole.ADMIN,
        business_unit_id=None,
    )
    db.add(admin)
    db.commit()

    tc = TestClient(app)
    tc.headers["Authorization"] = f"Bearer {issue(admin.id)}"
    yield tc
    app.dependency_overrides.clear()


@pytest.fixture()
def anon_client(db):
    """Unauthenticated TestClient, for 401/exemption tests."""
    def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()
