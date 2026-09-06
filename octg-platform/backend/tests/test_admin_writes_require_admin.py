"""/admin/* writes need the administrator role; /admin/* reads do not.

Adversarial review 2026-09-06, F02: `require_admin` existed and was applied
nowhere, so a planner's real token could change lead times, the coverage-scope
defaults and the substitution master data for every Business Unit at once.

Owner ruling the same day: gate the /admin/* WRITES and only those. Reads stay
open because every planner screen depends on them, and the other writes the
review named (BU lifecycle, customer remap, manual company inventory) stay open
to planners by explicit choice -- recorded in MVP_COMPROMISES.md, not here.

These run with the REAL token -> User -> role chain (no admin override), which
is the only way a role gate can be tested at all.
"""

import pytest

from app.auth.tokens import issue_token
from app.models import UserRole
from tests.test_auth import _auth, _mk_user, client_world, real_auth  # noqa: F401


READS = [
    "/admin/coverage-scope-defaults",
    "/admin/lead-time-components",
    "/admin/technical-substitutions",
    "/admin/customer-substitution-rules",
    "/admin/safety-stocks",
]

WRITES = [
    ("put", "/admin/coverage-scope-defaults", {"status": ["Confirmed"], "profile": ["Primary"], "acknowledged": True}),
    ("post", "/admin/lead-time-components", {"dimension": "Grade", "attribute_value": "*", "months": 1, "label": "x"}),
    ("post", "/admin/technical-substitutions", {"from_product_id": "a", "to_product_id": "b"}),
    ("post", "/admin/customer-substitution-rules", {"customer_id": "c", "from_product_id": "a", "to_product_id": "b", "allowed": True}),
    ("put", "/admin/safety-stocks/some-product", {"quantity": 1}),
    ("delete", "/admin/safety-stocks/some-product", None),
    ("delete", "/admin/technical-substitutions/some-id", None),
    ("patch", "/admin/lead-time-components/some-id", {"months": 2}),
]


def _planner(session_factory, world):
    uid = _mk_user(session_factory, email="planner@test", role=UserRole.PLANNER, business_unit_id=world.bu_id)
    return _auth(issue_token(uid))


def _admin(session_factory):
    uid = _mk_user(session_factory, email="admin@test", role=UserRole.ADMIN, business_unit_id=None)
    return _auth(issue_token(uid))


@pytest.mark.parametrize("path", READS)
def test_a_planner_can_still_read_admin_master_data(client_world, path):
    client, sf, world = client_world
    resp = client.get(path, headers=_planner(sf, world))
    assert resp.status_code == 200, (path, resp.text)


@pytest.mark.parametrize("method,path,body", WRITES)
def test_a_planner_cannot_write_admin_master_data(client_world, method, path, body):
    """403 before the handler runs: the ids above need not exist, and the
    response must say the role that is missing, not 404 for a row it never
    looked up."""
    client, sf, world = client_world
    kwargs = {"headers": _planner(sf, world)}
    if body is not None:
        kwargs["json"] = body
    resp = getattr(client, method)(path, **kwargs)
    assert resp.status_code == 403, (method, path, resp.text)
    assert "administrator role" in resp.json()["detail"]


def test_an_admin_is_not_gated(client_world):
    """The gate is about the role, not the verb: an admin's write reaches the
    handler (and meets that handler's own 404 for a row that does not exist)."""
    client, sf, _world = client_world
    resp = client.delete("/admin/safety-stocks/no-such-product", headers=_admin(sf))
    assert resp.status_code == 404, resp.text


def test_the_gate_is_not_on_every_router(client_world):
    """What the owner chose NOT to gate must keep working for a planner, so a
    later change cannot widen the rule without a test noticing."""
    client, sf, world = client_world
    resp = client.get("/business-units", headers=_planner(sf, world))
    assert resp.status_code == 200
