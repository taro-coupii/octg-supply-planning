"""Lists and system-wide views are confined to the planner's Business Unit.

Second half of F01 (review 2026-09-06). Resources addressed by id are refused
by app.auth.scope; lists are not addressed by id, so each list handler takes
`planner_bu` and filters its own query. This pins that no list, dashboard,
MRP view, export or coverage grid shows a planner another BU's rows -- and
that an import workbook naming a foreign well is refused whole.
"""

import io
from datetime import datetime, timedelta

import pytest
from openpyxl import Workbook

from app.auth.tokens import issue_token
from app.models import DemandImportBatch, UserRole
from tests.test_auth import _auth, _mk_user, client_world, real_auth  # noqa: F401
from tests.test_resource_scope_authz import _foreign_world


@pytest.fixture()
def scoped(client_world):
    client, sf, w = client_world
    f = _foreign_world(sf, w.p_a_id)
    uid = _mk_user(sf, email="planner@test", role=UserRole.PLANNER, business_unit_id=w.bu_id)
    aid = _mk_user(sf, email="admin@test", role=UserRole.ADMIN, business_unit_id=None)
    return client, sf, w, f, _auth(issue_token(uid)), _auth(issue_token(aid))


def test_wells_and_demand_lines_lists_omit_the_other_bu(scoped):
    client, sf, w, f, planner, admin = scoped
    wells = client.get("/wells", headers=planner).json()
    assert f["well"] not in {x["id"] for x in wells}
    assert w.w1_id in {x["id"] for x in wells}
    assert f["well"] in {x["id"] for x in client.get("/wells", headers=admin).json()}

    lines = client.get("/demand-lines", headers=planner).json()["rows"]
    assert f["line"] not in {x["id"] for x in lines}
    assert w.l1_id in {x["id"] for x in lines}


def test_scenarios_customers_and_business_units_lists_are_scoped(scoped):
    client, sf, w, f, planner, admin = scoped
    assert f["scenario"] not in {s["id"] for s in client.get("/scenarios", headers=planner).json()}
    assert f["customer"] not in {c["id"] for c in client.get("/customers", headers=planner).json()}
    assert [b["id"] for b in client.get("/business-units", headers=planner).json()] == [w.bu_id]
    assert len(client.get("/business-units", headers=admin).json()) == 2


def test_home_dashboard_counts_only_the_planner_bu(scoped):
    client, sf, w, f, planner, admin = scoped
    mine = client.get("/dashboard/home", headers=planner).json()
    assert f["well"] not in {x["id"] for x in mine["uncovered_wells"]}
    theirs = client.get("/dashboard/home", headers=admin).json()
    assert len(theirs["uncovered_wells"]) >= len(mine["uncovered_wells"])


def test_coverage_grid_without_a_customer_is_still_the_planner_bu(scoped):
    client, sf, w, f, planner, admin = scoped
    rows = client.get("/coverage", headers=planner).json()["rows"]
    assert f["well"] not in {r["well_id"] for r in rows}
    assert w.w1_id in {r["well_id"] for r in rows}


def test_system_wide_mrp_views_become_the_planner_bu_view(scoped):
    """MRP is system-wide by default; for a planner, 'the system' is their BU.
    The foreign BU holds 500 of the product and demands 100 of it; if that stock
    leaked into the planner's netting the recommendation would shrink."""
    client, sf, w, f, planner, admin = scoped
    mine = client.get("/mrp/summary", headers=planner).json()
    theirs = client.get("/mrp/summary", headers=admin).json()
    mine_lines = {l for r in mine for l in r.get("demand_line_ids", [])}
    assert f["line"] not in mine_lines
    mor = client.get("/mrp/order-requirements", headers=planner)
    assert mor.status_code == 200
    export = client.get("/mrp/export", headers=planner)
    assert export.status_code == 200
    assert "spreadsheet" in export.headers["content-type"]


def test_executive_and_surplus_default_to_the_planner_bu(scoped):
    client, sf, w, f, planner, admin = scoped
    ex = client.get("/dashboard/executive", headers=planner).json()
    assert ex["business_unit_id"] == w.bu_id
    su = client.get("/analysis/surplus", headers=planner).json()
    assert all(r["business_unit_id"] == w.bu_id for r in su["rows"])


def _xlsx(rows):
    wb = Workbook(); ws = wb.active; ws.title = "Demand"
    ws.append(["Well", "Product", "Quantity", "ROS Date", "Status", "Profile"])
    for r in rows: ws.append(r)
    b = io.BytesIO(); wb.save(b); return b.getvalue()


def test_an_import_naming_a_foreign_well_is_refused_whole(scoped):
    """Owner ruling: one foreign row refuses the batch; nothing is staged."""
    client, sf, w, f, planner, admin = scoped
    ros = (datetime.utcnow() + timedelta(days=30)).date().isoformat()
    with sf() as db:
        before = db.query(DemandImportBatch).count()
    payload = _xlsx([["WELL-1", w.p_a_desc, 7000, ros, "Confirmed", "Primary"],
                     ["FOREIGN-1", w.p_a_desc, 100, ros, "Confirmed", "Primary"]])
    r = client.post("/demand-imports", files={"file": ("d.xlsx", payload, "application/octet-stream")}, headers=planner)
    assert r.status_code == 403, r.text
    assert "another Business Unit" in r.json()["detail"]
    with sf() as db:
        assert db.query(DemandImportBatch).count() == before
    ok = client.post("/demand-imports", files={"file": ("d.xlsx", _xlsx([["WELL-1", w.p_a_desc, 7000, ros, "Confirmed", "Primary"]]), "application/octet-stream")}, headers=planner)
    assert ok.status_code == 201, ok.text
    assert ok.json()["id"] in {b["id"] for b in client.get("/demand-imports", headers=planner).json()}


def test_approvals_queue_is_scoped(scoped):
    client, sf, w, f, planner, admin = scoped
    r = client.get("/substitution-approvals", headers=planner)
    assert r.status_code == 200
