"""Every quantity the API serves has a UNIT reachable beside it.

The defect this module guards
-----------------------------
The product owner's report was blunt: "unit of measures are missing across all
screen. whenever quantity is shows there has to be a unit of measure." Every
quantity in every payload was a bare number, so no screen could label one.

A per-field test would have been the obvious response and would have been the
wrong one: it can only cover the fields somebody remembered, and the failure mode
here is a NEW quantity field being added later with no unit -- exactly the thing a
hand-written list cannot see. So the central test in this module
(`test_every_quantity_bearing_payload_exposes_a_unit`) WALKS the Pydantic schema
module, finds every model with a quantity-shaped field, and requires a unit to be
reachable from it. A new quantity field with no unit fails the suite rather than
reaching a screen.

The rest of the module pins the two things a structural walk cannot check: that
the units are actually POPULATED with the right values end to end, and that
cross-product aggregates refuse to produce a bare scalar across mixed units.
"""

import inspect
from datetime import datetime, timedelta

from pydantic import BaseModel

from app import schemas
from app.engines.executive import (
    MIXED_UNITS_NOTE,
    executive_summary,
    quantity_by_unit,
)
from app.engines.mrp import by_item, mrp_summary
from app.models import (
    AllocationPolicy,
    BusinessUnit,
    Customer,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryOnHand,
    PlanningNode,
    Product,
    UnitOfMeasure,
    Well,
)

# ---------------------------------------------------------------------------
# The structural guarantee
# ---------------------------------------------------------------------------

#: WORD-tokens that mark a field as carrying a QUANTITY OF MATERIAL. A field
#: matches when any underscore-separated token of its name is in this set.
#:
#: Token matching rather than substring matching, so `demand_line_id` and
#: `total_months` are not dragged in by "demand" and "total" -- an id is a join key
#: and a duration is a different dimension entirely. Deliberately generous
#: otherwise: it is better for this walk to demand a unit for a field that turns out
#: not to need one (a reviewed one-line entry in `NOT_A_MATERIAL_QUANTITY`) than to
#: miss one that does, because a missed field is a bare number on a screen.
QUANTITY_TOKENS = {
    "quantity",
    "quantities",
    "qty",
    "hand",
    "balance",
    "shareable",
    "shortfall",
    "assigned",
    "allocated",
    "unallocated",
    "demand",
    "total",
    # Added when the on-order projection landed. `on_order` is a quantity of
    # material and the walk must demand a unit for it; without this token the field
    # would have slipped in unlabelled, which is exactly the failure mode this walk
    # exists to catch. Widening the token set is a STRENGTHENING of the guard.
    "order",
    "incoming",
    # Added when customer-owned inventory landed. `customer_owned` is a quantity of
    # material and the walk must demand a unit for it; without this token the field
    # would have slipped in unlabelled, which is exactly the failure mode this walk
    # exists to catch. Widening the token set is a STRENGTHENING of the guard, the
    # same as "order" was.
    "owned",
}

#: Tokens that mark a field as measuring something OTHER than material, whatever
#: else its name contains. Any of these wins over `QUANTITY_TOKENS`.
NON_MATERIAL_TOKENS = {
    "id",
    # An arrival is an EVENT / a date, not a quantity. Needed once "order" became a
    # quantity token, so that `on_order_earliest_arrival` (a date) is not demanded
    # to carry a unit of measure while `on_order` (a quantity) still is.
    "arrival",
    "ids",
    "count",
    "months",
    "days",
    "pct",
    "date",
    "dates",
    "source",
    "note",
    "notes",
    "reason",
    "explanation",
    "definition",
    "trend",
}

#: Fields whose name matches a marker but which are NOT a quantity of material.
#: Every entry is a deliberate exemption with a stated reason, so the list is
#: reviewable rather than a way of silencing the test.
NOT_A_MATERIAL_QUANTITY = {
    # Counts of rows, not quantities of steel. A count is dimensionless.
    ("CoverageGridRowOut", "in_scope_line_count"),
    ("CoverageGridRowOut", "covered_line_count"),
    ("HorizonDemandOut", "current_line_count"),
    ("SupplyRiskOut", "unrecoverable_line_count"),
    ("DemandLineListOut", "total"),
    ("DemandImportBatchOut", "row_count"),
    ("DemandImportBatchSummaryOut", "row_count"),
    ("SubstitutionCandidateOut", "pending_line_count"),
    # Provenance string ("unavailable" | "synthetic" | "oracle" | "mixed"), not a
    # number at all.
    ("InventoryPositionOut", "assigned_source"),
    # Verbatim spreadsheet cell text, deliberately unparsed and unvalidated. A row
    # that failed to match a product has no product and therefore no unit; putting
    # one here would label a cell nobody has validated. `unit_of_measure` on the
    # same model covers the PARSED `quantity`.
    ("DemandImportRowOut", "raw_quantity"),
    # A boolean, despite the name.
    ("SubstitutionCandidateOut", "over_subscribed"),
    # A well's PLANNER-SET demand status (Planned / Budgeted / Confirmed) -- an
    # enum describing how firm the well's programme is, not a quantity of anything.
    # It matches the "demand" token, which is the walk being deliberately generous;
    # exempting it is a one-line reviewed decision, exactly as intended.
    ("WellOut", "demand_status"),
    ("WellSummary", "demand_status"),
    # Same enum, on the grid row, where it exists so an unevaluated well can name
    # the reason it has no verdict rather than only the fact.
    ("CoverageGridRowOut", "demand_status"),
    ("WellDemandStatusChangeOut", "demand_line_ids"),
    ("WellDemandStatusChangeOut", "revised_line_count"),
    # Prose notes.
    ("SoftAllocationCoverageOut", "note"),
    ("IncomingSupplyOut", "note"),
    ("FirstRunoutDetailOut", "note"),
    # Counts of rows, not quantities of steel (see the block above).
    ("SoftAllocationCoverageOut", "total_line_count"),
    ("FirstRunoutDetailOut", "total_well_count"),
    # Ids and labels, not quantities.
    ("SoftAllocationCoverageOut", "unresolved_customer_ids"),
    # A count of PRODUCTS with no on-order row -- dimensionless, and its whole point
    # is to say how much of the answer is missing.
    ("IncomingSupplyOut", "products_with_no_data"),
    ("IncomingSupplyOut", "products_with_nothing_on_order"),
    ("SupplyRiskOut", "note"),
    ("SubstitutionCandidateOut", "over_subscription_note"),
}

#: Models that carry the unit ONCE for the whole payload, or reach it through a
#: nested model, rather than repeating it on every quantity field. Mapped to how it
#: is reached, so the walk can verify the route actually exists.
UNIT_REACHED_VIA = {
    # One product per page: the unit is stated once at the top.
    "InventoryPositionOut": "unit_of_measure",
    "RunoutPointOut": "unit_of_measure",
    "ByItemAnalysisOut": "unit_of_measure",
    # Reached through the nested catalogue payload.
    "SubstitutionCandidateOut": "product.unit_of_measure",
    # Cross-product aggregates: the scalar unit may legitimately be null, and the
    # ALWAYS-correct answer is the per-unit breakdown. Both must be present.
    "HorizonDemandOut": "quantities_by_unit",
    "SupplyRiskOut": "quantities_by_unit",
    "SoftAllocationCoverageOut": "quantities_by_unit",
    "IncomingSupplyOut": "quantities_by_unit",
    "RiskImpactOut": "quantities_by_unit_before",
}


def _schema_models():
    """Every RESPONSE model in `app.schemas`.

    Request bodies (`...In`) are excluded, and that is a real distinction rather
    than convenience. The rule being enforced is about what the platform SERVES: a
    quantity it displays must be labelled. A request body is the other direction --
    `DemandRevisionIn` restates the quantity of an EXISTING demand line, whose
    product, and therefore whose unit, is already fixed by the line being revised.
    Accepting a unit there would create a second place the unit is stated and a way
    for a client to contradict the catalogue, which is exactly what keeping the unit
    on `Product` was meant to prevent.
    """
    for name, obj in vars(schemas).items():
        if (
            inspect.isclass(obj)
            and issubclass(obj, BaseModel)
            and obj is not BaseModel
            and obj.__module__ == schemas.__name__
            and not name.endswith("In")
            and not name.endswith("Patch")
        ):
            yield name, obj


def _is_model(annotation):
    """Does `annotation` resolve to a Pydantic model (directly, or inside list/|)?"""
    candidates = [annotation, *getattr(annotation, "__args__", ())]
    for candidate in candidates:
        for inner in [candidate, *getattr(candidate, "__args__", ())]:
            if inspect.isclass(inner) and issubclass(inner, BaseModel):
                return True
    return False


def _quantity_fields(model):
    """Fields of `model` that carry a quantity of material, directly.

    Fields whose type is another Pydantic model are SKIPPED: the quantity lives on
    that model, which this walk examines on its own, so requiring a unit on the
    container as well would demand it in the wrong place. `WellOut.demand_lines` is
    the example -- the unit belongs on `DemandLineOut`, per line, because the lines
    can be for different products.
    """
    for field_name, field in model.model_fields.items():
        tokens = set(field_name.split("_"))
        if not tokens & QUANTITY_TOKENS:
            continue
        if tokens & NON_MATERIAL_TOKENS:
            continue
        if _is_model(field.annotation):
            continue
        yield field_name


def _reaches_a_unit(name, model):
    """Is a unit of measure reachable from `model`? Returns a reason when not."""
    if "unit_of_measure" in model.model_fields:
        return None

    route = UNIT_REACHED_VIA.get(name)
    if route is None:
        return (
            "no `unit_of_measure` field and no entry in UNIT_REACHED_VIA. Add the "
            "field, or -- if the unit is genuinely reached through a nested model or "
            "a per-unit breakdown -- record the route in UNIT_REACHED_VIA so this "
            "test verifies it."
        )

    # Walk the recorded route and confirm every hop exists.
    current = model
    for hop in route.split("."):
        if not (
            hasattr(current, "model_fields") and hop in current.model_fields
        ):
            return f"UNIT_REACHED_VIA route {route!r} is broken at {hop!r}"
        annotation = current.model_fields[hop].annotation
        current = annotation
        # Unwrap list[...] / X | None so the next hop can be checked.
        args = getattr(annotation, "__args__", ())
        for arg in args:
            if inspect.isclass(arg) and issubclass(arg, BaseModel):
                current = arg
                break
    return None


def test_every_quantity_bearing_payload_exposes_a_unit():
    """THE structural guarantee. A quantity with no reachable unit is a defect.

    Walks every response model in `app.schemas`. This is what makes the fix hold
    over time: a quantity field added next year is caught here, by name, without
    anybody having to remember this rule.
    """
    problems = []
    for name, model in sorted(_schema_models()):
        fields = [
            field
            for field in _quantity_fields(model)
            if (name, field) not in NOT_A_MATERIAL_QUANTITY
        ]
        if not fields:
            continue
        reason = _reaches_a_unit(name, model)
        if reason is not None:
            problems.append(f"{name} (quantity fields {fields}): {reason}")

    assert not problems, (
        "these payloads carry a quantity with no reachable unit of measure:\n  "
        + "\n  ".join(problems)
    )


def test_the_walk_actually_finds_the_payloads_it_is_meant_to_guard():
    """Guard the guard: a marker list that matched nothing would pass vacuously.

    The specific payloads the product owner's report named are asserted to be IN
    the set the walk examines. Without this, a typo in QUANTITY_MARKERS would turn
    the test above into a no-op that still went green.
    """
    examined = {
        name
        for name, model in _schema_models()
        if any(
            (name, field) not in NOT_A_MATERIAL_QUANTITY
            for field in _quantity_fields(model)
        )
    }
    for required in (
        "DemandLineOut",
        "DemandLineRowOut",
        "MrpRecommendationOut",
        "ByItemDemandLineOut",
        "InventoryPositionOut",
        "RunoutPointOut",
        "SubstitutionCandidateOut",
        "HorizonDemandOut",
        "SupplyRiskOut",
        # RENAMED from AllocationSummaryOut: the block now reports SOFT allocation
        # coverage rather than Oracle's hard assignment (see
        # app.engines.executive). The guard-the-guard list follows the rename so it
        # keeps guarding a payload that exists.
        "SoftAllocationCoverageOut",
        "SoftAllocationChannelOut",
        "CoverageSummaryOut",
        "CoverageQuantityByStatusOut",
        "IncomingSupplyOut",
        "OnOrderArrivalOut",
        "FirstRunoutWellOut",
        "DemandImportRowOut",
        "LineCoverageChangeOut",
        "MrpRowChangeOut",
    ):
        assert required in examined, (
            f"{required} carries a quantity but the walk did not examine it -- "
            "QUANTITY_MARKERS no longer matches its field names"
        )


def test_the_unit_is_a_closed_enum_not_free_text():
    """`unit_of_measure` must be the enum, everywhere it appears.

    Free text would make the "never silently sum across units" rule unenforceable
    rather than untidy: `quantity_by_unit` GROUPS on this value, so "Mtr"/"mtr"/"m"
    would fragment one genuine single-unit total into three partial ones. See
    `app.models.product.UnitOfMeasure`.
    """
    for name, model in sorted(_schema_models()):
        field = model.model_fields.get("unit_of_measure")
        if field is None:
            continue
        annotation = field.annotation
        candidates = getattr(annotation, "__args__", (annotation,))
        assert UnitOfMeasure in candidates, (
            f"{name}.unit_of_measure is typed {annotation!r}, not UnitOfMeasure. "
            "The unit must be the closed enum so aggregates can group on it."
        )


# ---------------------------------------------------------------------------
# The values are actually populated, end to end
# ---------------------------------------------------------------------------


def _world(db, units=(UnitOfMeasure.MTR,)):
    """One customer, one well, one Confirmed line per unit in `units`."""
    bu = BusinessUnit(name="UoM BU")
    db.add(bu)
    db.flush()
    customer = Customer(
        name="UoM Co", allocation_policy=AllocationPolicy.SOFT, business_unit_id=bu.id
    )
    db.add(customer)
    db.flush()
    node = PlanningNode(customer_id=customer.id, node_type="Project", name="UoM Project")
    db.add(node)
    db.flush()
    well = Well(planning_node_id=node.id, name="WELL-UOM", demand_status=DemandStatus.CONFIRMED)
    db.add(well)
    db.flush()

    lines = []
    for index, unit in enumerate(units):
        product = Product(
            type="CSG", size=f"9-{index}/8", weight=53.5 + index, grade="P110",
            grade_type="Carbon", connection="VAM 21", commodity="SMLS",
            description=f"CSG unit-test product {unit.value}",
            unit_of_measure=unit,
        )
        db.add(product)
        db.flush()
        db.add(
            InventoryOnHand(
                business_unit_id=bu.id, product_id=product.id, quantity=10_000,
                source_system="synthetic",
            )
        )
        line = DemandLine(
            well_id=well.id, product_id=product.id, quantity=1000 * (index + 1),
            ros_date=datetime.utcnow() + timedelta(days=40 + index),
            profile=DemandProfile.PRIMARY,
        )
        db.add(line)
        db.flush()
        lines.append((product, line))
    return customer, well, lines


def test_demand_line_payload_carries_both_the_unit_and_a_description(db_session):
    """The two reported defects, on the payload both were reported against.

    `DemandLineOut` is what GET /wells/{id} serves and the Well Workspace renders.
    It carried `product_id` and a bare `quantity`, so the product column showed a
    raw UUID and the quantity column showed an unlabelled number.
    """
    from app.api.wells import _well_out
    from app.engines.coverage import recompute_well

    _customer, well, lines = _world(db_session)
    recompute_well(db_session, well)
    product, _line = lines[0]

    payload = _well_out(well)
    (row,) = payload.demand_lines

    assert row.unit_of_measure is UnitOfMeasure.MTR
    assert row.product_description == product.description
    # The specific regression: not a UUID.
    assert row.product_description != row.product_id
    assert "-" not in row.product_description or " " in row.product_description


def test_a_product_without_a_description_falls_back_to_its_id_not_to_blank(db_session):
    """The fallback is an identifier, never an empty cell.

    `Product.description` is nullable, so a catalogue row can genuinely lack one.
    Serving null would put the Well Workspace back to having nothing to render;
    serving the id at least identifies the row and is visibly an identifier.
    """
    from app.api.wells import _well_out
    from app.engines.coverage import recompute_well

    _customer, well, lines = _world(db_session)
    product, _line = lines[0]
    product.description = None
    db_session.flush()
    recompute_well(db_session, well)

    (row,) = _well_out(well).demand_lines
    assert row.product_description == product.id


def test_mrp_and_by_item_carry_the_analysed_products_unit(db_session):
    """Every quantity MRP serves is labelled, including the runout series."""
    from app.engines.coverage import recompute_well

    _customer, well, lines = _world(db_session)
    product, line = lines[0]
    # Make the line short so a recommendation exists to inspect.
    line.quantity = 99_999
    db_session.flush()
    recompute_well(db_session, well)

    (recommendation,) = mrp_summary(db_session)
    assert recommendation.unit_of_measure is UnitOfMeasure.MTR

    analysis = by_item(db_session, product.id)
    assert analysis.unit_of_measure is UnitOfMeasure.MTR
    assert analysis.inventory.unit_of_measure is UnitOfMeasure.MTR
    assert analysis.demand_lines[0].unit_of_measure is UnitOfMeasure.MTR
    # Every point of the series, so a chart axis can be labelled from the data.
    assert analysis.runout, "the projection produced no points to label"
    assert {point.unit_of_measure for point in analysis.runout} == {UnitOfMeasure.MTR}


def test_by_item_labels_the_series_with_the_PAGE_product_not_the_lines(db_session):
    """A substituted line's own unit must not relabel the balance being projected.

    The runout balance is a quantity of the product being ANALYSED, while the lines
    charged to it can belong to other products (that is what substitution does). If
    the series took its unit from the lines, a substitute's page would label the
    substitute's own stock with the primary's unit -- which is why
    `_runout_series` takes the unit as an argument rather than deriving it.
    """
    from app.engines.coverage import recompute_well

    _customer, well, lines = _world(db_session, units=(UnitOfMeasure.MTR, UnitOfMeasure.PC))
    recompute_well(db_session, well)
    mtr_product, _ = lines[0]
    pc_product, _ = lines[1]

    assert {p.unit_of_measure for p in by_item(db_session, mtr_product.id).runout} == {
        UnitOfMeasure.MTR
    }
    assert {p.unit_of_measure for p in by_item(db_session, pc_product.id).runout} == {
        UnitOfMeasure.PC
    }


# ---------------------------------------------------------------------------
# Cross-product aggregates: never a silent sum across units
# ---------------------------------------------------------------------------


def test_quantity_by_unit_groups_and_refuses_a_scalar_when_mixed():
    """The single implementation of the rule, tested directly."""
    breakdown, single = quantity_by_unit(
        [(UnitOfMeasure.MTR, 100.0), (UnitOfMeasure.MTR, 50.0)]
    )
    assert [(e.unit_of_measure, e.quantity) for e in breakdown] == [
        (UnitOfMeasure.MTR, 150.0)
    ]
    assert single is UnitOfMeasure.MTR

    breakdown, single = quantity_by_unit(
        [(UnitOfMeasure.MTR, 100.0), (UnitOfMeasure.MT, 7.0), (UnitOfMeasure.MTR, 5.0)]
    )
    # Grouped, ordered by the unit's own value so the list is stable between calls,
    # and NEVER added together: 105 metres and 7 tonnes, not 112 of anything.
    assert [(e.unit_of_measure, e.quantity) for e in breakdown] == [
        (UnitOfMeasure.MT, 7.0),
        (UnitOfMeasure.MTR, 105.0),
    ]
    assert single is None, "a mixed-unit aggregate must not claim a single unit"

    # Nothing summed means no unit to claim -- not a defaulted one.
    assert quantity_by_unit([]) == ((), None)

    # A unit present at zero still contributes an entry: "we have demand in tonnes
    # and it totals zero" is a fact, and hiding it would hide that a second unit is
    # in play -- which is what makes the scalar unsafe in the first place.
    breakdown, single = quantity_by_unit(
        [(UnitOfMeasure.MTR, 100.0), (UnitOfMeasure.MT, 0.0)]
    )
    assert len(breakdown) == 2
    assert single is None


def test_executive_dashboard_reports_one_unit_when_there_is_only_one(db_session):
    """The normal case: single unit, so the scalar totals are labelled and safe."""
    from app.engines.coverage import recompute_well

    customer, well, _lines = _world(db_session)
    recompute_well(db_session, well)

    summary = executive_summary(db_session, customer_id=customer.id)
    horizon = next(h for h in summary.demand_trend.horizons if h.months == 24)
    assert horizon.unit_of_measure is UnitOfMeasure.MTR
    assert [e.unit_of_measure for e in horizon.quantities_by_unit] == [UnitOfMeasure.MTR]
    assert horizon.quantities_by_unit[0].quantity == horizon.current_total
    assert MIXED_UNITS_NOTE not in summary.notes


def test_executive_dashboard_refuses_a_scalar_across_mixed_units(db_session):
    """The honest answer when metres and tonnes are both in scope.

    Three things must happen together, and all three are asserted: the scalar unit
    goes null (so no screen renders the bare total), the per-unit breakdown carries
    the real figures, and the PERCENTAGE change is reported unavailable rather than
    computed from a sum of unlike things.
    """
    from app.engines.coverage import recompute_well

    customer, well, lines = _world(
        db_session, units=(UnitOfMeasure.MTR, UnitOfMeasure.MT)
    )
    recompute_well(db_session, well)

    summary = executive_summary(db_session, customer_id=customer.id)
    horizon = next(h for h in summary.demand_trend.horizons if h.months == 24)

    assert horizon.unit_of_measure is None
    by_unit = {e.unit_of_measure: e.quantity for e in horizon.quantities_by_unit}
    assert by_unit == {UnitOfMeasure.MTR: 1000.0, UnitOfMeasure.MT: 2000.0}
    # The breakdown accounts for the whole scalar -- so nothing was dropped; the
    # scalar is simply not a number anybody may render.
    assert sum(by_unit.values()) == horizon.current_total

    assert horizon.change_pct.available is False
    assert "units of measure" in horizon.change_pct.reason

    # And the reader of the headline is told, at the top level.
    assert MIXED_UNITS_NOTE in summary.notes


def test_soft_allocation_channel_shares_are_withheld_across_mixed_units(db_session):
    """A channel PERCENTAGE of a mixed-unit total is undefined, not imprecise.

    There are now FIVE channels, not four: `from_customer_owned` was added when
    customer-owned inventory landed (it is drawn first for the same product). The
    assertion follows the channel list rather than being loosened -- every channel's
    ratio is still required to be withheld, so widening the list STRENGTHENS what is
    checked by one more ratio.

    REPURPOSED from `test_allocation_share_is_withheld_across_mixed_units`, which
    asserted the same rule about the block that reported Oracle's HARD assignment
    split. That block is gone -- the product owner asked for soft allocation
    coverage instead -- so the assertion follows the rule to its new home rather
    than being deleted. Nothing has stopped being checked: the mixed-unit ratio is
    still refused, the scalar unit still goes null, and the per-unit breakdown still
    carries the real figures. There are now FOUR ratios under the rule instead of
    two, and all of them are asserted.
    """
    from app.engines.coverage import recompute_well
    from app.models import InventoryAssignment

    customer, well, lines = _world(
        db_session, units=(UnitOfMeasure.MTR, UnitOfMeasure.MT)
    )
    recompute_well(db_session, well)
    product, line = lines[0]
    db_session.add(
        InventoryAssignment(
            demand_line_id=line.id, product_id=product.id, quantity=400,
            source_system="synthetic",
        )
    )
    db_session.flush()

    block = executive_summary(
        db_session, customer_id=customer.id, allocation_horizon_months=24
    ).soft_allocation_coverage

    assert block.available is True
    assert block.unit_of_measure is None
    assert len(block.channels) == 5
    assert [channel.pct for channel in block.channels] == [None] * 5
    assert "units of measure" in block.reason
    # The defensible figures are still there, per unit. This customer is SOFT, so
    # every satisfied quantity comes from the shared pool and NOTHING comes from its
    # own assignment -- which is the pooling guarantee, visible here as a number.
    from app.engines.executive import FROM_ASSIGNMENT, FROM_POOL

    by_key = {channel.key: channel for channel in block.channels}
    assert {
        e.unit_of_measure: e.quantity for e in by_key[FROM_POOL].quantities_by_unit
    } == {UnitOfMeasure.MTR: 1000.0, UnitOfMeasure.MT: 2000.0}
    assert by_key[FROM_ASSIGNMENT].quantity == 0.0

    # And the coverage block's own percentage obeys the same rule.
    coverage = executive_summary(db_session, customer_id=customer.id).coverage
    assert coverage.available is True
    assert coverage.unit_of_measure is None
    assert coverage.coverage_pct is None
    assert "units of measure" in coverage.reason


def test_no_conversion_factor_exists_on_the_enum_itself(db_session):
    """There is deliberately no metres-per-tonne factor ON THE ENUM to sum mixed
    units with.

    MVP-COMPROMISE[C-05] narrowed this design commitment rather than removing it:
    `app.engines.units.to_metric_tonnes` now converts FT/MTR/MT through the
    per-product `Product.weight` fact -- see that module's docstring for why a
    PER-PRODUCT measured weight is not the "plausible-looking invention" this
    test used to guard against wholesale, and for the boundary that conversion
    must stay behind (Executive Dashboard presentation only). What remains true,
    and is still asserted here, is that `UnitOfMeasure` ITSELF stores no factor:
    the arithmetic lives in one named module, not on the enum, so it cannot be
    reached from a context that never imported that module and never looked
    would still see the conversion coming.
    """
    assert not hasattr(UnitOfMeasure, "to_base")
    assert not hasattr(UnitOfMeasure, "factor")
    # Five units now: the three the source workbook carries, plus FT/JT added for
    # the metric-tonnes conversion layer. See `app.models.product.UnitOfMeasure`
    # and MVP_COMPROMISES.md C-04/C-05/C-06.
    assert {u.value for u in UnitOfMeasure} == {"Mtr", "PC", "MT", "FT", "JT"}
