"""Business Units and customers.

    GET    /business-units      list them
    POST   /business-units      create one
    PATCH  /business-units/{id} rename one
    DELETE /business-units/{id} delete one, only while nothing points at it
    GET    /customers           list them, with the policy their coverage is judged under
    GET    /customers/{id}      one of them
    PATCH  /customers/{id}      change its Business Unit and/or its allocation policy

WHY THE WRITES LIVE HERE AND NOT IN `app.api.admin`
===================================================
They are administrative in intent and the Administration screen is their only caller,
but they are writes to THESE resources at THESE paths. `POST /business-units` beside
`GET /business-units` is one router owning one collection; an `/admin/business-units`
would give the platform two paths for one thing, and a client would have to know that
reading and writing a Business Unit happen at different URLs. `app.api.admin` owns the
paths that have no non-admin reader (`/admin/lead-time-components`,
`/admin/coverage-scope-defaults`, the two substitution tables). These do.

The ENGINE follows the admin pattern exactly -- `app.engines.customer_admin` is the
deliberate sibling of `app.engines.lead_time_admin` and
`app.engines.substitution_admin`, with the same snapshot / mutate / recompute / diff
shape and the same SAVEPOINT isolation.

WHAT BECAME WRITEABLE, AND WHAT IS STILL NOT
============================================
The customer MASTER is still read-only, and that is not an oversight: nothing here
creates, renames or deletes a customer, because who the customers are is maintained
upstream. What is writeable is the two fields this platform owns:

  `Customer.business_unit_id`   this platform's mapping of a customer onto an inventory
                                pool whose `InventoryOnHand` rows are its own. The FK
                                is NULLABLE precisely so an operator can create a
                                customer, see which screens refuse to compute, and fix
                                the mapping -- see `app.models.customer.Customer`,
                                which calls making it NOT NULL "a defensible
                                follow-up". Until now there was no way to perform the
                                fix that docstring described.
  `Customer.allocation_policy`  a commercial modelling choice ("Allocation Rules Vary
                                By Business Model"). Oracle holds no such column; the
                                values arrived by being typed into a seed script.

THERE IS DELIBERATELY NO DELETE /business-units/{id}
====================================================
Creation was the requested feature; delete is neither cheap nor safe here, and the two
reasons are different in kind.

The cheap-looking one is customers: a BU with customers mapped to it cannot be deleted
without orphaning them, and "orphaned" is not a cosmetic state -- it is the unmapped
state whose every consequence `PATCH /customers/{id}` REFUSES to inflict deliberately
(see `app.engines.customer_admin`, refusal 2). A delete that refused while customers
reference it would therefore be a coherent endpoint, and it is the one an operator
would reach for.

The reason it is still not built is `InventoryOnHand`. On-hand quantity is keyed on
(business_unit_id, product_id) and is a read-only projection of Oracle-owned data (see
`app.models.inventory_on_hand.InventoryOnHand`). An empty BU can hold thousands of
those rows, and deleting the BU would either strand them pointing at nothing or delete
somebody else's data through a cascade this platform has no authority to trigger. That
is a decision about a feed this API does not own, and it needs its own design -- not a
convenience verb attached to a creation feature. A BU created here by mistake and left
empty costs a row and one line in a dropdown; the honest fix is a name that says so
until delete is designed properly.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.engines.customer_admin import (
    UNSET,
    CustomerConfigChange,
    CustomerConfigRefused,
    apply_customer_config_change,
    parse_allocation_policy,
)
from app.auth.scope import planner_bu
from app.models import (
    BusinessUnit,
    CompanyInventoryUpload,
    Customer,
    InventoryOnHand,
    InventoryOnOrder,
    ScenarioOverride,
    User,
)
from app.schemas import (
    BusinessUnitCreatedOut,
    BusinessUnitDeleteBlockedOut,
    BusinessUnitIn,
    BusinessUnitOut,
    BusinessUnitPatch,
    CustomerConfigChangeOut,
    CustomerConfigPatch,
    CustomerOut,
    WellCoverageRollupChangeOut,
)

router = APIRouter(tags=["customers"])


@router.get("/business-units", response_model=list[BusinessUnitOut])
def list_business_units(
    db: Session = Depends(get_db), bu_scope: str | None = Depends(planner_bu)
):
    """Business Units -- the outermost inventory boundary.

    A BU is a HARD boundary: on-hand quantity belongs to a (BU, product) pair and is
    never offered outside its BU by any code path. This endpoint exists so the UI can
    label which BU's stock a coverage figure was computed against, scope the
    cross-customer sharing analysis, and -- since `PATCH /customers/{id}` -- populate
    the remap picker.
    """
    query = db.query(BusinessUnit)
    if bu_scope is not None:
        query = query.filter(BusinessUnit.id == bu_scope)
    return query.order_by(BusinessUnit.name).all()


@router.post("/business-units", response_model=BusinessUnitCreatedOut, status_code=201)
def create_business_unit(payload: BusinessUnitIn, db: Session = Depends(get_db)):
    """Create one Business Unit.

    Creating a BU changes NO coverage verdict and needs no recompute, which makes it
    the least consequential write on the Administration screen: an empty BU has no
    customers, so no pass reads it. That is exactly why the response says what it
    CANNOT yet do -- a new BU looks configured and is inert, and the interesting
    failure comes one step later, when a customer is mapped into a BU that holds no
    `InventoryOnHand` rows. `PATCH /customers/{id}` refuses that remap, so this note
    warns about it here rather than letting the operator meet it as a 424.

    Two refusals, both with a stated reason:

      * an empty or whitespace name is a 400. The name is the only handle a human has
        on a BU (the id is a uuid), so a blank one would be unselectable in the very
        picker this row exists to appear in.
      * a duplicate name is a 409, matched case-INSENSITIVELY and naming the existing
        row. The database constraint is case-sensitive and is the backstop for two
        concurrent requests; see `app.models.business_unit.BusinessUnit`.
    """
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail=(
                "name is required and must not be blank. A Business Unit's name is the "
                "only handle a human has on it -- the id is a uuid, and every screen, "
                "every coverage provenance label and the customer remap picker render "
                "the name. A blank one would create a row that cannot be identified in "
                "the picker it exists to appear in. Nothing was saved."
            ),
        )

    # Case-insensitive pre-check. The constraint is case-sensitive (sqlite would need a
    # functional index otherwise), so this is deliberately STRICTER than the database:
    # two BUs differing only in case would be indistinguishable in a dropdown, and the
    # click they sit next to remaps a customer into a different warehouse.
    existing = (
        db.query(BusinessUnit)
        .filter(func.lower(BusinessUnit.name) == name.lower())
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"a Business Unit named {existing.name!r} already exists (id "
                f"{existing.id}). Names are unique, and the check is "
                "case-insensitive: the name is the only handle a human has on a BU, so "
                "two rows that only a database could tell apart would put an operator "
                "one indistinguishable click away from remapping a customer into the "
                "wrong inventory pool -- and a Business Unit is an absolute inventory "
                "boundary, so that is a different warehouse, not a different label. If "
                "you meant a second, genuinely separate unit, give it a name that "
                "distinguishes it. Nothing was saved."
            ),
        )

    unit = BusinessUnit(name=name)
    db.add(unit)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        text = str(getattr(exc, "orig", exc))
        if "uq_business_unit_name" in text or "UNIQUE" in text.upper():
            raise HTTPException(
                status_code=409,
                detail=(
                    f"a Business Unit named {name!r} already exists -- it was created "
                    "by another request while this one was in flight. Nothing was "
                    "saved. Re-read GET /business-units."
                ),
            ) from exc
        raise HTTPException(
            status_code=400,
            detail=(
                "the database refused this Business Unit and the reason is not the name "
                f"uniqueness rule: {text}. Nothing was saved."
            ),
        ) from exc
    db.commit()

    return BusinessUnitCreatedOut(
        business_unit=BusinessUnitOut(id=unit.id, name=unit.name),
        inventory_on_hand_row_count=0,
        note=(
            f"Created Business Unit {unit.name!r}. No coverage verdict changed and "
            "nothing was recomputed: a Business Unit with no customers is read by no "
            "coverage pass. IT IS ALSO INERT IN A SECOND WAY THAT MATTERS -- it holds "
            "NO InventoryOnHand rows. On-hand quantity is keyed on (Business Unit, "
            "product) and is an Oracle-owned projection this platform does not write, "
            "so until those rows arrive this BU has no steel in it. Mapping a customer "
            "into it now will be REFUSED (PATCH /customers/{id} rolls the remap back) "
            "for every product that customer demands and this BU has no row for -- "
            "deliberately, because a customer sitting under a BU whose inventory cannot "
            "answer for its demand looks correctly configured on every screen while "
            "every coverage read for it fails. Load the inventory rows first, then "
            "remap."
        ),
    )


def _blockers(db: Session, bu_id: str) -> dict[str, int]:
    """Count everything that still resolves through this Business Unit.

    Every one of these carries a `business_unit_id`. A BU is the ABSOLUTE inventory
    boundary, so a row left pointing at a deleted one is not a cosmetic dangling
    reference -- it is a customer whose pool cannot be resolved, and
    `recompute_customer` answers that with a refusal rather than a verdict. The count
    is taken for all six even once one is non-zero, because the operator's next
    question is "what do I have to move", not "what did you notice first".
    """
    return {
        "customer_count": db.query(Customer).filter_by(business_unit_id=bu_id).count(),
        "inventory_on_hand_row_count": db.query(InventoryOnHand)
        .filter_by(business_unit_id=bu_id)
        .count(),
        "inventory_on_order_row_count": db.query(InventoryOnOrder)
        .filter_by(business_unit_id=bu_id)
        .count(),
        "company_inventory_upload_count": db.query(CompanyInventoryUpload)
        .filter_by(business_unit_id=bu_id)
        .count(),
        "user_count": db.query(User).filter_by(business_unit_id=bu_id).count(),
        "scenario_override_count": db.query(ScenarioOverride)
        .filter_by(target_business_unit_id=bu_id)
        .count(),
    }


@router.patch("/business-units/{business_unit_id}", response_model=BusinessUnitOut)
def rename_business_unit(
    business_unit_id: str,
    payload: BusinessUnitPatch,
    db: Session = Depends(get_db),
):
    """Rename one Business Unit. Nothing else about it is editable.

    NO COVERAGE VERDICT CHANGES and nothing is recomputed. Every pool, assignment and
    scope resolves through the BU *id*; the name is what screens render. That is the
    whole reason a rename is safe while `PATCH /customers/{id}`'s remap -- which moves
    a customer to a different id -- has to recompute and can be refused.

    The two refusals mirror `POST /business-units` exactly, and for the same reason
    rather than for symmetry's sake: a blank name would make the row unselectable in
    the picker it exists to appear in, and a duplicate name -- matched
    case-INSENSITIVELY, stricter than the database's case-sensitive constraint --
    would put an operator one indistinguishable click away from remapping a customer
    into the wrong warehouse.

    Renaming a BU to the name it already has is accepted and is a no-op; it is not
    treated as a duplicate of itself.
    """
    unit = db.get(BusinessUnit, business_unit_id)
    if unit is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no Business Unit with id {business_unit_id!r}. Nothing was saved."
            ),
        )

    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(
            status_code=400,
            detail=(
                "name is required and must not be blank. A Business Unit's name is the "
                "only handle a human has on it -- the id is a uuid, and every screen, "
                "every coverage provenance label and the customer remap picker render "
                "the name. A blank one would create a row that cannot be identified in "
                "the picker it exists to appear in. Nothing was saved."
            ),
        )

    clash = (
        db.query(BusinessUnit)
        .filter(func.lower(BusinessUnit.name) == name.lower())
        .filter(BusinessUnit.id != unit.id)
        .first()
    )
    if clash is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"a different Business Unit is already named {clash.name!r} (id "
                f"{clash.id}). Names are unique and the check is case-insensitive: two "
                "rows that only a database could tell apart would put an operator one "
                "indistinguishable click away from remapping a customer into the wrong "
                "inventory pool -- and a Business Unit is an absolute inventory "
                "boundary, so that is a different warehouse, not a different label. "
                "Nothing was saved."
            ),
        )

    previous = unit.name
    unit.name = name
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"a Business Unit named {name!r} already exists -- it was created or "
                "renamed by another request while this one was in flight. Nothing was "
                "saved. Re-read GET /business-units."
            ),
        ) from exc
    db.commit()

    _ = previous
    return BusinessUnitOut(id=unit.id, name=unit.name)


@router.delete("/business-units/{business_unit_id}", status_code=204)
def delete_business_unit(business_unit_id: str, db: Session = Depends(get_db)):
    """Delete one Business Unit, and ONLY while nothing still points at it.

    The refusal is the feature. A BU is the absolute inventory boundary, so deleting
    one that still has customers -- or on-hand rows, or a planner pinned to it --
    does not tidy anything up: it strands every one of those rows against an id that
    no longer resolves, and the first symptom is a customer whose coverage cannot be
    computed at all. There is deliberately no cascade and no force flag. Move the
    customers (`PATCH /customers/{id}`), let the inventory feed retire the rows, then
    delete the empty shell.

    A 409 carries the itemised counts (`BusinessUnitDeleteBlockedOut`), not just a
    sentence, so the screen can list what has to move rather than sending the operator
    hunting for it.
    """
    unit = db.get(BusinessUnit, business_unit_id)
    if unit is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no Business Unit with id {business_unit_id!r}. Nothing was deleted."
            ),
        )

    counts = _blockers(db, unit.id)
    if any(counts.values()):
        parts = [
            f"{count} {label.replace('_count', '').replace('_row', ' row').replace('_', ' ')}"
            for label, count in counts.items()
            if count
        ]
        blocked = BusinessUnitDeleteBlockedOut(
            business_unit=BusinessUnitOut(id=unit.id, name=unit.name),
            **counts,
            note=(
                f"Business Unit {unit.name!r} still has {', '.join(parts)} pointing at "
                "it, so it was NOT deleted and nothing was changed. A Business Unit is "
                "the absolute inventory boundary: every one of those rows resolves "
                "through this id, and deleting it would strand them -- a customer left "
                "behind cannot have its coverage computed at all, which surfaces as a "
                "refusal on every screen rather than as a missing Business Unit. There "
                "is no cascade and no force: move the customers with PATCH "
                "/customers/{id}, let the inventory feed retire the rows, then delete "
                "the empty unit."
            ),
        )
        raise HTTPException(status_code=409, detail=blocked.model_dump())

    db.delete(unit)
    db.commit()
    return None


@router.get("/customers", response_model=list[CustomerOut])
def list_customers(
    db: Session = Depends(get_db), bu_scope: str | None = Depends(planner_bu)
):
    """Customers with the allocation policy their coverage is computed under, so
    the UI can label which model (soft / hard / hybrid) a well is judged by."""
    query = db.query(Customer)
    if bu_scope is not None:
        query = query.filter(Customer.business_unit_id == bu_scope)
    return query.order_by(Customer.name).all()


@router.get("/customers/{customer_id}", response_model=CustomerOut)
def get_customer(customer_id: str, db: Session = Depends(get_db)):
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer


def _config_note(change: CustomerConfigChange) -> str:
    """Plain-language summary, so a screen rendering only the note is still honest.

    Same job as `app.api.admin._impact_note`: the note is the one thing a minimal
    client is guaranteed to render, so every caveat carried in a structured field is
    also stated in prose here.
    """
    if change.unchanged:
        return (
            f"No change made: {change.customer_name} is already mapped to "
            f"{change.business_unit_name_after or 'no Business Unit'} with allocation "
            f"policy {change.allocation_policy_after}. Nothing was written and no "
            "coverage was recomputed."
        )

    parts: list[str] = []
    if change.business_unit_changed:
        parts.append(
            f"{change.customer_name} moved from Business Unit "
            f"{change.business_unit_name_before or 'NONE (unmapped)'} to "
            f"{change.business_unit_name_after or 'NONE'}. That changes the INVENTORY "
            "POOL its coverage is computed from entirely -- a Business Unit is an "
            "absolute boundary, and InventoryOnHand, InventoryOnOrder, the "
            "InventoryAssignment netting scope and the cross-customer sharing what-if "
            "all key on it. Assignments and on-hand rows do NOT travel with the "
            "customer."
        )
    if change.allocation_policy_changed:
        parts.append(
            f"Allocation policy changed {change.allocation_policy_before} -> "
            f"{change.allocation_policy_after}. That rewrites how this customer's whole "
            "coverage is decided rather than how much steel it has: 'soft' pools the "
            "BU's unassigned on-hand stock earliest-ROS-first, 'hard' grants coverage "
            "ONLY from inventory physically assigned to each demand line (no pool "
            "top-up, however much is on the shelf), and 'hybrid' draws the assignment "
            "first and the pool for the remainder."
        )
    parts.append(
        "Coverage stores its verdict, so this customer was recomputed "
        f"{change.recomputes_performed} time(s) in this same request "
        f"({change.wells_examined} well(s) visited) -- once even when both fields "
        "moved, because they feed the same pass and two passes would briefly store an "
        "intermediate state nobody asked for. No OTHER customer was recomputed and none "
        "needed to be: recompute_customer pools one customer's wells and stops there, "
        "so a neighbour's stored verdict cannot move because this one changed pool."
    )
    if change.well_changes:
        parts.append(
            f"{len(change.well_changes)} of {change.wells_examined} well(s) changed "
            "coverage rollup: "
            + ", ".join(
                f"{w.well_name} {w.coverage_before or 'not evaluated'} -> "
                f"{w.coverage_after or 'not evaluated'}"
                for w in change.well_changes
            )
            + "."
        )
    else:
        parts.append(
            "No well's coverage rollup moved, but every line-level verdict was "
            "re-derived."
        )
    if not change.coverage_resolvable_before:
        parts.append(
            "NOTE: this customer's coverage could NOT be resolved BEFORE this change "
            "either, so the before/after above starts from wells that had no computed "
            "verdict."
        )
    if change.unresolved_reason is not None:
        parts.append(
            "CAVEAT: coverage STILL cannot be resolved for this customer, so its stored "
            "verdicts were rolled back to exactly what they were and now predate this "
            "change. The allocation policy was changed anyway, and deliberately: the "
            "products a coverage pass must resolve are identical under all three "
            "policies, so the policy cannot be the cause -- and refusing would leave "
            "the field permanently uneditable for exactly the customers whose "
            f"configuration most needs fixing. {change.unresolved_reason}"
        )
    return " ".join(parts)


@router.patch("/customers/{customer_id}", response_model=CustomerConfigChangeOut)
def update_customer_configuration(
    customer_id: str, payload: CustomerConfigPatch, db: Session = Depends(get_db)
):
    """Change a customer's Business Unit and/or its allocation policy. ONE endpoint.

    Both fields in one PATCH is a decision, not a convenience. They feed the SAME
    coverage pass -- the BU decides which quantities are resolved, the policy decides
    how they are divided -- so a combined change is recomputed ONCE, after both
    mutations. Two endpoints would recompute twice and briefly store an intermediate
    state (the new BU under the old policy) that nobody asked for. The response's
    `recomputes_performed` makes the claim checkable.

    THREE REFUSALS, each with a status code that describes what is actually wrong:

      * 400 -- an unknown `allocation_policy` value, or a `business_unit_id` that
        matches no Business Unit. The request is well formed and the customer exists;
        what is wrong is a VALUE in the body, and the message names the valid ones
        rather than letting Pydantic emit a generic 422 or a foreign key surface as a
        500. Same stance as `app.api.admin._parse_dimension`.
      * 409 -- un-mapping a customer that HAS a Business Unit (`business_unit_id:
        null`). The state of the stored record is what blocks it, which is what 409
        means. An unmapped customer has no inventory pool at all, so every coverage
        read for it fails and NOTHING anybody could load would fix that. See
        `app.engines.customer_admin`.
      * 424 -- a remap whose destination Business Unit cannot resolve this customer's
        demand. On-hand quantity is an Oracle-owned projection and the destination's
        rows have not arrived, so the platform cannot answer because something it
        DEPENDS ON is absent -- the same reasoning `app.main` applies to
        `InventoryRowMissing`. THE REMAP IS ROLLED BACK, and the refusal names the
        products whose rows are missing so the operator can have the feed loaded and
        retry.

    An EMPTY body is a 400 rather than a no-op, matching
    `update_lead_time_component`: a client bug that drops the body must not report
    success for a change it never sent. A PATCH that sends the values already in force
    IS a legal no-op and comes back with `unchanged: true`, nothing written and nothing
    recomputed.
    """
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    fields_set = payload.model_fields_set
    if not fields_set:
        raise HTTPException(
            status_code=400,
            detail=(
                "nothing to update: send `business_unit_id`, `allocation_policy`, or "
                "both. An empty PATCH is refused rather than treated as a no-op so a "
                "client bug that drops the body cannot report success for a change it "
                "never sent. Sending the values already in force IS accepted and "
                "answers `unchanged: true`."
            ),
        )

    policy = UNSET
    if "allocation_policy" in fields_set:
        if payload.allocation_policy is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "allocation_policy cannot be null. The column is NOT NULL and every "
                    "customer's coverage is judged under exactly one of soft / hard / "
                    "hybrid -- there is no 'no policy' rule for the allocation engine to "
                    "apply, so a null would leave the pass with nothing to compute. "
                    "Nothing was saved."
                ),
            )
        try:
            policy = parse_allocation_policy(payload.allocation_policy)
        except ValueError as exc:
            # 400, not 422: the request is well formed and the resource exists; what is
            # wrong is the VALUE, and the message names the three permitted ones and
            # says what each does to the verdict.
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    business_unit_id = UNSET
    if "business_unit_id" in fields_set:
        business_unit_id = payload.business_unit_id
        if business_unit_id is not None:
            business_unit_id = business_unit_id.strip()
            if db.get(BusinessUnit, business_unit_id) is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"business_unit_id {business_unit_id!r} is not a Business Unit "
                        "on this platform. Pick one from GET /business-units, or create "
                        "one with POST /business-units. An id matching no BU would map "
                        "the customer onto an inventory pool that does not exist, and "
                        "since on-hand quantity is keyed on (Business Unit, product) "
                        "every coverage read for the customer would then fail. Nothing "
                        "was saved."
                    ),
                )

    try:
        change = apply_customer_config_change(
            db,
            customer,
            business_unit_id=business_unit_id,
            allocation_policy=policy,
        )
    except CustomerConfigRefused as exc:
        # The engine already put the customer's row back in this session; rolling the
        # transaction back is what makes that permanent. Both statuses are documented
        # above, and `detail` is a dict so a client can highlight the missing products
        # without parsing prose -- the same shape the demand-import conflict refusal
        # uses, and `app.api.client.ApiError` already unwraps it.
        db.rollback()
        raise HTTPException(
            status_code=409 if exc.reason == "unmap_refused" else 424,
            detail={
                "error": exc.reason,
                "detail": str(exc),
                "business_unit_id": exc.destination_business_unit_id,
                "missing_product_ids": list(exc.missing_product_ids),
            },
        ) from exc

    db.commit()
    db.refresh(customer)

    return CustomerConfigChangeOut(
        customer=CustomerOut(
            id=customer.id,
            name=customer.name,
            business_unit_id=customer.business_unit_id,
            allocation_policy=customer.allocation_policy,
        ),
        unchanged=change.unchanged,
        business_unit_changed=change.business_unit_changed,
        business_unit_id_before=change.business_unit_id_before,
        business_unit_id_after=change.business_unit_id_after,
        business_unit_name_before=change.business_unit_name_before,
        business_unit_name_after=change.business_unit_name_after,
        allocation_policy_changed=change.allocation_policy_changed,
        allocation_policy_before=change.allocation_policy_before,
        allocation_policy_after=change.allocation_policy_after,
        wells_examined=change.wells_examined,
        well_changes=[
            WellCoverageRollupChangeOut(
                well_id=w.well_id,
                well_name=w.well_name,
                coverage_before=w.coverage_before,
                coverage_after=w.coverage_after,
            )
            for w in change.well_changes
        ],
        recomputes_performed=change.recomputes_performed,
        neighbour_recompute_failures=list(change.neighbour_recompute_failures),
        coverage_resolvable_before=change.coverage_resolvable_before,
        unresolved_reason=change.unresolved_reason,
        note=_config_note(change),
    )
