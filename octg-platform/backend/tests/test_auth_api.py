from app.auth.passwords import hash_password
from app.auth.tokens import issue
from app.main import app
from app.models import BusinessUnit, Customer, User, UserRole


def _admin(db):
    u = User(email="a@x.com", password_hash=hash_password("pw"), role=UserRole.ADMIN, business_unit_id=None)
    db.add(u)
    db.commit()
    return u


def _planner(db, bu_id):
    u = User(email="p@x.com", password_hash=hash_password("pw"), role=UserRole.PLANNER, business_unit_id=bu_id)
    db.add(u)
    db.commit()
    return u


# --- login / me ---


def test_login_happy_path(anon_client, db):
    admin = User(
        email="login@x.com", password_hash=hash_password("secret"), role=UserRole.ADMIN, business_unit_id=None
    )
    db.add(admin)
    db.commit()

    resp = anon_client.post("/auth/login", json={"email": "login@x.com", "password": "secret"})
    assert resp.status_code == 200
    body = resp.json()
    assert "token" in body
    assert body["user"] == {"email": "login@x.com", "role": "ADMIN", "business_unit_id": None}


def test_login_wrong_password_401(anon_client, db):
    admin = User(
        email="login2@x.com", password_hash=hash_password("secret"), role=UserRole.ADMIN, business_unit_id=None
    )
    db.add(admin)
    db.commit()

    resp = anon_client.post("/auth/login", json={"email": "login2@x.com", "password": "nope"})
    assert resp.status_code == 401


def test_login_unknown_email_401(anon_client, db):
    resp = anon_client.post("/auth/login", json={"email": "nobody@x.com", "password": "x"})
    assert resp.status_code == 401


def test_me_requires_auth(anon_client, db):
    resp = anon_client.get("/auth/me")
    assert resp.status_code == 401


def test_me_returns_current_user(anon_client, db):
    u = _admin(db)
    resp = anon_client.get("/auth/me", headers={"Authorization": f"Bearer {issue(u.id)}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "a@x.com"


# --- invariant 5: protected routers 401 without auth; /auth/login and SPA exempt ---


def test_protected_route_401_without_bearer(anon_client, db):
    resp = anon_client.get("/business-units")
    assert resp.status_code == 401


def test_protected_route_401_with_garbage_bearer(anon_client, db):
    resp = anon_client.get("/business-units", headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 401


def test_login_route_exempt_from_auth_dependency(anon_client, db):
    # No Authorization header is sent, and the route still runs its own
    # (unrelated) validation rather than the auth 401 — proven by getting a
    # different status for a malformed body than for bad credentials.
    resp = anon_client.post("/auth/login", json={"email": "x"})  # missing password
    assert resp.status_code == 422


def test_openapi_exempt_from_auth(anon_client, db):
    resp = anon_client.get("/openapi.json")
    assert resp.status_code == 200


def test_docs_exempt_from_auth(anon_client, db):
    resp = anon_client.get("/docs")
    assert resp.status_code == 200


def test_authenticated_client_reaches_protected_route(client):
    resp = client.get("/business-units")
    assert resp.status_code == 200


# --- invariant 2: PLANNER BU scoping / ADMIN cross-BU / unassigned PLANNER ---


def test_planner_other_bu_customer_id_403(anon_client, db):
    bu1 = BusinessUnit(name="BU1")
    bu2 = BusinessUnit(name="BU2")
    db.add_all([bu1, bu2])
    db.flush()
    cust = Customer(name="Cust in BU2", business_unit_id=bu2.id)
    db.add(cust)
    db.commit()
    planner = _planner(db, bu1.id)

    resp = anon_client.get(
        f"/customers?customer_id={cust.id}", headers={"Authorization": f"Bearer {issue(planner.id)}"}
    )
    assert resp.status_code == 403


def test_planner_own_bu_customer_id_200(anon_client, db):
    bu1 = BusinessUnit(name="BU1b")
    db.add(bu1)
    db.flush()
    cust = Customer(name="Cust in BU1", business_unit_id=bu1.id)
    db.add(cust)
    db.commit()
    planner = _planner(db, bu1.id)

    resp = anon_client.get(
        f"/customers?customer_id={cust.id}", headers={"Authorization": f"Bearer {issue(planner.id)}"}
    )
    assert resp.status_code == 200


def test_admin_crosses_bu_200(anon_client, db):
    bu1 = BusinessUnit(name="BU1c")
    bu2 = BusinessUnit(name="BU2c")
    db.add_all([bu1, bu2])
    db.flush()
    cust = Customer(name="Cust in BU2c", business_unit_id=bu2.id)
    db.add(cust)
    db.commit()
    admin = _admin(db)

    resp = anon_client.get(
        f"/customers?customer_id={cust.id}", headers={"Authorization": f"Bearer {issue(admin.id)}"}
    )
    assert resp.status_code == 200


def test_planner_without_bu_always_403(anon_client, db):
    # A PLANNER without a business unit can't legally exist per the model
    # invariant, but the auth dependency must still enforce the 403 for any
    # request as defense-in-depth if one is ever constructed out-of-band
    # (e.g. its BU was deleted out from under it — the model validator only
    # runs on insert/update of the User row itself, not on a bulk update).
    bu = BusinessUnit(name="BU-orphan-source")
    db.add(bu)
    db.flush()
    planner = User(email="orphan@x.com", password_hash=hash_password("pw"), role=UserRole.PLANNER, business_unit_id=bu.id)
    db.add(planner)
    db.commit()
    db.query(User).filter_by(email="orphan@x.com").update({"business_unit_id": None})
    db.commit()

    resp = anon_client.get("/business-units", headers={"Authorization": f"Bearer {issue(planner.id)}"})
    assert resp.status_code == 403


# --- invariant 6: no logout route ---


def test_no_logout_route_exists(anon_client, db):
    paths = set(anon_client.get("/openapi.json").json()["paths"].keys())
    assert "/auth/logout" not in paths


# --- invariant 7: require_admin defined-but-unapplied — PLANNER reaches /admin ---


def test_planner_reaches_admin_route_200(anon_client, db):
    bu = BusinessUnit(name="BU-admin-test")
    db.add(bu)
    db.flush()
    planner = _planner(db, bu.id)

    resp = anon_client.get(
        "/admin/coverage-scope", headers={"Authorization": f"Bearer {issue(planner.id)}"}
    )
    # Documents the faithfully-reproduced gap (COMPROMISE[C-15R]): require_admin
    # exists but isn't wired to /admin routes, so a PLANNER isn't blocked here.
    assert resp.status_code == 200
