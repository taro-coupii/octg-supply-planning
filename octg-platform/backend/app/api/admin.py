"""Administration endpoints -- the platform's ADJUSTABLE assumptions.

    GET    /admin/lead-time-components          list every additive lead-time term
    POST   /admin/lead-time-components          create one
    PATCH  /admin/lead-time-components/{id}     change its months / label
    DELETE /admin/lead-time-components/{id}     remove it
    GET    /admin/coverage-scope-defaults       the scope coverage is evaluated under
    PUT    /admin/coverage-scope-defaults       change it, platform-wide
    GET    /admin/technical-substitutions       layer 1: engineering compatibility
    POST   /admin/technical-substitutions       register one
    DELETE /admin/technical-substitutions/{id}  withdraw it, reporting what relied on it
    GET    /admin/customer-substitution-rules   layer 2: whether a customer permits a pair
    POST   /admin/customer-substitution-rules   record one
    PATCH  /admin/customer-substitution-rules/{id}   flip `allowed`
    DELETE /admin/customer-substitution-rules/{id}   remove it, reverting to blocked

WHY THESE ARE WRITEABLE WHEN MOST OF THIS API IS NOT
====================================================
The Administration screen was read-only, and correctly so: a Business Unit, a
customer and an allocation policy are maintained upstream, and edit controls that
could not save would be worse than none. `InventoryOnHand`, `InventoryOnOrder` and
`InventoryAssignment` expose no mutation anywhere for a stronger reason still -- they
are read-only projections of Oracle-owned data, and a platform that let a planner
edit its own copy would be claiming ownership of a fact it does not own.

Both settings here are the opposite case, and they are the same case
`app.api.customer_owned_inventory` makes for itself: THIS PLATFORM OWNS THEM.

  Lead-time components are the platform's own modelling assumptions -- how many months
  an OD/WT, a grade family, a connection and the sailing leg each contribute. Oracle
  holds no such table. They arrived by being typed into a seed script, which is not a
  system of record, and the planner who knows the mill queue has lengthened is the
  person who should be able to say so.

  The coverage scope default is a statement about how THIS platform evaluates demand.
  Nothing upstream has an opinion about it; it was a Python constant, editable only by
  a deploy.

  The two substitution master-data tables are the same case again. A technical
  substitution is an ENGINEERING COMPATIBILITY CLAIM and a customer substitution rule
  is a COMMERCIAL AGREEMENT; Oracle holds neither, and both arrived by being typed
  into a seed script. A planner asked where a substitution item could be registered
  and the answer was nowhere.

CONSEQUENTIAL, AND PERFORMED ANYWAY
===================================
Every write here can move a coverage verdict, and the deletes can retract the
platform's one terminal claim (`CoverageStatus.UNRECOVERABLE`). None of them refuses,
warns-then-requires-a-second-call, or hides behind a confirmation flag the server
cannot verify. They perform the change and REPORT the blast radius, which is the
established pattern of this codebase:

  * `PUT /wells/{well_id}/demand-status` returns status_before / status_after and
    coverage_before / coverage_after, plus a revision and impact id per affected line,
    so a caller can prove the fan-out happened.
  * The customer-owned inventory upload returns `coverage_changes` for every well
    whose rollup moved -- including wells the uploaded file never mentioned.

So does every write below. Stored derived data is repaired in the same transaction --
see `app.engines.lead_time_admin` and `app.engines.coverage_scope` for why deferring
was rejected, and for the measured cost of not deferring.

ONE REFINEMENT, FOUND BY RUNNING IT
===================================
The substitution endpoints report `coverage_recompute_failures`. A customer whose
inventory facts are incomplete cannot be recomputed at all, because
`app.engines.inventory` refuses to invent an on-hand quantity -- and a technical
substitution is BU-agnostic while the recompute is BU-scoped, so registering a pair can
require a row some Business Unit never received. The write PROCEEDS and names the
customers whose stored verdicts therefore still predate it. One warehouse's missing feed
must not veto an engineering claim or a commercial agreement that applies to every BU,
and the alternative -- substituting a zero -- is the invention this platform exists not
to make. See `app.engines.substitution_admin`.


REFUSALS STATE A REASON, NOT A SCHEMA VIOLATION
==============================================
`dimension`, `status_filter` and `profile_filter` all arrive as plain strings rather
than as enums, so the 400 can name the valid values and say WHY the set is closed
instead of emitting Pydantic's generic 422. That is the same stance
`app.engines.inventory` takes when it refuses to invent an on-hand quantity and the
same one `app.main`'s 409/424 handlers take: the response tells the operator what to
do next.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.engines.coverage_scope import (
    DEFAULT_PROFILE_FILTER,
    DEFAULT_STATUS_FILTER,
    InvalidCoverageScope,
    coverage_scope,
    parse_profile_filter,
    parse_status_filter,
    set_coverage_scope_defaults,
)
from app.engines.lead_time import REQUIRED_DIMENSIONS
from app.engines.lead_time_admin import LeadTimeChangeImpact, apply_lead_time_change
from app.engines.substitution_admin import (
    SubstitutionChangeImpact,
    apply_substitution_change,
    pair_dependencies,
    technical_substitution_exists,
)
from app.models import (
    ANY_ATTRIBUTE_VALUE,
    Customer,
    CustomerSubstitutionRule,
    DemandProfile,
    DemandStatus,
    LeadTimeComponent,
    LeadTimeDimension,
    Product,
    TechnicalSubstitution,
    SafetyStock,
)
from app.schemas import (
    CoverageScopeDefaultsChangeOut,
    CoverageScopeDefaultsIn,
    CoverageScopeDefaultsOut,
    CoverageRecomputeFailureOut,
    CustomerSubstitutionRuleChangeOut,
    CustomerSubstitutionRuleIn,
    CustomerSubstitutionRuleOut,
    CustomerSubstitutionRulePatch,
    CustomerSubstitutionRulesOut,
    LeadTimeComponentChangeOut,
    LeadTimeComponentIn,
    LeadTimeComponentOut,
    LeadTimeComponentPatch,
    LeadTimeComponentsOut,
    ProductLeadTimeChangeOut,
    TechnicalSubstitutionChangeOut,
    TechnicalSubstitutionIn,
    TechnicalSubstitutionOut,
    TechnicalSubstitutionsOut,
    WellCoverageRollupChangeOut,
    SafetyStockIn,
    SafetyStockListOut,
    SafetyStockRowOut,
)

router = APIRouter(prefix="/admin", tags=["admin"])

#: The four dimensions, in the order `app.engines.lead_time` renders a breakdown.
#: Taken from `REQUIRED_DIMENSIONS` rather than from `LeadTimeDimension` directly so
#: the vocabulary this API advertises and the set the resolver REQUIRES can never
#: disagree -- a dimension the resolver demanded but this endpoint did not offer would
#: leave every product unmodelled with no way to fix it from the screen.
DIMENSION_VALUES = [dimension.value for dimension in REQUIRED_DIMENSIONS]


# ---------------------------------------------------------------------------
# Lead-time components
# ---------------------------------------------------------------------------


def _component_out(row: LeadTimeComponent) -> LeadTimeComponentOut:
    """Serialise one component, deriving `shared` from the wildcard sentinel."""
    dimension = row.dimension
    # Defensive, matching `app.engines.lead_time._rows_by_dimension`: a legacy row can
    # hold a raw string rather than an enum member.
    value = dimension.value if isinstance(dimension, LeadTimeDimension) else str(dimension)
    return LeadTimeComponentOut(
        id=row.id,
        dimension=value,
        attribute_value=row.attribute_value,
        months=float(row.months),
        label=row.label,
        # The ONE place the wildcard is turned into a boolean for a client. The admin
        # table and the lead-time breakdown therefore agree by construction about
        # which rows are shared.
        shared=(row.attribute_value or "").strip() == ANY_ATTRIBUTE_VALUE,
    )


def _parse_dimension(raw: str) -> LeadTimeDimension:
    """`raw` as a `LeadTimeDimension`, or a 400 that explains the closed set.

    Deliberately not delegated to Pydantic. The set of dimensions is a MODELLING
    decision -- the spec names four and the resolver requires all four -- and a typo'd
    free-text dimension used to silently create an additive term nobody could find
    (see `app.models.lead_time`). The refusal therefore states the vocabulary and the
    reason, so an operator who sends "Logistic" is told what to send instead.
    """
    text = (raw or "").strip()
    by_value = {dimension.value: dimension for dimension in LeadTimeDimension}
    if text in by_value:
        return by_value[text]
    by_lower = {value.lower(): member for value, member in by_value.items()}
    if text.lower() in by_lower:
        return by_lower[text.lower()]
    raise HTTPException(
        status_code=400,
        detail=(
            f"{raw!r} is not a lead-time dimension. It must be one of "
            f"{DIMENSION_VALUES}. The set is CLOSED on purpose: a lead time is built "
            "from exactly these four attribute terms, and free-text dimensions would "
            "add a term the resolver never asks for -- silently contributing months "
            "that no product's breakdown could explain. Nothing was saved."
        ),
    )


def _conflict_if_pair_exists(
    db: Session,
    dimension: LeadTimeDimension,
    attribute_value: str,
    exclude_id: str | None = None,
) -> None:
    """409 when (dimension, attribute_value) is already taken.

    Checked BEFORE the insert so the client gets a sentence rather than a driver
    message. The unique constraint is still the authority -- see `_integrity_conflict`
    for the backstop -- but a raw `IntegrityError` reaching a client leaks the
    constraint name and SQL, and says nothing about why two rows for one pair are
    forbidden.

    Why they are forbidden is worth the endpoint stating: `resolve_lead_time` raises
    `AmbiguousLeadTimeComponent` rather than picking a winner, because a silently
    chosen term would be invisible in the published breakdown -- and the breakdown is
    the entire reason planners trust the number.
    """
    query = db.query(LeadTimeComponent).filter(
        LeadTimeComponent.dimension == dimension,
        LeadTimeComponent.attribute_value == attribute_value,
    )
    if exclude_id is not None:
        query = query.filter(LeadTimeComponent.id != exclude_id)
    existing = query.first()
    if existing is None:
        return
    shared_note = (
        " That row is the WILDCARD for this dimension -- it already applies to every "
        "product that has no more specific row, so a second one would be ambiguous "
        "for all of them."
        if attribute_value == ANY_ATTRIBUTE_VALUE
        else ""
    )
    raise HTTPException(
        status_code=409,
        detail=(
            f"a lead-time component for dimension {dimension.value!r} and attribute "
            f"value {attribute_value!r} already exists (id {existing.id}, "
            f"{float(existing.months):g} months"
            + (f", labelled {existing.label!r}" if existing.label else "")
            + f").{shared_note} Two rows for one (dimension, attribute value) pair "
            "make the lead time unexplainable, so the resolver refuses to total them "
            "at all rather than silently pick one. Edit the existing row's months "
            f"(PATCH /admin/lead-time-components/{existing.id}) or delete it first. "
            "Nothing was saved."
        ),
    )


def _integrity_conflict(db: Session, exc: IntegrityError) -> HTTPException:
    """Turn a unique-constraint `IntegrityError` into the same clean 409.

    The pre-check above handles every ordinary case. This is the backstop for the one
    it cannot: two concurrent requests both passing the check and both inserting. Its
    existence is what makes the guarantee "a client never sees a raw database error"
    true rather than merely usually true.
    """
    db.rollback()
    text = str(getattr(exc, "orig", exc))
    if "uq_lead_time_component_dimension_value" in text or "UNIQUE" in text.upper():
        return HTTPException(
            status_code=409,
            detail=(
                "a lead-time component for that (dimension, attribute value) pair "
                "already exists -- it was created by another request while this one "
                "was in flight. Two rows for one pair make the lead time "
                "unexplainable, so the insert was refused. Re-read "
                "GET /admin/lead-time-components and edit the existing row instead. "
                "Nothing was saved."
            ),
        )
    # Not the constraint we know about. Still refused, still explained, and still not
    # a traceback -- but not mislabelled as a duplicate either.
    return HTTPException(
        status_code=400,
        detail=(
            "the database refused this lead-time component and the reason is not the "
            f"(dimension, attribute value) uniqueness rule: {text}. Nothing was saved."
        ),
    )


def _substitution_integrity_conflict(
    db: Session, exc: IntegrityError, what: str
) -> HTTPException:
    """Backstop for the substitution tables' uniqueness constraints.

    Same job and same reasoning as `_integrity_conflict` above: the pre-checks in the
    substitution endpoints handle every ordinary case, and this covers the one they
    cannot -- two concurrent requests both passing the check and both inserting. Its
    existence is what makes "a client never sees a raw database error" true rather than
    merely usually true, and it is why the two unique constraints were added to
    `app.models.substitution` at all (see `d5f2a4b91c70`); without a constraint there
    would be nothing to catch and the duplicate would simply be stored.
    """
    db.rollback()
    text = str(getattr(exc, "orig", exc))
    if (
        "uq_technical_substitution_pair" in text
        or "uq_customer_substitution_rule_pair" in text
        or "UNIQUE" in text.upper()
    ):
        return HTTPException(
            status_code=409,
            detail=(
                f"that {what} already exists -- it was created by another request while "
                "this one was in flight. A duplicate would make the substitution "
                "candidate list ambiguous (the same substitute offered twice, or two "
                "rows disagreeing about one customer's permission), so the insert was "
                "refused. Re-read the list and edit the existing row instead. Nothing "
                "was saved."
            ),
        )
    return HTTPException(
        status_code=400,
        detail=(
            f"the database refused this {what} and the reason is not the uniqueness "
            f"rule: {text}. Nothing was saved."
        ),
    )


def _impact_note(action: str, impact: LeadTimeChangeImpact) -> str:
    """Plain-language summary, so a screen rendering only the note is still honest."""
    parts = [
        f"Component {action}. Every lead time is resolved live from this table on each "
        f"read, so the change is already visible everywhere ({impact.products_examined} "
        "catalogue product(s) re-resolved)."
    ]
    unmodelled = [c for c in impact.product_changes if c.became_unmodelled]
    modelled = [c for c in impact.product_changes if c.became_modelled]
    moved = [
        c
        for c in impact.product_changes
        if not c.became_unmodelled and not c.became_modelled
    ]
    if unmodelled:
        parts.append(
            f"{len(unmodelled)} product(s) are now NOT MODELLED (their component set "
            "is incomplete, so the total is reported as 0/'not modelled' rather than "
            "summed): "
            + ", ".join(
                f"{c.product_description or c.product_id} "
                f"(missing {', '.join(c.missing_dimensions_after)})"
                for c in unmodelled
            )
            + ". An unmodelled product is never judged Unrecoverable, because absent "
            "data means 'cannot judge', not 'hopeless' -- so any Unrecoverable verdict "
            "resting on it has been retracted."
        )
    if modelled:
        parts.append(
            f"{len(modelled)} product(s) became MODELLED for the first time, so they "
            "now have a real order date and can be judged Unrecoverable: "
            + ", ".join(c.product_description or c.product_id for c in modelled)
            + "."
        )
    if moved:
        parts.append(
            f"{len(moved)} product(s) kept a modelled lead time with a different total: "
            + ", ".join(
                f"{c.product_description or c.product_id} "
                f"{c.total_months_before:g} -> {c.total_months_after:g} mo"
                for c in moved
            )
            + "."
        )
    if not impact.product_changes:
        parts.append("No product's resolved lead time moved.")
    parts.append(
        "Coverage stores its Uncovered/Unrecoverable choice, so it was recomputed for "
        f"{len(impact.recomputed_customer_ids)} customer(s) in this same request -- a "
        "stored verdict computed from a superseded assumption would be misinformation, "
        "not merely stale."
    )
    if impact.well_changes:
        parts.append(
            f"{len(impact.well_changes)} well(s) changed coverage rollup: "
            + ", ".join(
                f"{w.well_name} {w.coverage_before or 'not evaluated'} -> "
                f"{w.coverage_after or 'not evaluated'}"
                for w in impact.well_changes
            )
            + "."
        )
    else:
        parts.append("No well's coverage rollup moved.")
    return " ".join(parts)


def _change_out(
    action: str,
    component: LeadTimeComponent | None,
    impact: LeadTimeChangeImpact,
) -> LeadTimeComponentChangeOut:
    return LeadTimeComponentChangeOut(
        action=action,
        component=_component_out(component) if component is not None else None,
        products_examined=impact.products_examined,
        product_changes=[
            ProductLeadTimeChangeOut(
                product_id=c.product_id,
                product_description=c.product_description,
                total_months_before=c.total_months_before,
                total_months_after=c.total_months_after,
                modelled_before=c.modelled_before,
                modelled_after=c.modelled_after,
                missing_dimensions_after=list(c.missing_dimensions_after),
                became_unmodelled=c.became_unmodelled,
                became_modelled=c.became_modelled,
            )
            for c in impact.product_changes
        ],
        recomputed_customer_ids=list(impact.recomputed_customer_ids),
        well_changes=[
            WellCoverageRollupChangeOut(
                well_id=w.well_id,
                well_name=w.well_name,
                coverage_before=w.coverage_before,
                coverage_after=w.coverage_after,
            )
            for w in impact.well_changes
        ],
        note=_impact_note(action, impact),
    )


@router.get("/lead-time-components", response_model=LeadTimeComponentsOut)
def list_lead_time_components(db: Session = Depends(get_db)):
    """Every additive lead-time term, grouped-ready and with the vocabulary to add one.

    Sorted by dimension in the order a breakdown is rendered, then wildcard rows
    FIRST within each dimension, then by attribute value. The wildcard leads because it
    is the dimension's default and every specific row below it is an exception to it --
    listing it alphabetically among the others would bury the row that explains the
    rest.

    `dimensions_with_no_rows` exists because the table's most dangerous state looks
    healthy. All four dimensions are required for any product to be modelled, so a
    complete-looking set of OD/WT, Grade and Connection rows with no Logistics row at
    all means EVERY product on the platform reports "not modelled" -- and no amount of
    reading the rows present reveals the one that is absent.
    """
    rows = db.query(LeadTimeComponent).all()
    components = [_component_out(row) for row in rows]
    order = {value: index for index, value in enumerate(DIMENSION_VALUES)}
    components.sort(
        key=lambda c: (
            # An unrecognised dimension sorts last rather than crashing -- it is
            # visible data, and hiding it would hide a row the resolver may be raising
            # about.
            order.get(c.dimension, len(order)),
            0 if c.shared else 1,
            c.attribute_value,
        )
    )

    present = {c.dimension for c in components}
    missing = [value for value in DIMENSION_VALUES if value not in present]
    note = None
    if missing:
        note = (
            "NO product on this platform has a modelled lead time: all four dimensions "
            f"are required and {', '.join(missing)} has no row at all. Every product "
            "therefore resolves to 'not modelled' (total 0, deliberately not a partial "
            "sum), no order date is recommended for any of them, and none can be "
            "judged Unrecoverable. Add a row for each missing dimension -- a wildcard "
            f"({ANY_ATTRIBUTE_VALUE}) row is enough if one allowance applies to "
            "everything."
        )
    return LeadTimeComponentsOut(
        components=components,
        dimensions=DIMENSION_VALUES,
        wildcard=ANY_ATTRIBUTE_VALUE,
        dimensions_with_no_rows=missing,
        incomplete_note=note,
    )


@router.post(
    "/lead-time-components", response_model=LeadTimeComponentChangeOut, status_code=201
)
def create_lead_time_component(
    payload: LeadTimeComponentIn, db: Session = Depends(get_db)
):
    """Add one additive lead-time term.

    Creating a row can be as consequential as deleting one: supplying the last missing
    dimension makes a whole family of products modelled for the first time, which gives
    them a real order date AND makes them eligible for the terminal `Unrecoverable`
    verdict they were previously exempt from. The response reports that in
    `product_changes` / `became_modelled`.

    `months` of 0 is accepted and is NOT the same as omitting the row. The spec's own
    worked example has Connection at +0 months, so a zero is a real answer -- "this
    dimension adds nothing" -- while an absent row means "not modelled". It is a row's
    PRESENCE, never its value, that makes a dimension modelled.
    """
    dimension = _parse_dimension(payload.dimension)
    attribute_value = (payload.attribute_value or "").strip()
    if not attribute_value:
        raise HTTPException(
            status_code=400,
            detail=(
                "attribute_value is required. It is the value this term applies to on "
                f"its dimension (for example {'4-1/2 12.6'!r} for OD/WT, or "
                f"{'Carbon'!r} for Grade), or {ANY_ATTRIBUTE_VALUE!r} for a row that "
                "applies to every product on that dimension. An empty value would "
                "match no product and no wildcard, so the row would contribute "
                "nothing while looking like configuration. Nothing was saved."
            ),
        )
    if payload.months < 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"months must not be negative (got {payload.months:g}). A negative "
                "term would shorten a total lead time, producing a recommended order "
                "date LATER than physics allows and an is_recoverable verdict that "
                "says a line can be saved when it cannot. Nothing was saved."
            ),
        )
    _conflict_if_pair_exists(db, dimension, attribute_value)

    row = LeadTimeComponent(
        dimension=dimension,
        attribute_value=attribute_value,
        months=float(payload.months),
        label=(payload.label or "").strip() or None,
    )

    try:
        impact = apply_lead_time_change(db, lambda: db.add(row))
    except IntegrityError as exc:
        raise _integrity_conflict(db, exc) from exc
    db.commit()
    return _change_out("created", row, impact)


@router.patch(
    "/lead-time-components/{component_id}", response_model=LeadTimeComponentChangeOut
)
def update_lead_time_component(
    component_id: str, payload: LeadTimeComponentPatch, db: Session = Depends(get_db)
):
    """Change a component's MONTHS and/or LABEL.

    `dimension` and `attribute_value` cannot be changed here, and
    `app.schemas.LeadTimeComponentPatch` documents why at length: that pair is the
    row's identity, so changing it is a delete plus a create, and the honest way to
    perform a delete is with DELETE -- which reports what it broke.

    `label` is presentation only. It is never matched on and never affects arithmetic,
    so editing it cannot move a verdict; the response's `product_changes` will
    correctly be empty. `months` is the opposite: it feeds the total, the recommended
    order date and the `is_recoverable` verdict, so its blast radius is reported in
    full.
    """
    row = db.get(LeadTimeComponent, component_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Lead-time component {component_id!r} not found"
        )

    fields_set = payload.model_fields_set
    if not fields_set:
        raise HTTPException(
            status_code=400,
            detail=(
                "nothing to update: send `months`, `label`, or both. An empty PATCH is "
                "refused rather than treated as a no-op so a client bug that drops the "
                "body cannot report success for a change it never sent. To change the "
                "dimension or the attribute value, DELETE this component and POST a "
                "new one -- that pair is the row's identity, and re-pointing it at "
                "different products is not an edit."
            ),
        )
    if "months" in fields_set and payload.months is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "months cannot be null. The column is NOT NULL and an absent term is "
                "expressed by DELETING the row, not by blanking its value -- 0 months "
                "is a real, deliberate answer ('this dimension adds nothing') and must "
                "stay distinguishable from 'not modelled'."
            ),
        )
    if payload.months is not None and payload.months < 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"months must not be negative (got {payload.months:g}). A negative "
                "term would shorten a total lead time, producing a recommended order "
                "date LATER than physics allows and an is_recoverable verdict that "
                "says a line can be saved when it cannot. Nothing was saved."
            ),
        )

    def mutate() -> None:
        if payload.months is not None:
            row.months = float(payload.months)
        if "label" in fields_set:
            # An explicit null CLEARS the label, which is a legitimate edit -- the
            # column is nullable and a planner may want the dimension to speak for
            # itself. Distinguished from "not sent" by `model_fields_set`, so omitting
            # `label` while changing `months` does not silently erase it.
            row.label = (payload.label or "").strip() or None

    impact = apply_lead_time_change(db, mutate)
    db.commit()
    return _change_out("updated", row, impact)


@router.delete(
    "/lead-time-components/{component_id}", response_model=LeadTimeComponentChangeOut
)
def delete_lead_time_component(component_id: str, db: Session = Depends(get_db)):
    """Remove one additive lead-time term. IT PROCEEDS, and it reports what it broke.

    This is the most consequential endpoint on the screen, and it deliberately does not
    warn-then-require-confirmation. "Adjustable" means a real administrative control;
    a two-phase confirmation would be the server inventing a workflow it cannot
    enforce (nothing stops a client sending the confirmed call first), and this
    codebase has an established answer for a consequential-but-legitimate change:
    perform it and report before/after. `PUT /wells/{well_id}/demand-status` does
    exactly this, and the customer-owned upload reports `coverage_changes` the same
    way. The frontend confirmation dialog is a UI courtesy on top, not the safety
    mechanism.

    What it can break, stated plainly, because the response says so too:

      * Removing the LAST row a product matches on some dimension makes that product's
        component set INCOMPLETE, so it resolves to "not modelled" (total 0, never a
        partial sum) and its recommended order date disappears.
      * An unmodelled product can never be judged `Unrecoverable` --
        `app.engines.order_dates.is_recoverable` treats absent data as "cannot judge",
        not "hopeless" -- so deleting a row can RETRACT the platform's one terminal
        claim and quietly turn an Unrecoverable well into a merely Uncovered one.
      * Removing a WILDCARD row does this to every product that had no more specific
        row on that dimension, which is usually most of the catalogue.

    All three are reported in `product_changes` (with `became_unmodelled` flagged) and
    `well_changes`, and coverage is recomputed in this same request so no stored
    verdict outlives the assumption it was computed from.
    """
    row = db.get(LeadTimeComponent, component_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Lead-time component {component_id!r} not found"
        )
    # Serialised BEFORE the delete: after it the instance is expunged and its
    # attributes are gone, and the note below names the row that was removed.
    removed = _component_out(row)

    impact = apply_lead_time_change(db, lambda: db.delete(row))
    db.commit()

    out = _change_out("deleted", None, impact)
    out.note = (
        f"Deleted the {removed.dimension} term for "
        + (
            f"the wildcard {ANY_ATTRIBUTE_VALUE!r} (it applied to every product with no "
            "more specific row on this dimension)"
            if removed.shared
            else f"attribute value {removed.attribute_value!r}"
        )
        + f", {removed.months:g} months"
        + (f", labelled {removed.label!r}" if removed.label else "")
        + ". "
        + out.note
    )
    return out


# ---------------------------------------------------------------------------
# Coverage scope defaults
# ---------------------------------------------------------------------------

#: Stated on both the GET and the PUT, because it is the fact that makes this setting
#: different in kind from the Coverage Workspace's filter toggles.
_BLAST_RADIUS = (
    "This is a PLATFORM-WIDE setting, not a per-customer one and not a display "
    "filter. The two filters decide which demand lines COMPETE for the same steel, so "
    "changing them moves coverage verdicts for lines that were in scope all along -- "
    "for every customer in every Business Unit, at once. Saving recomputes every "
    "customer immediately so no stored verdict is left describing the old scope."
)


def _scope_out(db: Session) -> CoverageScopeDefaultsOut:
    scope = coverage_scope(db)
    status_values = sorted(s.value for s in scope.status_filter)
    profile_values = sorted(p.value for p in scope.profile_filter)
    if scope.persisted:
        provenance = (
            "This scope was set from this screen"
            + (
                f" on {scope.updated_at.isoformat()}"
                if scope.updated_at is not None
                else ""
            )
            + "."
            + (
                " It happens to match the values the platform ships with."
                if scope.matches_shipped_default
                else ""
            )
        )
    else:
        provenance = (
            "Nobody has adjusted the scope, so the platform is running on the values "
            "it ships with. No setting row exists -- these are not a stored copy of "
            "the shipped values, they ARE the shipped values."
        )
    return CoverageScopeDefaultsOut(
        status_filter=status_values,
        profile_filter=profile_values,
        available_statuses=[s.value for s in DemandStatus],
        available_profiles=[p.value for p in DemandProfile],
        shipped_status_filter=sorted(s.value for s in DEFAULT_STATUS_FILTER),
        shipped_profile_filter=sorted(p.value for p in DEFAULT_PROFILE_FILTER),
        persisted=scope.persisted,
        updated_at=scope.updated_at,
        matches_shipped_default=scope.matches_shipped_default,
        note=(
            f"Demand status {', '.join(status_values)} selects whole WELLS (demand "
            "status lives on the well, so a status filter takes a well entirely or "
            f"leaves it out entirely). Profile {', '.join(profile_values)} selects "
            "LINES, cutting inside a well. Demand outside this scope is NOT EVALUATED "
            "-- a third state, distinct from covered and from uncovered. "
            f"{provenance} {_BLAST_RADIUS}"
        ),
    )


@router.get("/coverage-scope-defaults", response_model=CoverageScopeDefaultsOut)
def get_coverage_scope_defaults(db: Session = Depends(get_db)):
    """The scope the coverage engine is evaluating under right now.

    Reports the EFFECTIVE scope -- the persisted setting when one exists, the shipped
    constants otherwise -- and says which of the two it is served. Those are different
    facts and the values alone cannot distinguish them, because an administrator may
    save the shipped values back deliberately.
    """
    return _scope_out(db)


@router.put("/coverage-scope-defaults", response_model=CoverageScopeDefaultsChangeOut)
def put_coverage_scope_defaults(
    payload: CoverageScopeDefaultsIn, db: Session = Depends(get_db)
):
    """Change the platform-wide coverage scope, recomputing every customer.

    Both filters must be sent, and neither may be empty. An empty status filter is
    syntactically fine and catastrophic -- it puts every well out of scope, so every
    line on the platform becomes "not evaluated" and the coverage grid empties -- so it
    is refused with a stated reason rather than saved.

    Saving the scope already in effect is a no-op: `unchanged` comes back True and
    nothing is written or recomputed. Note that this is compared against the EFFECTIVE
    scope, so saving the shipped values on a platform nobody has adjusted correctly
    creates no row.

    The recompute is synchronous and full. See `app.engines.coverage_scope` for the
    measured cost and for why leaving stored `CoverageResult` rows to be repaired
    lazily was rejected.
    """
    try:
        status_filter = parse_status_filter(",".join(payload.status_filter))
        profile_filter = parse_profile_filter(",".join(payload.profile_filter))
        change = set_coverage_scope_defaults(db, status_filter, profile_filter)
    except InvalidCoverageScope as exc:
        # 400 rather than 422: the request is well formed and the resource exists;
        # what is wrong is the VALUE asked for, and the message names the permitted
        # ones. Same reasoning as `_parse_dimension`.
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()

    if change.unchanged:
        note = (
            "That scope was already in effect, so nothing was written and no coverage "
            "was recomputed. Demand status "
            f"{', '.join(change.status_filter_after)}, profile "
            f"{', '.join(change.profile_filter_after)}."
        )
    else:
        note = (
            "Coverage scope changed from demand status "
            f"{', '.join(change.status_filter_before)} / profile "
            f"{', '.join(change.profile_filter_before)} to demand status "
            f"{', '.join(change.status_filter_after)} / profile "
            f"{', '.join(change.profile_filter_after)}. Every customer was recomputed "
            f"in this request ({len(change.recomputed_customer_ids)} customer(s), "
            f"{change.recomputed_well_count} well(s) visited), so no stored verdict "
            "still describes the old scope. "
            + (
                f"{len(change.well_changes)} well(s) changed coverage rollup: "
                + ", ".join(
                    f"{w.well_name} {w.coverage_before or 'not evaluated'} -> "
                    f"{w.coverage_after or 'not evaluated'}"
                    for w in change.well_changes
                )
                + "."
                if change.well_changes
                else "No well's coverage rollup moved, but every line-level verdict "
                "was re-derived under the new scope."
            )
            + f" {_BLAST_RADIUS}"
        )

    return CoverageScopeDefaultsChangeOut(
        status_filter_before=list(change.status_filter_before),
        profile_filter_before=list(change.profile_filter_before),
        status_filter_after=list(change.status_filter_after),
        profile_filter_after=list(change.profile_filter_after),
        unchanged=change.unchanged,
        recomputed_synchronously=change.recomputed_synchronously,
        recomputed_customer_ids=list(change.recomputed_customer_ids),
        recomputed_well_count=change.recomputed_well_count,
        well_changes=[
            WellCoverageRollupChangeOut(
                well_id=w.well_id,
                well_name=w.well_name,
                coverage_before=w.coverage_before,
                coverage_after=w.coverage_after,
            )
            for w in change.well_changes
        ],
        note=note,
    )


# ---------------------------------------------------------------------------
# Substitution master data -- layer 1 (technical) and layer 2 (customer)
#
# WHY THESE TWO ARE WRITEABLE
# ===========================
# The same argument the lead-time components make, and it is the stronger case of the
# two. A `TechnicalSubstitution` is an ENGINEERING COMPATIBILITY CLAIM and a
# `CustomerSubstitutionRule` is a COMMERCIAL AGREEMENT. Oracle holds neither. They
# arrived by being typed into a seed script, which is not a system of record, and a
# planner asked outright where a substitution item could be registered -- the honest
# answer was nowhere.
#
# The third layer, `WellSubstitutionApproval`, is deliberately NOT administered here.
# It is not master data: it is one customer's decision about one demand line, and it
# already has its own endpoints in `app.api.substitution` that recompute the affected
# well. Adding a second way to write those rows from a settings screen would give the
# platform two front doors to the same decision.
#
# WHAT A REGISTRATION HERE DOES AND DOES NOT GRANT
# ================================================
# Stated on the payloads too, because it is the single most likely misreading of this
# screen: registering a technical substitution makes a product OFFERED as a candidate.
# It does not make it usable. `app.engines.substitution.find_candidates` requires all
# three layers, and layer 2 is a TRUE ALLOW-LIST -- verified by reading it, not
# assumed: `customer_allowed = bool(rule is not None and rule.allowed)`, so a pair with
# no rule is blocked exactly as an explicit veto is. That is why DELETE of a rule is a
# real state change and gets a consequence report rather than being treated as cleanup.
#
# CONSEQUENTIAL, AND PERFORMED ANYWAY
# ===================================
# Every write below can move a stored coverage verdict
# (`CoverageStatus.COVERED_VIA_SUBSTITUTE`, `CoverageStatus.PENDING_APPROVAL`), so
# every write recomputes coverage in the same transaction and reports which wells
# moved -- the pattern `PUT /wells/{id}/demand-status`, the customer-owned upload and
# the lead-time endpoints above all follow. See `app.engines.substitution_admin`.
# ---------------------------------------------------------------------------


def _product_or_400(db: Session, product_id: str, field: str) -> Product:
    """`product_id` as a Product, or a 400 naming which field was wrong.

    400 rather than 404: the resource identified by the PATH exists (or is being
    created), and what is wrong is a VALUE inside the body. Same stance as
    `_parse_dimension` -- the response tells the operator what to do next instead of
    letting a foreign-key IntegrityError surface as a 500.
    """
    product = db.get(Product, (product_id or "").strip())
    if product is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{field} {product_id!r} is not a product on this platform. Pick one "
                "from GET /products -- substitution is defined between two CATALOGUE "
                "entries, so an id that matches no product would create a row "
                "referencing nothing and no screen could ever render it. Nothing was "
                "saved."
            ),
        )
    return product


def _technical_out(db: Session, row: TechnicalSubstitution) -> TechnicalSubstitutionOut:
    """Serialise one layer-1 claim WITH descriptions and with what stands on it.

    Descriptions are joined here rather than left to the client because this API does
    not serve bare uuids where a description exists -- a planner cannot check a picker
    they cannot read. The counts come from `pair_dependencies`, the one place that
    question is answered, so the list and the delete report cannot disagree.
    """
    deps = pair_dependencies(db, row.from_product_id, row.to_product_id)
    from_product = db.get(Product, row.from_product_id)
    to_product = db.get(Product, row.to_product_id)
    return TechnicalSubstitutionOut(
        id=row.id,
        from_product_id=row.from_product_id,
        from_product_description=from_product.description if from_product else None,
        to_product_id=row.to_product_id,
        to_product_description=to_product.description if to_product else None,
        customer_rule_count=deps.customer_rule_count,
        allowing_customer_rule_count=deps.allowing_customer_rule_count,
        well_approval_count=deps.well_approval_count,
        approved_well_approval_count=deps.approved_well_approval_count,
    )


def _rule_out(db: Session, row: CustomerSubstitutionRule) -> CustomerSubstitutionRuleOut:
    """Serialise one layer-2 rule, flagging it when it currently gates nothing."""
    customer = db.get(Customer, row.customer_id)
    from_product = db.get(Product, row.from_product_id)
    to_product = db.get(Product, row.to_product_id)
    has_technical = technical_substitution_exists(
        db, row.from_product_id, row.to_product_id
    )
    return CustomerSubstitutionRuleOut(
        id=row.id,
        customer_id=row.customer_id,
        customer_name=customer.name if customer else None,
        from_product_id=row.from_product_id,
        from_product_description=from_product.description if from_product else None,
        to_product_id=row.to_product_id,
        to_product_description=to_product.description if to_product else None,
        allowed=bool(row.allowed),
        technical_substitution_exists=has_technical,
        inert_reason=(
            None
            if has_technical
            else (
                "No technical substitution is registered for this pair, so this rule "
                "has NO effect: the candidate list is built from the technical rows "
                "and a customer rule outside that set is never consulted. Register the "
                "technical substitution to make this permission live."
            )
        ),
    )


def _well_change_out(impact: SubstitutionChangeImpact) -> list[WellCoverageRollupChangeOut]:
    return [
        WellCoverageRollupChangeOut(
            well_id=w.well_id,
            well_name=w.well_name,
            coverage_before=w.coverage_before,
            coverage_after=w.coverage_after,
        )
        for w in impact.well_changes
    ]


def _recompute_failures_out(
    impact: SubstitutionChangeImpact,
) -> list[CoverageRecomputeFailureOut]:
    return [
        CoverageRecomputeFailureOut(
            customer_id=f.customer_id, customer_name=f.customer_name, reason=f.reason
        )
        for f in impact.recompute_failures
    ]


def _coverage_sentence(impact: SubstitutionChangeImpact) -> str:
    """The recompute half of every note below, worded identically in all four places.

    One function rather than four literals: the claim being made is that no stored
    verdict outlives the permission it was computed from, and four copies of that
    sentence is how one of them eventually stops being true.
    """
    parts = [
        "Coverage stores its CoveredViaSubstitute / PendingApproval choice, so it was "
        f"recomputed for {len(impact.recomputed_customer_ids)} customer(s) "
        f"({impact.wells_examined} well(s) visited) in this same request -- a stored "
        "verdict resting on a permission that no longer exists would be "
        "misinformation, not merely stale."
    ]
    if impact.well_changes:
        parts.append(
            f"{len(impact.well_changes)} well(s) changed coverage rollup: "
            + ", ".join(
                f"{w.well_name} {w.coverage_before or 'not evaluated'} -> "
                f"{w.coverage_after or 'not evaluated'}"
                for w in impact.well_changes
            )
            + "."
        )
    else:
        parts.append(
            "No well's coverage rollup moved, but every line-level verdict was "
            "re-derived."
        )
    if impact.recompute_failures:
        # Stated in the prose as well as in the structured field, because the note is
        # the one thing a minimal screen is guaranteed to render -- and this caveat
        # applies to every other number in it.
        parts.append(
            "CAVEAT: "
            f"{len(impact.recompute_failures)} customer(s) could NOT be recomputed, so "
            "their stored verdicts still predate this change and are excluded from the "
            "well changes above. Their inventory facts are incomplete and this platform "
            "refuses to invent an on-hand quantity; the master-data change was still "
            "made, because one Business Unit's missing inventory feed must not veto an "
            "engineering or commercial fact that applies to all of them. "
            + " ".join(
                f"{f.customer_name}: {f.reason}" for f in impact.recompute_failures
            )
        )
    return " ".join(parts)


def _pair_label(from_product: Product | None, to_product: Product | None, from_id: str, to_id: str) -> str:
    left = (from_product.description if from_product else None) or from_id
    right = (to_product.description if to_product else None) or to_id
    return f"{left} -> {right}"


# ---- Layer 1: technical substitutions -------------------------------------


@router.get("/technical-substitutions", response_model=TechnicalSubstitutionsOut)
def list_technical_substitutions(db: Session = Depends(get_db)):
    """Every engineering compatibility claim, with product descriptions.

    Sorted by from-product description then to-product description, so all the
    substitutes for one primary product sit together -- which is the question a planner
    arrives with ("what can replace this?"), and an id-ordered table would scatter it.

    `unpermitted_count` names the table's quietly useless state. A technical row that
    no customer rule permits grants nobody anything, because layer 2 is a positive
    allow-list; the row looks like working configuration and is not, which is exactly
    the failure `dimensions_with_no_rows` exists to surface on the lead-time table.
    """
    rows = db.query(TechnicalSubstitution).all()
    out = [_technical_out(db, row) for row in rows]
    out.sort(
        key=lambda s: (
            (s.from_product_description or s.from_product_id),
            (s.to_product_description or s.to_product_id),
        )
    )
    unpermitted = [s for s in out if s.allowing_customer_rule_count == 0]
    note = (
        f"{len(out)} technical substitution(s). A row here is LAYER 1 of three and is "
        "DIRECTIONAL -- A -> B does not mean B -> A, so both directions are separate "
        "rows if both are true. Registering one makes the substitute OFFERED as a "
        "candidate; it does NOT make it usable. The owning customer must also permit "
        "the pair (layer 2, a positive allow-list -- silence blocks), and the specific "
        "demand line needs an Approved well-layer approval (layer 3)."
    )
    if unpermitted:
        note += (
            f" {len(unpermitted)} of these are permitted by NO customer rule at all, "
            "so they currently offer a candidate that every demand line will report as "
            "blocked at the customer layer: "
            + ", ".join(
                f"{s.from_product_description or s.from_product_id} -> "
                f"{s.to_product_description or s.to_product_id}"
                for s in unpermitted
            )
            + "."
        )
    return TechnicalSubstitutionsOut(
        substitutions=out, unpermitted_count=len(unpermitted), note=note
    )


@router.post(
    "/technical-substitutions",
    response_model=TechnicalSubstitutionChangeOut,
    status_code=201,
)
def create_technical_substitution(
    payload: TechnicalSubstitutionIn, db: Session = Depends(get_db)
):
    """Register one engineering compatibility claim.

    Creating a row is consequential in its own right and not merely a precondition: if
    the customer already permits the pair and a demand line already holds an Approved
    approval for it -- which can happen, because `request_approval` deliberately allows
    a request while the other layers are unclear -- then this single insert is the last
    gate, and a line moves to CoveredViaSubstitute the moment it commits. The response
    reports that in `well_changes`.

    Two refusals, both stating a reason rather than a schema violation:

      * `from == to` is a 400. A product substituting for itself is not a weaker claim
        than a real one, it is a meaningless one: `find_candidates` would offer the
        line's OWN product back as a substitute for itself, with its own on-hand
        quantity presented as substitute availability, so the same steel would appear
        twice in one planner's answer.
      * a duplicate ordered pair is a 409 naming the existing row.
    """
    from_id = (payload.from_product_id or "").strip()
    to_id = (payload.to_product_id or "").strip()
    if from_id == to_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"from_product_id and to_product_id are both {from_id!r}: a product "
                "cannot be registered as a substitute for itself. Substitution answers "
                "'what ELSE can serve this demand', so a self-substitution would make "
                "find_candidates offer the demand line's own product back as an "
                "alternative to itself -- presenting one quantity of steel twice, once "
                "as primary coverage and once as substitute availability. Pick two "
                "different products. Nothing was saved."
            ),
        )
    from_product = _product_or_400(db, from_id, "from_product_id")
    to_product = _product_or_400(db, to_id, "to_product_id")

    existing = (
        db.query(TechnicalSubstitution)
        .filter(
            TechnicalSubstitution.from_product_id == from_id,
            TechnicalSubstitution.to_product_id == to_id,
        )
        .first()
    )
    if existing is not None:
        # Checked BEFORE the insert so the client gets a sentence rather than a driver
        # message, exactly as `_conflict_if_pair_exists` does for lead-time components.
        raise HTTPException(
            status_code=409,
            detail=(
                f"a technical substitution for {_pair_label(from_product, to_product, from_id, to_id)} "
                f"already exists (id {existing.id}). A second row for one ordered pair "
                "would make find_candidates offer that substitute TWICE for every "
                "demand line on the primary product -- the same steel presented as two "
                "independent options -- so it is refused. Note that the pair is "
                "DIRECTIONAL: the reverse direction "
                f"({_pair_label(to_product, from_product, to_id, from_id)}) is a "
                "different claim and can still be registered. Nothing was saved."
            ),
        )

    row = TechnicalSubstitution(from_product_id=from_id, to_product_id=to_id)
    # Gathered BEFORE the insert: an Approved approval or a permitting customer rule
    # may already be sitting there waiting for this claim, and after the recompute the
    # counts would be the same but the reader could no longer tell they pre-existed.
    deps = pair_dependencies(db, from_id, to_id)
    try:
        impact = apply_substitution_change(db, lambda: db.add(row))
    except IntegrityError as exc:
        raise _substitution_integrity_conflict(db, exc, "technical substitution") from exc
    db.commit()

    note = (
        f"Registered technical substitution {_pair_label(from_product, to_product, from_id, to_id)}. "
        "This is LAYER 1 only: the substitute is now OFFERED as a candidate for every "
        "demand line on the primary product, but it is not usable until the owning "
        "customer permits the pair (layer 2) and the specific demand line has an "
        "Approved approval (layer 3). "
    )
    if deps.allowing_customer_rule_count == 0:
        note += (
            "No customer rule permits this pair yet, so every candidate will currently "
            "report blocking_layer 'customer'. Add a customer substitution rule with "
            "allowed=true to change that. "
        )
    else:
        note += (
            f"{deps.allowing_customer_rule_count} customer rule(s) already permit this "
            "pair, so layer 2 is clear for them. "
        )
    if deps.approved_well_approval_count > 0:
        note += (
            f"{deps.approved_well_approval_count} demand line(s) already hold an "
            "APPROVED well-layer approval for this exact pair, which were dormant "
            "while no technical claim existed and are live again as of this call. "
        )
    note += _coverage_sentence(impact)

    return TechnicalSubstitutionChangeOut(
        action="created",
        substitution=_technical_out(db, row),
        well_approvals_affected=deps.well_approval_count,
        approved_well_approvals_affected=deps.approved_well_approval_count,
        pending_well_approvals_affected=deps.pending_well_approval_count,
        customer_rules_affected=deps.customer_rule_count,
        allowing_customer_rules_affected=deps.allowing_customer_rule_count,
        recomputed_customer_ids=list(impact.recomputed_customer_ids),
        wells_examined=impact.wells_examined,
        well_changes=_well_change_out(impact),
        coverage_recompute_failures=_recompute_failures_out(impact),
        note=note,
    )


@router.delete(
    "/technical-substitutions/{substitution_id}",
    response_model=TechnicalSubstitutionChangeOut,
)
def delete_technical_substitution(substitution_id: str, db: Session = Depends(get_db)):
    """Withdraw one engineering compatibility claim. IT PROCEEDS, and it reports what
    it broke.

    No two-phase confirmation, for the reason `delete_lead_time_component` states at
    length: a confirmation the server cannot enforce is theatre, and this codebase's
    answer to a consequential-but-legitimate change is to perform it and report
    before/after.

    What it breaks, stated because the response says so too:

      * The pair leaves `find_candidates` entirely. Every demand line on the primary
        product stops being offered this substitute -- including lines whose customer
        permits it and whose approval is APPROVED. A line standing at
        CoveredViaSubstitute on this pair loses that coverage; a line at
        PendingApproval on it stops pursuing anything.
      * `WellSubstitutionApproval` rows for the pair are COUNTED and reported, and
        deliberately LEFT IN PLACE. They are records of decisions customers actually
        made and deleting them to tidy a table would destroy history; a dangling
        approval cannot crash or mislead `find_candidates`, which derives its candidate
        set from the technical rows and never visits an approval outside it. See
        `app.engines.substitution_admin` for the verification.
      * `CustomerSubstitutionRule` rows for the pair are likewise counted, reported and
        left alone. They become inert -- layer 2 is only consulted for pairs layer 1
        offers -- and are flagged as such by
        GET /admin/customer-substitution-rules.
    """
    row = db.get(TechnicalSubstitution, substitution_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Technical substitution {substitution_id!r} not found",
        )
    from_id, to_id = row.from_product_id, row.to_product_id
    from_product = db.get(Product, from_id)
    to_product = db.get(Product, to_id)
    # Gathered BEFORE the delete -- afterwards the instance is expunged and the
    # dependency question can no longer be asked about a pair nothing records.
    deps = pair_dependencies(db, from_id, to_id)

    impact = apply_substitution_change(db, lambda: db.delete(row))
    db.commit()

    note = (
        "Withdrew technical substitution "
        f"{_pair_label(from_product, to_product, from_id, to_id)}. The pair is gone "
        "from the candidate list for every demand line on the primary product, "
        "whatever the other two layers say. "
    )
    if deps.well_approval_count:
        note += (
            f"{deps.well_approval_count} well-layer approval(s) referenced this pair "
            f"({deps.approved_well_approval_count} Approved, "
            f"{deps.pending_well_approval_count} Pending). They were NOT deleted: an "
            "approval records a decision a customer actually made, so it is kept as "
            "history. They now have no effect -- the candidate list is derived from "
            "the technical rows, so an approval outside that set is never consulted -- "
            "and they become live again if this pair is ever re-registered. "
        )
    else:
        note += "No well-layer approval referenced this pair. "
    if deps.customer_rule_count:
        note += (
            f"{deps.customer_rule_count} customer rule(s) named this pair "
            f"({deps.allowing_customer_rule_count} permitting). They were NOT deleted "
            "either -- a customer's permission or veto is a commercial fact this "
            "endpoint does not own -- but they are now INERT and are flagged as such "
            "on GET /admin/customer-substitution-rules. "
        )
    else:
        note += "No customer rule named this pair. "
    note += _coverage_sentence(impact)

    return TechnicalSubstitutionChangeOut(
        action="deleted",
        substitution=None,
        well_approvals_affected=deps.well_approval_count,
        approved_well_approvals_affected=deps.approved_well_approval_count,
        pending_well_approvals_affected=deps.pending_well_approval_count,
        customer_rules_affected=deps.customer_rule_count,
        allowing_customer_rules_affected=deps.allowing_customer_rule_count,
        recomputed_customer_ids=list(impact.recomputed_customer_ids),
        wells_examined=impact.wells_examined,
        well_changes=_well_change_out(impact),
        coverage_recompute_failures=_recompute_failures_out(impact),
        note=note,
    )


# ---- Layer 2: customer substitution rules ---------------------------------


@router.get(
    "/customer-substitution-rules", response_model=CustomerSubstitutionRulesOut
)
def list_customer_substitution_rules(
    customer_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """Layer-2 rules for one customer, or for every customer when `customer_id` is
    omitted.

    An unknown `customer_id` is a 404 rather than an empty list. "This customer has no
    rules" and "there is no such customer" are different facts, and serving the second
    as the first would let a screen show a blank allow-list -- which reads as "nothing
    is permitted", a real and much stronger claim -- for what is actually a typo.
    """
    if customer_id is not None:
        if db.get(Customer, customer_id) is None:
            raise HTTPException(
                status_code=404, detail=f"Customer {customer_id!r} not found"
            )
    query = db.query(CustomerSubstitutionRule)
    if customer_id is not None:
        query = query.filter(CustomerSubstitutionRule.customer_id == customer_id)
    rules = [_rule_out(db, row) for row in query.all()]
    rules.sort(
        key=lambda r: (
            (r.customer_name or r.customer_id),
            (r.from_product_description or r.from_product_id),
            (r.to_product_description or r.to_product_id),
        )
    )
    inert = [r for r in rules if not r.technical_substitution_exists]
    vetoes = [r for r in rules if not r.allowed]
    note = (
        f"{len(rules)} customer substitution rule(s). This is LAYER 2 and it is a TRUE "
        "ALLOW-LIST: a pair with NO row here is blocked exactly as an allowed=false row "
        "is. So deleting a permitting rule reverts the pair to blocked, and the "
        "difference between allowed=false and no row at all is provenance -- a recorded "
        "customer decision versus silence -- not effect. "
        f"{len(vetoes)} row(s) are explicit vetoes (allowed=false)."
    )
    if inert:
        note += (
            f" {len(inert)} rule(s) name a pair with NO technical substitution and "
            "therefore have no effect at all: the candidate list is built from the "
            "technical rows and a rule outside that set is never consulted. Register "
            "the technical substitution to make them live."
        )
    return CustomerSubstitutionRulesOut(
        rules=rules, customer_id=customer_id, inert_count=len(inert), note=note
    )


@router.post(
    "/customer-substitution-rules",
    response_model=CustomerSubstitutionRuleChangeOut,
    status_code=201,
)
def create_customer_substitution_rule(
    payload: CustomerSubstitutionRuleIn, db: Session = Depends(get_db)
):
    """Record whether one customer permits one substitution pair.

    A MISSING TECHNICAL SUBSTITUTION IS A WARNING, NOT A REFUSAL
    ------------------------------------------------------------
    It is tempting to refuse -- "a customer cannot permit what is not technically
    valid" -- and that was rejected for three reasons, in decreasing order of weight.

    1. The three layers are documented and implemented as INDEPENDENT, and the
       platform's other write endpoint on this model already behaves this way:
       `app.engines.substitution.request_approval` states outright that "requesting is
       allowed even when the technical/customer layers do not clear -- the layers are
       independent". Enforcing a layer-1 prerequisite here while layer 3 has none would
       be this endpoint inventing a dependency the engine does not have, and two
       sibling writes disagreeing about whether the layers are independent.
    2. It is the real order of business. A commercial agreement with a customer
       routinely lands before engineering signs off the compatibility -- the
       negotiation is what triggers the engineering review. Refusing would force the
       operator to fabricate a technical claim first, which is a worse database than an
       inert rule.
    3. The actual hazard is INVISIBILITY, not incorrectness. Such a rule cannot cause
       a wrong verdict: `find_candidates` never reads it. It can only sit there looking
       like configuration while doing nothing -- and this codebase's answer to that is
       a loud statement, not a refusal (see `dimensions_with_no_rows`, which does
       exactly this for a lead-time table that looks healthy and is not). So the row is
       written, `warning` says it is inert and why, `technical_substitution_exists` is
       false on every read of it, and the list endpoint counts it in `inert_count`.

    A duplicate (customer, from, to) IS refused, with a 409 naming the existing row and
    pointing at PATCH -- because for that triple there is a correct single answer and a
    second row would make which permission is in force depend on query order.
    """
    customer = db.get(Customer, (payload.customer_id or "").strip())
    if customer is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"customer_id {payload.customer_id!r} is not a customer on this "
                "platform. Pick one from GET /customers. Nothing was saved."
            ),
        )
    from_id = (payload.from_product_id or "").strip()
    to_id = (payload.to_product_id or "").strip()
    if from_id == to_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"from_product_id and to_product_id are both {from_id!r}. A rule about "
                "substituting a product for itself permits nothing, because there is "
                "no such substitution to permit -- POST "
                "/admin/technical-substitutions refuses the same pair for the same "
                "reason. Pick two different products. Nothing was saved."
            ),
        )
    from_product = _product_or_400(db, from_id, "from_product_id")
    to_product = _product_or_400(db, to_id, "to_product_id")

    existing = (
        db.query(CustomerSubstitutionRule)
        .filter(
            CustomerSubstitutionRule.customer_id == customer.id,
            CustomerSubstitutionRule.from_product_id == from_id,
            CustomerSubstitutionRule.to_product_id == to_id,
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{customer.name} already has a rule for "
                f"{_pair_label(from_product, to_product, from_id, to_id)} "
                f"(id {existing.id}, allowed="
                f"{'true' if existing.allowed else 'false'}). Two rows for one "
                "(customer, from, to) triple would make the permission actually in "
                "force depend on which row the engine's query returned last -- an "
                "explicit veto could be silently overridden by a stale allow, with no "
                "screen able to show which one won. To change the answer, flip the "
                f"existing row: PATCH /admin/customer-substitution-rules/{existing.id} "
                'with {"allowed": ...}. Nothing was saved.'
            ),
        )

    row = CustomerSubstitutionRule(
        customer_id=customer.id,
        from_product_id=from_id,
        to_product_id=to_id,
        allowed=bool(payload.allowed),
    )
    has_technical = technical_substitution_exists(db, from_id, to_id)
    try:
        impact = apply_substitution_change(db, lambda: db.add(row))
    except IntegrityError as exc:
        raise _substitution_integrity_conflict(
            db, exc, "customer substitution rule"
        ) from exc
    db.commit()

    pair = _pair_label(from_product, to_product, from_id, to_id)
    warning = None
    note = (
        f"Recorded that {customer.name} "
        + ("PERMITS" if row.allowed else "does NOT permit")
        + f" {pair}. "
    )
    if row.allowed:
        note += (
            "Layer 2 is now clear for this customer. The substitute still needs an "
            "Approved well-layer approval on each specific demand line (layer 3) "
            "before it can cover anything. "
        )
    else:
        note += (
            "This is an explicit VETO. Note that it reaches the same verdict as having "
            "no rule at all -- layer 2 is a positive allow-list -- so what it adds over "
            "silence is provenance: the block is now a recorded customer decision "
            "rather than an absence. "
        )
    if not has_technical:
        warning = (
            "This rule was SAVED but currently has NO EFFECT: no technical "
            f"substitution is registered for {pair}, and the candidate list is built "
            "from the technical rows -- a customer rule outside that set is never "
            "consulted by find_candidates. It was accepted rather than refused because "
            "the three substitution layers are independent (the well-layer approval "
            "endpoint accepts a request under the same conditions) and a commercial "
            "agreement legitimately precedes engineering signoff. Register the "
            "technical substitution via POST /admin/technical-substitutions to make "
            "this permission live."
        )
        note += warning + " "
    note += _coverage_sentence(impact)

    return CustomerSubstitutionRuleChangeOut(
        action="created",
        rule=_rule_out(db, row),
        allowed_before=None,
        allowed_after=bool(row.allowed),
        warning=warning,
        recomputed_customer_ids=list(impact.recomputed_customer_ids),
        wells_examined=impact.wells_examined,
        well_changes=_well_change_out(impact),
        coverage_recompute_failures=_recompute_failures_out(impact),
        note=note,
    )


@router.patch(
    "/customer-substitution-rules/{rule_id}",
    response_model=CustomerSubstitutionRuleChangeOut,
)
def update_customer_substitution_rule(
    rule_id: str, payload: CustomerSubstitutionRulePatch, db: Session = Depends(get_db)
):
    """Flip `allowed` on an existing rule.

    This exists as a separate verb from DELETE because it is a different EVENT. A
    customer that used to disallow a substitution and now permits it is a normal
    commercial change; expressing it as delete-then-create would pass through "no
    row" -- a third state with different provenance -- and would discard the row's
    identity so nothing could refer to it.

    Setting `allowed` to the value it already has is accepted and reported as such
    rather than refused. The permission is unchanged, so nothing has been misstated;
    coverage is still recomputed, because unconditional recompute is the one rule (see
    `app.engines.substitution_admin`).
    """
    row = db.get(CustomerSubstitutionRule, rule_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Customer substitution rule {rule_id!r} not found"
        )
    before = bool(row.allowed)
    after = bool(payload.allowed)
    rule_before = _rule_out(db, row)

    def mutate() -> None:
        row.allowed = after

    impact = apply_substitution_change(db, mutate)
    db.commit()

    pair = f"{rule_before.from_product_description or rule_before.from_product_id} -> {rule_before.to_product_description or rule_before.to_product_id}"
    who = rule_before.customer_name or rule_before.customer_id
    if before == after:
        note = (
            f"{who} already {'permitted' if after else 'did not permit'} {pair}; "
            "allowed was already "
            f"{'true' if after else 'false'}, so the permission is unchanged. "
        )
    elif after:
        note = (
            f"{who} now PERMITS {pair} (allowed false -> true). Layer 2 is clear for "
            "this customer; each specific demand line still needs an Approved "
            "well-layer approval before the substitute can cover anything. "
        )
    else:
        note = (
            f"{who} now VETOES {pair} (allowed true -> false). Every candidate for "
            "this pair will report blocking_layer 'customer', regardless of any "
            "Approved well-layer approval -- the layers are independent and the "
            "customer layer is reported first. Any line that was covered via this "
            "substitute has lost that coverage. "
        )
    warning = rule_before.inert_reason
    if warning is not None:
        note += (
            "This rule names a pair with no technical substitution, so the flip changes "
            "nothing today. "
        )
    note += _coverage_sentence(impact)

    return CustomerSubstitutionRuleChangeOut(
        action="updated",
        rule=_rule_out(db, row),
        allowed_before=before,
        allowed_after=after,
        warning=warning,
        recomputed_customer_ids=list(impact.recomputed_customer_ids),
        wells_examined=impact.wells_examined,
        well_changes=_well_change_out(impact),
        coverage_recompute_failures=_recompute_failures_out(impact),
        note=note,
    )


@router.delete(
    "/customer-substitution-rules/{rule_id}",
    response_model=CustomerSubstitutionRuleChangeOut,
)
def delete_customer_substitution_rule(rule_id: str, db: Session = Depends(get_db)):
    """Remove one layer-2 rule. IT PROCEEDS, and it reports what it broke.

    REMOVING A RULE IS NOT NEUTRAL. Layer 2 is a positive allow-list -- verified in
    `find_candidates`, which computes `customer_allowed = bool(rule is not None and
    rule.allowed)` -- so deleting an `allowed=true` row reverts the pair to BLOCKED,
    exactly as an explicit veto would. Any line covered via that substitute loses its
    coverage in this request, and `well_changes` proves it.

    Deleting an `allowed=false` row changes no verdict at all: silence and a veto reach
    the same answer. What it destroys is PROVENANCE -- the record that the customer was
    asked and said no -- and the note says so rather than reporting a harmless no-op.
    """
    row = db.get(CustomerSubstitutionRule, rule_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Customer substitution rule {rule_id!r} not found"
        )
    removed = _rule_out(db, row)
    before = bool(row.allowed)

    impact = apply_substitution_change(db, lambda: db.delete(row))
    db.commit()

    pair = f"{removed.from_product_description or removed.from_product_id} -> {removed.to_product_description or removed.to_product_id}"
    who = removed.customer_name or removed.customer_id
    if before:
        note = (
            f"Deleted {who}'s PERMISSION for {pair}. This is not a cleanup: layer 2 is "
            "a positive allow-list, so with no row the pair is now BLOCKED for this "
            "customer exactly as an explicit veto would block it. Every candidate for "
            "the pair will report blocking_layer 'customer', and any line that was "
            "covered via this substitute has lost that coverage. To record a decision "
            "instead of reverting to silence, POST the rule back with allowed=false. "
        )
    else:
        note = (
            f"Deleted {who}'s VETO on {pair}. No verdict changes: layer 2 is a "
            "positive allow-list, so 'no row' already blocks the pair exactly as "
            "allowed=false did. What is lost is PROVENANCE -- the record that this "
            "customer was asked and declined is gone, and the block now reads as "
            "silence. "
        )
    if not removed.technical_substitution_exists:
        note += (
            "The pair has no technical substitution anyway, so this rule was inert "
            "before the delete. "
        )
    note += _coverage_sentence(impact)

    return CustomerSubstitutionRuleChangeOut(
        action="deleted",
        rule=None,
        allowed_before=before,
        allowed_after=None,
        warning=removed.inert_reason,
        recomputed_customer_ids=list(impact.recomputed_customer_ids),
        wells_examined=impact.wells_examined,
        well_changes=_well_change_out(impact),
        coverage_recompute_failures=_recompute_failures_out(impact),
        note=note,
    )

# ---------------------------------------------------------------------------
# Safety stock -- planner-owned, per product. See app.models.safety_stock for
# the boundary: read by Material Order Requirements, deliberately NOT by
# coverage.
# ---------------------------------------------------------------------------


@router.get("/safety-stocks", response_model=SafetyStockListOut)
def list_safety_stocks(db: Session = Depends(get_db)):
    """Every product, with its safety stock where one is set. Products without
    a row are listed with quantity=None -- "not set" is a real state and the
    screen must show it as such rather than as 0."""
    stocks = {row.product_id: row for row in db.query(SafetyStock).all()}
    rows = []
    for product in db.query(Product).order_by(Product.description).all():
        row = stocks.get(product.id)
        rows.append(
            SafetyStockRowOut(
                product_id=product.id,
                product_description=product.description,
                unit_of_measure=product.unit_of_measure,
                quantity=row.quantity if row is not None else None,
                note=row.note if row is not None else None,
            )
        )
    return SafetyStockListOut(
        rows=rows,
        note=(
            "Safety stock changes WHEN TO ORDER (Material Order Requirements "
            "triggers below this level), never WHETHER DEMAND IS COVERED -- "
            "coverage verdicts ignore it by design."
        ),
    )


@router.put("/safety-stocks/{product_id}", response_model=SafetyStockRowOut)
def upsert_safety_stock(
    product_id: str, body: SafetyStockIn, db: Session = Depends(get_db)
):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    if body.quantity < 0:
        raise HTTPException(
            status_code=400, detail="Safety stock cannot be negative"
        )
    row = (
        db.query(SafetyStock)
        .filter(SafetyStock.product_id == product_id)
        .first()
    )
    if row is None:
        row = SafetyStock(product_id=product_id)
        db.add(row)
    row.quantity = body.quantity
    row.note = body.note
    db.commit()
    return SafetyStockRowOut(
        product_id=product.id,
        product_description=product.description,
        unit_of_measure=product.unit_of_measure,
        quantity=row.quantity,
        note=row.note,
    )


@router.delete("/safety-stocks/{product_id}", status_code=204)
def delete_safety_stock(product_id: str, db: Session = Depends(get_db)):
    """Remove the setting entirely -- back to "not set", which is not the same
    statement as an explicit 0."""
    row = (
        db.query(SafetyStock)
        .filter(SafetyStock.product_id == product_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No safety stock set")
    db.delete(row)
    db.commit()

