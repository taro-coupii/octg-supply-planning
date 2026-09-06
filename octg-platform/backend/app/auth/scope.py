"""Resource-resolving Business Unit authorization (review 2026-09-06, F01).

WHAT WAS WRONG
==============
`enforce_customer_scope` looked at exactly two things: a `customer_id` query
parameter and a `customer_id` path parameter. Everything else authenticated and
stopped: GET /wells/{id} for another BU's well answered 200, POST /scenarios
with another BU's customer_id in the JSON body answered 201, PATCH
/company-inventory/on-hand/{id} rewrote another BU's stock. A UUID being hard to
guess is not a boundary -- the list endpoints handed the ids out.

WHAT THIS DOES
==============
One dependency, attached to every protected router, that walks every id a
request names -- in the path, the query string, or the JSON body -- back to the
Business Unit that owns it, and refuses the request with 403 and ZERO writes if
any of them lies outside the caller's BU. Administrators (no BU) pass.

The walk is the data model's own chain:
    well -> planning node -> customer -> BU
    demand line -> well -> ...
    scenario -> customer -> BU;  override -> scenario
    approval -> demand line -> ...
    import batch -> its rows' wells (see `bu_of_import_batch`)
    on-hand / on-order row -> business_unit_id;  assignment -> demand line
    customer-owned upload -> customer;  company upload -> business_unit_id
Products, lead-time components and the substitution master data are global and
carry no BU; they are deliberately not in the table.

UNKNOWN IDS PASS THROUGH. The handler's own 404 already speaks for a missing
row, and pre-empting it here would turn one honest error into two competing
ones. What must not happen is a 404 that leaks existence across the wall, and
it cannot: a row that exists and is foreign is a 403 here, before the handler,
so the handler never sees it and never gets to say "not found" about it.

BATCHES (owner ruling 2026-09-06): a request naming several resources is judged
as a whole. One foreign id refuses the whole request; nothing is applied
partially and the response names what was out of scope.

This module does not filter LISTS -- that is `planner_bu` plus each list
handler's own query, because a list is not addressed by id.
"""

from __future__ import annotations

import json
from typing import Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.db import get_db
from app.models import (
    BusinessUnit,
    CompanyInventoryUpload,
    Customer,
    CustomerOwnedInventoryUpload,
    DemandImportBatch,
    DemandImportRow,
    DemandLine,
    InventoryAssignment,
    InventoryOnHand,
    InventoryOnOrder,
    Scenario,
    ScenarioOverride,
    User,
    Well,
    WellSubstitutionApproval,
)


class _Unknown:
    """The id names nothing. Distinct from None (= the row exists but has no BU)."""


UNKNOWN = _Unknown()


def bu_of_customer(db: Session, customer_id: str):
    c = db.get(Customer, customer_id)
    return UNKNOWN if c is None else c.business_unit_id


def bu_of_well(db: Session, well_id: str):
    w = db.get(Well, well_id)
    if w is None:
        return UNKNOWN
    node = w.planning_node
    return UNKNOWN if node is None else bu_of_customer(db, node.customer_id)


def bu_of_demand_line(db: Session, line_id: str):
    line = db.get(DemandLine, line_id)
    return UNKNOWN if line is None else bu_of_well(db, line.well_id)


def bu_of_scenario(db: Session, scenario_id: str):
    s = db.get(Scenario, scenario_id)
    return UNKNOWN if s is None else (s.business_unit_id or UNKNOWN)


def bu_of_override(db: Session, override_id: str):
    o = db.get(ScenarioOverride, override_id)
    return UNKNOWN if o is None else bu_of_scenario(db, o.scenario_id)


def bu_of_approval(db: Session, approval_id: str):
    a = db.get(WellSubstitutionApproval, approval_id)
    return UNKNOWN if a is None else bu_of_demand_line(db, a.demand_line_id)


def bu_of_assignment(db: Session, row_id: str):
    a = db.get(InventoryAssignment, row_id)
    return UNKNOWN if a is None else bu_of_demand_line(db, a.demand_line_id)


def bu_of_on_hand(db: Session, row_id: str):
    r = db.get(InventoryOnHand, row_id)
    return UNKNOWN if r is None else r.business_unit_id


def bu_of_on_order(db: Session, row_id: str):
    r = db.get(InventoryOnOrder, row_id)
    return UNKNOWN if r is None else r.business_unit_id


def bu_of_business_unit(db: Session, bu_id: str):
    return UNKNOWN if db.get(BusinessUnit, bu_id) is None else bu_id


def bu_of_company_upload(db: Session, upload_id: str):
    u = db.get(CompanyInventoryUpload, upload_id)
    return UNKNOWN if u is None else u.business_unit_id


def bu_of_customer_owned_upload(db: Session, upload_id: str):
    u = db.get(CustomerOwnedInventoryUpload, upload_id)
    return UNKNOWN if u is None else bu_of_customer(db, u.customer_id)


def bus_of_import_batch(db: Session, batch_id: str) -> set | _Unknown:
    """Every BU an import batch touches, through the wells its rows matched.

    A batch has no owner column; its scope is the union of its rows' wells. A
    row whose well did not resolve contributes nothing (it will fail on its own
    at apply time). An empty set means the batch names no BU yet.
    """
    if db.get(DemandImportBatch, batch_id) is None:
        return UNKNOWN
    rows = db.query(DemandImportRow.well_id).filter(
        DemandImportRow.batch_id == batch_id, DemandImportRow.well_id.isnot(None)
    )
    found = set()
    for (well_id,) in rows:
        bu = bu_of_well(db, well_id)
        if bu is not UNKNOWN and bu is not None:
            found.add(bu)
    return found


# --- what each parameter name means ------------------------------------------

#: Path and query parameter names, and the resolver that walks each to a BU.
_ID_RESOLVERS: dict[str, Callable] = {
    "customer_id": bu_of_customer,
    "business_unit_id": bu_of_business_unit,
    "well_id": bu_of_well,
    "demand_line_id": bu_of_demand_line,
    "scenario_id": bu_of_scenario,
    "override_id": bu_of_override,
    "approval_id": bu_of_approval,
}

#: JSON body keys that name a resource. Same walk, same refusal.
_BODY_RESOLVERS: dict[str, Callable] = {
    "customer_id": bu_of_customer,
    "business_unit_id": bu_of_business_unit,
    "well_id": bu_of_well,
    "demand_line_id": bu_of_demand_line,
    "target_demand_line_id": bu_of_demand_line,
    "target_well_id": bu_of_well,
    "target_business_unit_id": bu_of_business_unit,
    "target_assignment_id": bu_of_assignment,
    "target_approval_id": bu_of_approval,
}

#: `row_id` means a different table depending on the path.
_ROW_ID_BY_PREFIX: tuple[tuple[str, Callable], ...] = (
    ("/company-inventory/on-hand/", bu_of_on_hand),
    ("/company-inventory/on-order/", bu_of_on_order),
    ("/company-inventory/assignments/", bu_of_assignment),
)


def _refuse(user: User, what: str) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail=(
            f"{what} is outside your Business Unit scope. Your account "
            f"({user.email}) is confined to one Business Unit; nothing was "
            "changed."
        ),
    )


def _check(user: User, label: str, resolved) -> None:
    """Refuse if `resolved` is a real, different BU. UNKNOWN and None pass."""
    if resolved is UNKNOWN or resolved is None:
        return
    if isinstance(resolved, set):
        foreign = sorted(b for b in resolved if b != user.business_unit_id)
        if foreign:
            raise _refuse(user, f"{label} (it touches another Business Unit)")
        return
    if resolved != user.business_unit_id:
        raise _refuse(user, label)


async def _json_body(request: Request) -> dict:
    if request.method not in ("POST", "PUT", "PATCH"):
        return {}
    if "application/json" not in request.headers.get("content-type", ""):
        return {}
    raw = await request.body()  # cached by Starlette; the handler reads it again
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}  # the handler's own 422 speaks for malformed JSON
    return parsed if isinstance(parsed, dict) else {}


async def enforce_resource_scope(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """403 when ANY resource the request names lies outside the caller's BU.

    Runs before the handler on every protected router, so a refusal is a
    refusal with zero writes.
    """
    if user.business_unit_id is None:
        return  # administrator

    path = request.url.path

    for name, value in request.path_params.items():
        if not isinstance(value, str) or not value:
            continue
        if name == "row_id":
            for prefix, resolver in _ROW_ID_BY_PREFIX:
                if path.startswith(prefix):
                    _check(user, f"Inventory row {value}", resolver(db, value))
                    break
            continue
        if name == "batch_id":
            _check(user, f"Import batch {value}", bus_of_import_batch(db, value))
            continue
        resolver = _ID_RESOLVERS.get(name)
        if resolver is not None:
            _check(user, f"{name.replace('_id', '').replace('_', ' ').title()} {value}", resolver(db, value))

    for name, resolver in _ID_RESOLVERS.items():
        value = request.query_params.get(name)
        if value:
            _check(user, f"{name.replace('_id', '').replace('_', ' ').title()} {value}", resolver(db, value))

    body = await _json_body(request)
    for name, resolver in _BODY_RESOLVERS.items():
        value = body.get(name)
        if isinstance(value, str) and value:
            _check(user, f"{name} {value}", resolver(db, value))


def planner_bu(user: User = Depends(get_current_user)) -> str | None:
    """The BU a LIST must be confined to: the caller's, or None for an admin.

    Lists are not addressed by id, so `enforce_resource_scope` cannot judge
    them; each list handler takes this and filters its own query.
    """
    return user.business_unit_id
