import os

import pytest

from app.auth import tokens
from app.auth.passwords import hash_password, verify_password
from app.auth.provider import DevPasswordProvider, get_provider
from app.models import BusinessUnit, User, UserRole
from seed.seed_users import DEV_PASSWORD, seed_users


# --- invariant 3: PBKDF2 password hashing ---


def test_hash_password_verifies_correct_password():
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded) is True


def test_hash_password_rejects_wrong_password():
    encoded = hash_password("correct horse battery staple")
    assert verify_password("wrong password", encoded) is False


def test_hash_password_uses_distinct_salts():
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b


# --- invariant 1: token TTL boundaries, tamper, no-bearer ---


def test_token_valid_at_ttl_minus_one_second():
    now = 1_000_000.0
    token = tokens.issue("user-1", now=now)
    just_before_expiry = now + tokens.TTL_SECONDS - 1
    assert tokens.verify(token, now=just_before_expiry) == "user-1"


def test_token_invalid_at_ttl_plus_one_second():
    now = 1_000_000.0
    token = tokens.issue("user-1", now=now)
    just_after_expiry = now + tokens.TTL_SECONDS + 1
    assert tokens.verify(token, now=just_after_expiry) is None


def test_token_rejects_tampered_payload():
    now = 1_000_000.0
    token = tokens.issue("user-1", now=now)
    payload_b64, sig_b64 = token.split(".")
    tampered = tokens.issue("user-2", now=now).split(".")[0] + "." + sig_b64
    assert tokens.verify(tampered, now=now) is None


def test_token_rejects_garbage():
    assert tokens.verify("not-a-token", now=1_000_000.0) is None
    assert tokens.verify("", now=1_000_000.0) is None


# --- invariant 4: provider selection ---


def test_get_provider_dev_by_default(monkeypatch):
    monkeypatch.delenv("AUTH_PROVIDER", raising=False)
    assert isinstance(get_provider(), DevPasswordProvider)


def test_get_provider_dev_explicit(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "dev")
    assert isinstance(get_provider(), DevPasswordProvider)


def test_get_provider_unknown_raises(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "bogus")
    with pytest.raises(RuntimeError):
        get_provider()


def test_dev_provider_authenticates_valid_credentials(db):
    bu = BusinessUnit(name="Test BU")
    db.add(bu)
    db.flush()
    user = User(
        email="a@example.com",
        password_hash=hash_password("secret"),
        role=UserRole.ADMIN,
        business_unit_id=None,
    )
    db.add(user)
    db.commit()

    provider = DevPasswordProvider()
    assert provider.authenticate(db, "a@example.com", "secret").id == user.id
    assert provider.authenticate(db, "a@example.com", "wrong") is None
    assert provider.authenticate(db, "nobody@example.com", "secret") is None


# --- PLANNER-requires-BU model validation ---


def test_planner_without_bu_rejected(db):
    user = User(email="p@example.com", password_hash="x", role=UserRole.PLANNER, business_unit_id=None)
    db.add(user)
    with pytest.raises(Exception):
        db.flush()


def test_planner_with_bu_accepted(db):
    bu = BusinessUnit(name="Test BU 2")
    db.add(bu)
    db.flush()
    user = User(email="p2@example.com", password_hash="x", role=UserRole.PLANNER, business_unit_id=bu.id)
    db.add(user)
    db.flush()  # should not raise


def test_admin_without_bu_accepted(db):
    user = User(email="a2@example.com", password_hash="x", role=UserRole.ADMIN, business_unit_id=None)
    db.add(user)
    db.flush()  # should not raise


# --- users survive seed wipe ---


def test_seed_users_upserts_and_survives_bu_regeneration(db):
    norway = BusinessUnit(name="SCEU Norway")
    db.add(norway)
    db.flush()
    seed_users(db)
    db.commit()

    admin = db.query(User).filter_by(email="admin@octg.dev").first()
    planner = db.query(User).filter_by(email="planner@octg.dev").first()
    assert admin.role == UserRole.ADMIN
    assert planner.role == UserRole.PLANNER
    assert planner.business_unit_id == norway.id
    admin_id, planner_id = admin.id, planner.id

    # Simulate seed_minimal's wipe-and-recreate of business units: nullify
    # the FK first (bulk update bypasses the ORM validator), delete the old
    # BU rows, recreate with a fresh id, and reseed.
    db.query(User).update({User.business_unit_id: None})
    db.query(BusinessUnit).delete()
    new_norway = BusinessUnit(name="SCEU Norway")
    db.add(new_norway)
    db.flush()
    seed_users(db)
    db.commit()

    admin2 = db.query(User).filter_by(email="admin@octg.dev").first()
    planner2 = db.query(User).filter_by(email="planner@octg.dev").first()
    assert admin2.id == admin_id
    assert planner2.id == planner_id
    assert planner2.business_unit_id == new_norway.id
    assert verify_password(DEV_PASSWORD, planner2.password_hash)
    assert db.query(User).count() == 2
