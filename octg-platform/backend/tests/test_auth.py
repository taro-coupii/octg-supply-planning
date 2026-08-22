"""Authentication and BU scoping.

These tests POP conftest's admin override (see `real_auth`) so requests run
through the real dependency chain: bearer token -> User row -> scope check.
"""

import pytest

from app.auth.deps import get_current_user
from app.auth.passwords import hash_password
from app.auth.tokens import issue_token, verify_token
from app.main import app
from app.models import BusinessUnit, Customer, User, UserRole
from tests.phase5_fixtures import build_client


def _mk_foreign_customer(session_factory):
    """A customer in a second BU, for the cross-BU refusal cases."""
    with session_factory() as db:
        bu = BusinessUnit(name="BU-2")
        db.add(bu)
        db.flush()
        customer = Customer(name="Foreign Co", business_unit_id=bu.id)
        db.add(customer)
        db.commit()
        return customer.id


@pytest.fixture()
def real_auth():
    app.dependency_overrides.pop(get_current_user, None)
    yield


@pytest.fixture()
def client_world(real_auth):
    client, session_factory, world = build_client()
    try:
        yield client, session_factory, world
    finally:
        app.dependency_overrides.clear()


def _mk_user(session_factory, **kwargs):
    defaults = dict(
        email="user@test",
        display_name="User",
        role=UserRole.PLANNER,
        password_hash=hash_password("pw"),
        is_active=True,
    )
    defaults.update(kwargs)
    with session_factory() as db:
        user = User(**defaults)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Token layer
# ---------------------------------------------------------------------------


def test_token_roundtrip_and_expiry():
    token = issue_token("u1", now=1000.0)
    assert verify_token(token, now=1000.0) == "u1"
    # 12h TTL: valid just before, dead just after
    assert verify_token(token, now=1000.0 + 12 * 3600 - 1) == "u1"
    assert verify_token(token, now=1000.0 + 12 * 3600 + 1) is None


def test_tampered_token_rejected():
    token = issue_token("u1")
    payload, signature = token.split(".")
    assert verify_token(f"{payload}x.{signature}") is None
    assert verify_token(f"{payload}.{signature[:-2]}aa") is None
    assert verify_token("garbage") is None


# ---------------------------------------------------------------------------
# Endpoints are closed
# ---------------------------------------------------------------------------


def test_all_business_routes_require_auth(client_world):
    client, _, _ = client_world
    for path in ["/wells", "/coverage", "/mrp/summary", "/dashboard/executive"]:
        resp = client.get(path)
        assert resp.status_code == 401, (path, resp.status_code)


def test_health_and_login_are_open(client_world):
    client, _, _ = client_world
    assert client.get("/health").status_code == 200
    # login itself must be reachable logged out (401 here = bad credentials,
    # not "login requires login")
    resp = client.post(
        "/auth/login", json={"email": "nobody@x", "password": "x"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid email or password"


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------


def test_login_and_me(client_world):
    client, session_factory, _ = client_world
    _mk_user(
        session_factory, email="admin@test", role=UserRole.ADMIN
    )
    resp = client.post(
        "/auth/login", json={"email": "admin@test", "password": "pw"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["email"] == "admin@test"
    assert body["user"]["role"] == "admin"

    me = client.get("/auth/me", headers=_auth(body["token"]))
    assert me.status_code == 200
    assert me.json()["email"] == "admin@test"


def test_login_wrong_password_and_inactive(client_world):
    client, session_factory, _ = client_world
    _mk_user(session_factory, email="a@test")
    _mk_user(session_factory, email="off@test", is_active=False)
    assert (
        client.post(
            "/auth/login", json={"email": "a@test", "password": "wrong"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/login", json={"email": "off@test", "password": "pw"}
        ).status_code
        == 401
    )


def test_entra_provisioned_user_cannot_dev_login(client_world):
    """NULL password_hash (an Entra-provisioned row) never authenticates in
    dev mode -- including with an empty password."""
    client, session_factory, _ = client_world
    _mk_user(session_factory, email="entra@test", password_hash=None)
    resp = client.post(
        "/auth/login", json={"email": "entra@test", "password": ""}
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------


def test_planner_confined_to_own_bu_customer(client_world):
    client, session_factory, world = client_world
    uid = _mk_user(
        session_factory,
        email="planner@test",
        business_unit_id=world.bu_id,
    )
    foreign_id = _mk_foreign_customer(session_factory)
    token = issue_token(uid)
    own = client.get(
        "/coverage",
        params={"customer_id": world.acme_id},
        headers=_auth(token),
    )
    assert own.status_code == 200, own.text
    foreign = client.get(
        "/coverage",
        params={"customer_id": foreign_id},
        headers=_auth(token),
    )
    assert foreign.status_code == 403
    assert "outside your Business Unit" in foreign.json()["detail"]


def test_admin_crosses_bus_freely(client_world):
    client, session_factory, world = client_world
    uid = _mk_user(
        session_factory, email="boss@test", role=UserRole.ADMIN
    )
    token = issue_token(uid)
    for cid in [world.acme_id, world.beta_id]:
        resp = client.get(
            "/coverage", params={"customer_id": cid}, headers=_auth(token)
        )
        assert resp.status_code == 200, resp.text


def test_unscoped_planner_refused_loudly(client_world):
    """A planner with no BU is a misconfiguration: 403 with the corrective
    action, never silently widened to all BUs."""
    client, session_factory, _ = client_world
    uid = _mk_user(session_factory, email="lost@test", business_unit_id=None)
    resp = client.get("/wells", headers=_auth(issue_token(uid)))
    assert resp.status_code == 403
    assert "no Business Unit" in resp.json()["detail"]


def test_access_token_query_param_downloads(client_world):
    """The <a href> download fallback (MVP-COMPROMISE[C-14])."""
    client, session_factory, world = client_world
    uid = _mk_user(session_factory, email="dl@test", role=UserRole.ADMIN)
    resp = client.get(
        "/demand-imports/template",
        params={
            "customer_id": world.acme_id,
            "access_token": issue_token(uid),
        },
    )
    assert resp.status_code == 200, resp.text
    assert "spreadsheetml" in resp.headers["content-type"]


def test_deleted_user_token_dies(client_world):
    client, session_factory, _ = client_world
    uid = _mk_user(session_factory, email="gone@test")
    token = issue_token(uid)
    with session_factory() as db:
        db.delete(db.get(User, uid))
        db.commit()
    resp = client.get("/wells", headers=_auth(token))
    assert resp.status_code == 401
