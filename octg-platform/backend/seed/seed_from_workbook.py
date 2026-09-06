"""Seed synthetic demo data shaped like the workbook's Safety Stock / Lead-Time
/ By Item tabs. Values here are illustrative examples only -- not AkerBP's
real commercial figures."""

import argparse
import sys
from datetime import datetime, timedelta

from sqlalchemy import inspect

from app.db import Base, SessionLocal, engine
from app.engines.coverage import recompute_customer, recompute_well
from app.engines.substitution import decide_approval, request_approval
from app.models import (
    ANY_ATTRIBUTE_VALUE,
    AllocationPolicy,
    BusinessUnit,
    Customer,
    CustomerOwnedInventory,
    CustomerOwnedInventoryUpload,
    CustomerSubstitutionRule,
    DemandLine,
    DemandProfile,
    DemandRevision,
    DemandStatus,
    InventoryAssignment,
    InventoryOnHand,
    InventoryOnOrder,
    LeadTimeComponent,
    LeadTimeDimension,
    PlanningNode,
    Product,
    Scenario,
    ScenarioOverride,
    ScenarioStatus,
    ScenarioTargetKind,
    TechnicalSubstitution,
    UnitOfMeasure,
    Well,
)


#: Demand status PER WELL -- the authority is `Well.demand_status`, and this is the
#: whole of the demo's spread across Planned / Budgeted / Confirmed.
#:
#: WHY ALMOST EVERY WELL IS CONFIRMED
#: ----------------------------------
#: The status filter now selects WELLS (`app.engines.coverage`), and the platform
#: default is Confirmed only. A demonstrated scenario whose well is not Confirmed is
#: not "a scenario with a different status" -- it is a well the engine does not
#: evaluate at all, with no coverage badge, no MRP row and no verdict to look at. So
#: every well that exists to show a coverage rule is Confirmed, necessarily.
#:
#: WHY THE TWO EXCLUDED WELLS ARE NEW WELLS
#: ----------------------------------------
#: The exclusion still has to be demonstrable, so two wells were ADDED rather than
#: an existing scenario well being demoted:
#:
#:   Well Shelduck-26  BUDGETED   holds the CSG 9-5/8 53.5 P110 line that used to
#:                                sit on Eagle-01 as a Budgeted line. Status is a
#:                                well-level fact now, so a Budgeted line inside a
#:                                Confirmed well is exactly the state that no longer
#:                                exists -- and it was the state that made the
#:                                owner's coverage grid unreadable. Moving the line
#:                                to a well of its own preserves BOTH facts it
#:                                carried: it is out of scope by default (so
#:                                Falcon-04 still draws the whole 3000 CSG pool and
#:                                is still PendingApproval), and switching the status
#:                                filter to all three statuses brings a whole well
#:                                in that TAKES 1500 of that pool -- the
#:                                inventory-freeing property, visible in demo data
#:                                rather than only in a unit test.
#:   Well Merganser-25 PLANNED    a second excluded well on the same product (1200
#:                                @ +18d), so the "before/after" the owner asked for
#:                                moves by TWO whole wells rather than one, and the
#:                                Planned member of the enum is represented too.
#:
#: Demoting a scenario well instead would have deleted a demonstration to make room
#: for this one.
_WELL_DEMAND_STATUS = {
    # -- Norheim Energy (SOFT, BU North) ----------------------------------
    "Well Eagle-01": DemandStatus.CONFIRMED,
    "Well Falcon-04": DemandStatus.CONFIRMED,
    "Well Hawk-07": DemandStatus.CONFIRMED,
    "Well Osprey-09": DemandStatus.CONFIRMED,
    "Well Puffin-12": DemandStatus.CONFIRMED,
    "Well Tern-08": DemandStatus.CONFIRMED,
    "Well Fulmar-13": DemandStatus.CONFIRMED,
    "Well Guillemot-16": DemandStatus.CONFIRMED,
    "Well Razorbill-17": DemandStatus.CONFIRMED,
    "Well Shearwater-18": DemandStatus.CONFIRMED,
    "Well Kittiwake-20": DemandStatus.CONFIRMED,
    "Well Gadwall-22": DemandStatus.CONFIRMED,
    "Well Dunlin-23": DemandStatus.CONFIRMED,
    "Well Auklet-24": DemandStatus.CONFIRMED,
    # The three wells that demonstrate the CUSTOMER-OWNED consumption priority.
    # Confirmed, necessarily: a well the engine does not evaluate has no verdict to
    # demonstrate, and the whole point of these three is the verdict.
    "Well Wigeon-27": DemandStatus.CONFIRMED,
    "Well Pintail-28": DemandStatus.CONFIRMED,
    "Well Garganey-29": DemandStatus.CONFIRMED,
    # The two wells that exist to be EXCLUDED. See the block comment above.
    "Well Merganser-25": DemandStatus.PLANNED,
    "Well Shelduck-26": DemandStatus.BUDGETED,
    # -- Vestfjord Petroleum (HARD, BU North) ----------------------------------
    "Well Kestrel-02": DemandStatus.CONFIRMED,
    "Well Merlin-05": DemandStatus.CONFIRMED,
    "Well Gannet-11": DemandStatus.CONFIRMED,
    "Well Fratercula-19": DemandStatus.CONFIRMED,
    "Well Storm-21": DemandStatus.CONFIRMED,
    # -- Pelican Gulf Drilling (HYBRID, BU Gulf) ---------------------------------
    "Well Petrel-03": DemandStatus.CONFIRMED,
    "Well Skua-06": DemandStatus.CONFIRMED,
    "Well Skerry-14": DemandStatus.CONFIRMED,
}


def _well(planning_node_id, name):
    """Build a Well with the demand status `_WELL_DEMAND_STATUS` states for it.

    Every well in this seed goes through here, so a well added without a stated
    status raises rather than silently defaulting to Planned -- which would put it
    out of the platform's default coverage scope and quietly delete whatever it was
    added to demonstrate.
    """
    try:
        status = _WELL_DEMAND_STATUS[name]
    except KeyError:
        raise SystemExit(
            f"SEED BUG: well {name!r} has no entry in _WELL_DEMAND_STATUS. Demand "
            "status is a required, planner-set property of a well and defaulting it "
            "would decide whether this well is evaluated at all. Add it there, "
            "beside the note explaining the spread."
        )
    return Well(planning_node_id=planning_node_id, name=name, demand_status=status)


def _assign(db, demand_line, product, quantity):
    """Seed one row of the Oracle-owned inventory-assignment projection.

    source_system="synthetic" is deliberate and load-bearing: it is what keeps
    MRP's InventoryPosition.assigned_source honest about this being demo data
    rather than a real Oracle feed.
    """
    db.add(
        InventoryAssignment(
            demand_line_id=demand_line.id,
            product_id=product.id,
            quantity=quantity,
            source_system="synthetic",
            source_reference=None,
            synced_at=None,
        )
    )
    db.flush()


def _on_hand(db, business_unit, product, quantity):
    """Seed one row of the Oracle-owned per-BU on-hand projection.

    source_system="synthetic" for the same reason as `_assign`: this is demo
    data, not an Oracle feed, and the projection must say so.
    """
    db.add(
        InventoryOnHand(
            business_unit_id=business_unit.id,
            product_id=product.id,
            quantity=quantity,
            source_system="synthetic",
            synced_at=None,
        )
    )
    db.flush()


def _on_order(db, business_unit, product, quantity, arrives_in_days=None, booking_status=None):
    """Seed one row of the Oracle-owned per-BU purchase-order projection.

    source_system="synthetic" for the same reason as `_assign` and `_on_hand`: this
    is demo data, not an Oracle feed, and the projection must say so. It is what
    keeps `InventoryPosition.on_order_source` and the Executive Dashboard's
    `incoming_supply.source` honest while `oracle_integrated` stays False.

    `arrives_in_days=None` writes a row with NO `expected_arrival_date`, which is a
    real state (a PO raised but not acknowledged) and is deliberately counted in the
    total while appearing in no arrival horizon.
    """
    db.add(
        InventoryOnOrder(
            business_unit_id=business_unit.id,
            product_id=product.id,
            quantity=quantity,
            expected_arrival_date=(
                datetime.utcnow() + timedelta(days=arrives_in_days)
                if arrives_in_days is not None
                else None
            ),
            # PO'ed vs Booked (workbook distinction). Dated arrivals are seeded
            # as Booked unless stated otherwise; an unacknowledged PO stays
            # PO'ed; None exercises the "feed did not state it" path.
            booking_status=booking_status
            or ("Booked" if arrives_in_days is not None else "PO"),
            source_system="synthetic",
            source_reference=None,
            synced_at=None,
        )
    )
    db.flush()


def _seed_on_order(db, bu_north, tbg, csg, hard_product):
    """Incoming supply, covering all THREE states the dashboard must keep apart.

    On-order was previously absent from the platform entirely -- a hardcoded 0 in
    `app.engines.mrp.InventoryPosition` -- so none of these states was reachable in
    the demo and the "no data is not zero" rule could only be unit-tested. Each one
    is now seeded deliberately:

      MEASURED, POSITIVE  the 13CR TBG (BU North). Two purchase orders, 4000 landing
                          in ~4 months and 3000 in ~8, against the very product Well
                          Hawk-07 and Well Osprey-09 are short of. So the Executive
                          Dashboard's incoming-supply figure is about the same steel
                          the supply-risk figure is about -- which is what makes the
                          two readable side by side. Note it changes NO verdict:
                          coverage is decided from on-hand stock alone, and nothing
                          nets on-order material against demand.
      MEASURED, ZERO      the scarce CSG 9-5/8 53.5 P110 (BU North). An explicit
                          `quantity = 0` row, i.e. the FACT "nothing is on order of
                          this". Falcon-04 is short of exactly this product, so the
                          demo shows a shortfall with a confirmed empty pipeline --
                          the case a planner most needs to tell apart from silence.
      UNKNOWN             every other product in the catalogue, by having no row at
                          all. `incoming_supply.products_with_no_data` counts them
                          and the block says the total is a floor rather than a
                          complete answer.

    Plus one UNDATED row on the HARD customer's CSG 7 29.0: a purchase order with no
    promised arrival date. It is in the total and in no arrival window, which is the
    only honest handling -- dating it to today would make unscheduled steel look
    imminent in every horizon.

    BU Gulf deliberately gets NOTHING. A purchase order into another Business Unit
    is another Business Unit's steel, and the demo should show the boundary holding
    on this screen as it does everywhere else.
    """
    _on_order(db, bu_north, tbg, 4000, arrives_in_days=120)
    _on_order(db, bu_north, tbg, 3000, arrives_in_days=240)
    _on_order(db, bu_north, csg, 0)
    _on_order(db, bu_north, hard_product, 2000, arrives_in_days=None)


def _seed_synthetic_history(db, now):
    """SYNTHETIC DEMO HISTORY, so the demand-trend prior-period comparison renders.

    THE HONEST WAY, AND WHY IT MATTERS
    ---------------------------------
    The Executive Dashboard's prior-period figure is reconstructed from
    `DemandRevision` by `app.engines.executive._state_as_of`, and it reports itself
    UNAVAILABLE unless every in-scope line's state at the comparison date can be
    genuinely rebuilt. Demo databases are built from scratch moments before they are
    looked at, so no vantage point 3 / 6 / 12 / 24 months back has any history behind
    it and every horizon renders "not computable yet" -- which is correct, and which
    shows the product owner nothing about how the screen looks.

    So this function writes REAL HISTORY that the engine reads through its ordinary
    path. It does NOT special-case the engine and it does NOT inject a "previous
    total" anywhere:

      * each demand line's `created_at` is backdated ~30 months, so the line
        genuinely existed at every comparison date;
      * each line gets TWO backdated `DemandRevision` rows -- revision 1 at ~30
        months ago and revision 2 at ~14 months ago -- carrying the quantity, ROS
        date, status and profile the line HAD at those moments;
      * the revisions written at creation are renumbered upwards so the sequence
        still reads 1 -> N in time order and `current_revision_no` still names the
        latest row. The invariant `app.models.demand._write_initial_revision`
        establishes (a revision numbered `current_revision_no` always exists) is
        preserved, not bypassed.

    The engine then does exactly what it does in production: finds each line's latest
    revision at or before the vantage date and totals that book's forward horizon.

    WHY THE TREND IS NOT FLAT
    ------------------------
    Two things vary, on purpose, so the UI's up / down / unchanged rendering is all
    exercised:

      * the historic ROS dates are shifted back with the vantage point, so each
        horizon's prior window catches a DIFFERENT subset of lines than the current
        window does;
      * the historic quantities are scaled by a factor that alternates between lines
        (0.85 and 1.25 for the 14-month vantage, 0.6 for the 30-month one), so some
        horizons come out up and others down rather than all moving one way.

    IT IS DEMO DATA AND SAYS SO
    ---------------------------
    The seed prints that this history is synthetic. It is not a claim that anybody
    revised anything; it is a stand-in for the accumulation that a real deployment
    gets for free by simply running for a year. Nothing about the mechanism is
    special-cased, so the screen a reviewer sees here is the screen a real database
    produces once it has history.
    """
    # ~30 and ~14 months back, as day counts so the arithmetic is obvious.
    long_ago = now - timedelta(days=913)
    mid = now - timedelta(days=426)

    lines = db.query(DemandLine).all()
    for index, line in enumerate(lines):
        # Push the revisions written at creation up by two, so the two synthetic
        # rows below can take numbers 1 and 2 and the sequence stays time-ordered.
        for rev in sorted(line.revisions, key=lambda r: r.revision_no, reverse=True):
            rev.revision_no += 2
        line.current_revision_no += 2
        line.created_at = long_ago

        status = line.well.demand_status
        scale = 0.85 if index % 2 == 0 else 1.25
        for revision_no, created_at, ros_shift_days, quantity in (
            (1, long_ago, 913, round(line.quantity * 0.6, 1)),
            (2, mid, 426, round(line.quantity * scale, 1)),
        ):
            db.add(
                DemandRevision(
                    demand_line_id=line.id,
                    revision_no=revision_no,
                    quantity=quantity,
                    ros_date=line.ros_date - timedelta(days=ros_shift_days),
                    status=status,
                    profile=line.profile,
                    created_at=created_at,
                )
            )
    db.flush()
    return len(lines)


def _seed_bu_boundary_scenario(db, now, bu_north, bu_gulf, soft_customer, hard_customer):
    """The Business Unit boundary, and what HARD withholds inside one.

    One product, three facts:

      * BU North holds 9000 of it; BU Gulf holds 50000 of the SAME product.
      * There is no unscoped quantity anywhere -- `Product.on_hand_qty` no longer
        exists. Every figure in this seed is an `InventoryOnHand(BU, product)`
        row, which is the only thing the coverage engine can read.
      * Norheim Energy (SOFT, BU North) demands 2000. Vestfjord Petroleum (HARD,
        BU North) demands 5000 with NOTHING assigned, so under hard allocation it
        is Uncovered even though the steel is on the shelf -- and it stays
        Uncovered under BU-wide allocation, because a hard line never draws on
        the shared pool. That is the point of the case.

    What it demonstrates on screen:

      * BU Gulf's 50000 of this product is never offered to BU North, and BU
        North's stock is never offered to BU Gulf. The boundary holds in both
        directions -- it is the one wall D01 did not move.
      * Inside BU North the pool is shared, but hard allocation still withholds:
        the recommended action names the Oracle step rather than the shelf.

    This seeded the cross-customer sharing what-if until that analysis was
    retired (D01, 2026-09-06): with the pool divided across the BU there is no
    neighbour's surplus left to ask about. The data is unchanged and still earns
    its place -- it is the demo's only two-BU case.
    """
    product = Product(
        type="CSG", size="10-3/4", weight=60.7, grade="L80", grade_type="Carbon",
        connection="VAM 21", commodity="SMLS",
        description="CSG 10-3/4 60.7 L80 VAM 21 SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add(product)
    db.flush()

    _on_hand(db, bu_north, product, 9000)
    # Ample stock of the very same product in a DIFFERENT BU. The analysis must
    # refuse to offer any of it.
    _on_hand(db, bu_gulf, product, 50000)

    soft_node = (
        db.query(PlanningNode).filter(PlanningNode.customer_id == soft_customer.id).first()
    )
    hard_node = (
        db.query(PlanningNode).filter(PlanningNode.customer_id == hard_customer.id).first()
    )

    # The surplus holder: 2000 of 9000 consumed, 7000 left standing.
    surplus_well = _well(soft_node.id, "Well Puffin-12")
    # The customer that needs help: 5000 required, nothing assigned.
    needy_well = _well(hard_node.id, "Well Gannet-11")
    db.add_all([surplus_well, needy_well])
    db.flush()

    db.add_all([
        DemandLine(
            well_id=surplus_well.id, product_id=product.id, quantity=2000,
            ros_date=now + timedelta(days=410),
            profile=DemandProfile.PRIMARY,
        ),
        DemandLine(
            well_id=needy_well.id, product_id=product.id, quantity=5000,
            ros_date=now + timedelta(days=420),
            profile=DemandProfile.PRIMARY,
        ),
    ])
    db.flush()

    recompute_well(db, surplus_well)
    recompute_well(db, needy_well)
    return product, surplus_well, needy_well


def _seed_two_bu_independence(db, now, bu_north, bu_gulf, north_customer, gulf_customer):
    """ONE product demanded from BOTH Business Units, with different BU quantities.

    This case was ABSENT from the seed, and its absence is why the cross-BU
    on-hand leak stayed invisible for so long. Every other product here happens to
    be demanded from exactly one BU, so a resolver that ignored the BU dimension
    still produced the right screens -- the bug was latent, not firing, and nothing
    in the demo could have shown it.

    The shape (all synthetic):

        Product  TBG 5-1/2 20.0 13CR110 VAM TOP SMLS
        BU North Sea Operations   7000 on hand
        BU Gulf Operations        2000 on hand

        Well Tern-08    (Norheim Energy, BU North) 6000 @ ROS +410d -> Covered
        Well Skerry-14  (Pelican Gulf Drilling, BU Gulf)  6000 @ ROS +410d -> Uncovered

    Identical product, identical quantity, identical ROS, OPPOSITE verdicts --
    and each verdict is decided solely by its own BU's row. That makes the
    boundary demonstrable rather than merely documented: any regression that put a
    single global quantity back in the resolution path would have to make these
    two agree, and one of them would visibly flip.

    Note the two quantities are chosen so NEITHER answer is reachable from the
    other's number: 7000 covers 6000 and 2000 cannot, so a leak in either
    direction changes a badge on screen.
    """
    product = Product(
        type="TBG", size="5-1/2", weight=20.0, grade="13CR110", grade_type="13CR",
        connection="VAM TOP", commodity="SMLS",
        description="TBG 5-1/2 20.0 13CR110 VAM TOP SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add(product)
    db.flush()

    _on_hand(db, bu_north, product, 7000)
    _on_hand(db, bu_gulf, product, 2000)

    north_node = (
        db.query(PlanningNode)
        .filter(PlanningNode.customer_id == north_customer.id)
        .first()
    )
    gulf_node = (
        db.query(PlanningNode)
        .filter(PlanningNode.customer_id == gulf_customer.id)
        .first()
    )

    north_well = _well(north_node.id, "Well Tern-08")
    gulf_well = _well(gulf_node.id, "Well Skerry-14")
    db.add_all([north_well, gulf_well])
    db.flush()

    db.add_all([
        DemandLine(
            well_id=north_well.id, product_id=product.id, quantity=6000,
            ros_date=now + timedelta(days=410),
            profile=DemandProfile.PRIMARY,
        ),
        DemandLine(
            well_id=gulf_well.id, product_id=product.id, quantity=6000,
            ros_date=now + timedelta(days=410),
            profile=DemandProfile.PRIMARY,
        ),
    ])
    db.flush()

    recompute_well(db, north_well)
    recompute_well(db, gulf_well)
    return product, north_well, gulf_well


def _seed_hard_allocation_customer(db, now, business_unit):
    """HARD allocation demo -- coverage follows the physical assignment only.

    Deliberately given its OWN product so that adding this customer cannot
    perturb the SOFT customer's wells or the MRP recommendations built on their
    shared 13CR/Carbon stock.
    """
    customer = Customer(
        name="Vestfjord Petroleum",
        allocation_policy=AllocationPolicy.HARD,
        business_unit_id=business_unit.id,
    )
    db.add(customer)
    db.flush()

    node = PlanningNode(
        customer_id=customer.id, parent_id=None, node_type="Campaign",
        name="Vestfjord 2026 Subsea Tieback",
    )
    db.add(node)
    db.flush()

    # 12000 on hand. Only 4000 of it will ever be assigned, so 8000 sits
    # unassigned in the pool -- visible, plentiful, and useless to a
    # hard-allocated line.
    product = Product(
        type="CSG", size="7", weight=29.0, grade="L80", grade_type="Carbon",
        connection="VAM 21", commodity="SMLS",
        description="CSG 7 29.0 L80 VAM 21 SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add(product)
    db.flush()
    _on_hand(db, business_unit, product, 12000)

    # Kestrel-02: 4000 required, 4000 explicitly assigned -> Covered.
    well_covered = _well(node.id, "Well Kestrel-02")
    # Merlin-05: 5000 required, NOTHING assigned. 8000 unassigned tubulars are
    # physically on the shelf and it is STILL Uncovered. This is the case that
    # distinguishes hard from soft -- under SOFT this well would be Covered.
    well_unassigned = _well(node.id, "Well Merlin-05")
    db.add_all([well_covered, well_unassigned])
    db.flush()

    assigned_line = DemandLine(
        well_id=well_covered.id, product_id=product.id, quantity=4000,
        ros_date=now + timedelta(days=380),
        profile=DemandProfile.PRIMARY,
    )
    unassigned_line = DemandLine(
        well_id=well_unassigned.id, product_id=product.id, quantity=5000,
        ros_date=now + timedelta(days=400),
        profile=DemandProfile.PRIMARY,
    )
    db.add_all([assigned_line, unassigned_line])
    db.flush()

    _assign(db, assigned_line, product, 4000)

    wells = (well_covered, well_unassigned)
    for well in wells:
        recompute_well(db, well)
    return customer, wells, product


def _seed_hybrid_allocation_customer(db, now, business_unit):
    """HYBRID allocation demo -- assigned first, then the unassigned remainder
    of the pool by earliest ROS.

    Own product again, for the same isolation reason as the HARD customer.
    """
    customer = Customer(
        name="Pelican Gulf Drilling",
        allocation_policy=AllocationPolicy.HYBRID,
        business_unit_id=business_unit.id,
    )
    db.add(customer)
    db.flush()

    node = PlanningNode(
        customer_id=customer.id, parent_id=None, node_type="Campaign",
        name="Pelican Gulf 2027 Infill Programme",
    )
    db.add(node)
    db.flush()

    # 6000 on hand, of which 3500 gets assigned -> 2500 unassigned pool.
    product = Product(
        type="TBG", size="3-1/2", weight=9.2, grade="L80", grade_type="Carbon",
        connection="VAM TOP", commodity="SMLS",
        description="TBG 3-1/2 9.2 L80 VAM TOP SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add(product)
    db.flush()
    _on_hand(db, business_unit, product, 6000)

    # Petrel-03: 4000 required, 2500 assigned -> 1500 shortfall drawn from the
    # 2500 unassigned pool -> Covered. The headline hybrid case.
    well_topup = _well(node.id, "Well Petrel-03")
    # Skua-06: 5000 required, 1000 assigned -> 4000 shortfall against a pool
    # that cannot cover it -> Uncovered. Shows the pool is finite and that a
    # partial assignment is not a guarantee.
    well_short = _well(node.id, "Well Skua-06")
    db.add_all([well_topup, well_short])
    db.flush()

    topup_line = DemandLine(
        well_id=well_topup.id, product_id=product.id, quantity=4000,
        ros_date=now + timedelta(days=390),
        profile=DemandProfile.PRIMARY,
    )
    short_line = DemandLine(
        well_id=well_short.id, product_id=product.id, quantity=5000,
        ros_date=now + timedelta(days=430),
        profile=DemandProfile.PRIMARY,
    )
    db.add_all([topup_line, short_line])
    db.flush()

    _assign(db, topup_line, product, 2500)
    _assign(db, short_line, product, 1000)

    wells = (well_topup, well_short)
    for well in wells:
        recompute_well(db, well)
    return customer, wells


def _seed_partial_consumption(db, now, bu_north, soft_customer):
    """The partial-consumption TRADE-OFF, made visible in the demo.

    Own product, so it cannot perturb any verdict above. 2000 on hand, no
    assignments, SOFT:

        Well Fulmar-13     5000 @ ROS +380d   -> draws all 2000, still 3000 short
        Well Guillemot-16  2000 @ ROS +410d   -> draws nothing, Uncovered

    Both wells end Uncovered and the demo shows ZERO covered wells here.

    THAT IS THE POINT. Under the old all-or-nothing rule Fulmar-13 would have left
    the pool alone and Guillemot-16 -- whose requirement the 2000 exactly matches --
    would have been Covered. The owner reversed the rule because physically the
    earlier well gets the pipe, and accepted that the covered-well count can drop.
    Seeding the case means the trade-off is arguable from the screens rather than
    only from a docstring, and Fulmar-13's coverage reason states exactly how much
    it drew and how short it remains.

    Values synthetic.
    """
    product = Product(
        type="CSG", size="13-3/8", weight=68.0, grade="P110", grade_type="Carbon",
        connection="VAM 21", commodity="SMLS",
        description="CSG 13-3/8 68.0 P110 VAM 21 SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add(product)
    db.flush()
    _on_hand(db, bu_north, product, 2000)

    node = (
        db.query(PlanningNode)
        .filter(PlanningNode.customer_id == soft_customer.id)
        .first()
    )
    early_well = _well(node.id, "Well Fulmar-13")
    later_well = _well(node.id, "Well Guillemot-16")
    db.add_all([early_well, later_well])
    db.flush()

    db.add_all([
        DemandLine(
            well_id=early_well.id, product_id=product.id, quantity=5000,
            ros_date=now + timedelta(days=380),
            profile=DemandProfile.PRIMARY,
        ),
        DemandLine(
            well_id=later_well.id, product_id=product.id, quantity=2000,
            ros_date=now + timedelta(days=410),
            profile=DemandProfile.PRIMARY,
        ),
    ])
    db.flush()
    recompute_well(db, early_well)
    return product, early_well, later_well


def _customer_owned_upload(db, customer, filename, row_count, error_count=0):
    """Seed the UPLOAD record for a customer's customer-owned inventory.

    Its existence is not bookkeeping. It is the FACT that a position has been
    declared, which is what lets `app.engines.inventory.customer_owned_map` answer
    "this customer owns none of product P" instead of "nobody has ever told us" -- see
    `app.models.customer_owned_inventory.CustomerOwnedInventoryUpload`. Seeding
    positions without it would leave the demo unable to reach the MEASURED-zero state
    at all.

    `source_system="customer-upload"` rather than "synthetic", deliberately: this is
    the one inventory table the PLATFORM owns, so "the row came from a spreadsheet" is
    the honest provenance and there is no Oracle feed for it to be pretending to be.
    The `filename` says loudly that the file is a demo one.
    """
    upload = CustomerOwnedInventoryUpload(
        customer_id=customer.id,
        filename=filename,
        sheet_name="AkerBP Owned Surplus List",
        row_count=row_count,
        applied_count=row_count - error_count,
        created_count=row_count - error_count,
        replaced_count=0,
        error_count=error_count,
        source_system="customer-upload",
    )
    db.add(upload)
    db.flush()
    return upload


def _customer_owned(db, customer, product, quantity, upload, days_ago=45):
    """Seed one declared customer-owned position.

    `uploaded_at` is backdated, on purpose. This data is only as current as the last
    spreadsheet somebody sent, and a demo where every position was counted "just now"
    would hide the one property a planner most needs to see about it. 45 days is a
    plausible age for a stock count nobody has refreshed.
    """
    db.add(
        CustomerOwnedInventory(
            customer_id=customer.id,
            product_id=product.id,
            quantity=quantity,
            source_system="customer-upload",
            source_reference=upload.filename,
            uploaded_at=datetime.utcnow() - timedelta(days=days_ago),
            upload_id=upload.id,
        )
    )
    db.flush()


def _seed_customer_owned_priority(db, now, bu_north, soft_customer):
    """THE consumption-priority rule, made visible in the demo rather than only tested.

    The product owner's ruling: for the same product, consuming customer-owned
    inventory takes priority over FIFO -- customer-owned inventory is
    consumed BEFORE company inventory.

    A rule whose only observable effect is a passing unit test is a rule nobody can
    argue with from the screens, so this helper seeds the case where THE DRAW ORDER
    DECIDES THE OUTCOME. Two products, both owned by the SOFT customer, both with
    their own wells so nothing above can move.

    CASE 1 -- the priority CHANGES the answer for two whole wells
    ------------------------------------------------------------
        CSG 11-3/4 65.0 P110    company on hand   2000  (BU North)
                                customer owns     3000

        Well Wigeon-27   3000 @ ROS +370d   (earlier)
        Well Pintail-28  2000 @ ROS +400d

      WITH the rule (what the platform does):
        Wigeon-27 draws 3000 from the CUSTOMER'S OWN stock -> Covered, and it touches
        the company pool NOT AT ALL. Pintail-28 finds the customer tier empty and
        draws its whole 2000 from the untouched company pool -> Covered.
        RESULT: 2 wells covered, 0 company steel wasted.

      WITHOUT the rule (company-owned first, or a single undifferentiated pool of
      2000 -- which is what this platform did before):
        Wigeon-27 draws all 2000 of the company pool in ROS order and is STILL 1000
        short -> Uncovered (partial consumption is real; see
        `app.engines.allocation`). Pintail-28 then finds nothing -> Uncovered.
        RESULT: 0 wells covered.

      So the tier is worth exactly two wells here, and the mechanism is visible on the
      screens: Wigeon-27's coverage came entirely from customer-owned stock while the
      whole 2000 company pool is still standing there for the later well. That "the
      company-owned remainder is left intact" property is the half of the rule that is
      easy to get wrong and impossible to see from a covered/uncovered badge alone --
      the Executive Dashboard's `from_customer_owned` channel is where it reads.

    CASE 2 -- SHORT AFTER drawing customer-owned stock
    -------------------------------------------------
        TBG 3-1/2 9.3 L80       company on hand      0  (an explicit 0 row: the FACT
                                                        that BU North holds none)
                                customer owns     1000

        Well Garganey-29  2500 @ ROS +430d  -> draws all 1000 of its own material,
                                               still short by 1500 -> Uncovered

      This exists for the coverage REASON text. A planner who knows their own 1000
      metres were on the dock and reads "insufficient on-hand inventory" will conclude
      the platform ignored their upload. Garganey-29's reason says instead that the
      customer-owned stock was drawn FIRST, how much of it there was, and what remains
      short -- which is the difference between a planner trusting the number and
      phoning about it.

    Every value synthetic -- NOT AkerBP figures.
    """
    priority = Product(
        type="CSG", size="11-3/4", weight=65.0, grade="P110", grade_type="Carbon",
        connection="VAM 21", commodity="SMLS",
        description="CSG 11-3/4 65.0 P110 VAM 21 SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    shortfall = Product(
        type="TBG", size="3-1/2", weight=9.3, grade="L80", grade_type="Carbon",
        connection="VAM TOP", commodity="SMLS",
        description="TBG 3-1/2 9.3 L80 VAM TOP SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add_all([priority, shortfall])
    db.flush()
    _on_hand(db, bu_north, priority, 2000)
    # An explicit ZERO on-hand row, not an absent one: "BU North holds none of this"
    # is a fact, and an absent row would make the whole pass raise
    # `InventoryRowMissing` (unknown is not zero -- see `app.engines.inventory`).
    _on_hand(db, bu_north, shortfall, 0)

    upload = _customer_owned_upload(
        db,
        soft_customer,
        filename="norheim_owned_inventory_declaration.xlsx",
        row_count=2,
    )
    _customer_owned(db, soft_customer, priority, 3000, upload)
    _customer_owned(db, soft_customer, shortfall, 1000, upload)

    node = (
        db.query(PlanningNode)
        .filter(PlanningNode.customer_id == soft_customer.id)
        .first()
    )
    wigeon = _well(node.id, "Well Wigeon-27")
    pintail = _well(node.id, "Well Pintail-28")
    garganey = _well(node.id, "Well Garganey-29")
    db.add_all([wigeon, pintail, garganey])
    db.flush()

    db.add_all([
        DemandLine(
            well_id=wigeon.id, product_id=priority.id, quantity=3000,
            ros_date=now + timedelta(days=370),
            profile=DemandProfile.PRIMARY,
        ),
        DemandLine(
            well_id=pintail.id, product_id=priority.id, quantity=2000,
            ros_date=now + timedelta(days=400),
            profile=DemandProfile.PRIMARY,
        ),
        DemandLine(
            well_id=garganey.id, product_id=shortfall.id, quantity=2500,
            ros_date=now + timedelta(days=430),
            profile=DemandProfile.PRIMARY,
        ),
    ])
    db.flush()
    recompute_well(db, wigeon)
    return priority, shortfall, wigeon, pintail, garganey


def _seed_oversubscribed_pending_substitute(db, now, bu_north, soft_customer):
    """A pending substitute that CANNOT satisfy every line pending on it.

    A pending substitute reserves nothing (the platform never creates a hard
    reservation), so both lines below are told the substitute could close their gap
    -- which is true of each individually and false of both together. The
    over-subscription is therefore stated on BOTH coverage reasons and in the
    substitution-candidate payload.

        primary   TBG 2-7/8 6.5 L80    0 on hand   -> neither line has own stock
        substitute TBG 2-7/8 7.9 L80  4000 on hand -> enough for ONE of them

        Well Razorbill-17  3000 @ ROS +360d  -> PendingApproval
        Well Shearwater-18 3000 @ ROS +390d  -> PendingApproval
        combined requirement 6000 vs 4000 available -> short by 2000

    Layers 1 and 2 clear; layer 3 is deliberately never requested, which is the
    PendingApproval path. Values synthetic.
    """
    primary = Product(
        type="TBG", size="2-7/8", weight=6.5, grade="L80", grade_type="Carbon",
        connection="VAM TOP", commodity="SMLS",
        description="TBG 2-7/8 6.5 L80 VAM TOP SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    substitute = Product(
        type="TBG", size="2-7/8", weight=7.9, grade="L80", grade_type="Carbon",
        connection="VAM TOP", commodity="SMLS",
        description="TBG 2-7/8 7.9 L80 VAM TOP SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add_all([primary, substitute])
    db.flush()
    _on_hand(db, bu_north, primary, 0)
    _on_hand(db, bu_north, substitute, 4000)

    db.add(TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id))
    db.add(
        CustomerSubstitutionRule(
            customer_id=soft_customer.id, from_product_id=primary.id,
            to_product_id=substitute.id, allowed=True,
        )
    )
    db.flush()

    node = (
        db.query(PlanningNode)
        .filter(PlanningNode.customer_id == soft_customer.id)
        .first()
    )
    well_a = _well(node.id, "Well Razorbill-17")
    well_b = _well(node.id, "Well Shearwater-18")
    db.add_all([well_a, well_b])
    db.flush()
    db.add_all([
        DemandLine(
            well_id=well_a.id, product_id=primary.id, quantity=3000,
            ros_date=now + timedelta(days=360),
            profile=DemandProfile.PRIMARY,
        ),
        DemandLine(
            well_id=well_b.id, product_id=primary.id, quantity=3000,
            ros_date=now + timedelta(days=390),
            profile=DemandProfile.PRIMARY,
        ),
    ])
    db.flush()
    recompute_well(db, well_a)
    return primary, substitute, well_a, well_b


def _seed_oracle_release_blocked_substitute(
    db, now, bu_north, soft_customer, hard_customer
):
    """A fully approved substitute that the platform still may not take.

    Every unit of the substitute is hard-assigned to ANOTHER CUSTOMER's demand line
    in the same Business Unit. The stock is on the shelf and all three substitution
    layers clear, so it IS offered as a candidate -- but it can never produce
    CoveredViaSubstitute, because releasing a hard reservation is not something this
    platform does. The recommended action names the Oracle step:
    "the user must go into Oracle themselves and release the hard assignment".

        primary    CSG 8-5/8 36.0 L80    0 on hand
        substitute CSG 8-5/8 40.0 L80 5000 on hand, ALL 5000 assigned to
                   Vestfjord Petroleum's Well Fratercula-19

        Well Kittiwake-20 (Norheim Energy, SOFT) 4000 @ ROS +370d
            -> Uncovered, blocking_layer "hard-assigned-elsewhere"

    Norheim Energy is SOFT on purpose: that is precisely the case where the
    platform used to report CoveredViaSubstitute off somebody else's reserved
    steel, because soft allocation ignores assignment rows. Values synthetic.
    """
    primary = Product(
        type="CSG", size="8-5/8", weight=36.0, grade="L80", grade_type="Carbon",
        connection="VAM 21", commodity="SMLS",
        description="CSG 8-5/8 36.0 L80 VAM 21 SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    substitute = Product(
        type="CSG", size="8-5/8", weight=40.0, grade="L80", grade_type="Carbon",
        connection="VAM 21", commodity="SMLS",
        description="CSG 8-5/8 40.0 L80 VAM 21 SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add_all([primary, substitute])
    db.flush()
    _on_hand(db, bu_north, primary, 0)
    _on_hand(db, bu_north, substitute, 5000)

    db.add(TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id))
    db.add(
        CustomerSubstitutionRule(
            customer_id=soft_customer.id, from_product_id=primary.id,
            to_product_id=substitute.id, allowed=True,
        )
    )
    db.flush()

    # The other customer's claim on the substitute stock.
    hard_node = (
        db.query(PlanningNode)
        .filter(PlanningNode.customer_id == hard_customer.id)
        .first()
    )
    claim_well = _well(hard_node.id, "Well Fratercula-19")
    db.add(claim_well)
    db.flush()
    claim_line = DemandLine(
        well_id=claim_well.id, product_id=substitute.id, quantity=5000,
        ros_date=now + timedelta(days=450),
        profile=DemandProfile.PRIMARY,
    )
    db.add(claim_line)
    db.flush()
    _assign(db, claim_line, substitute, 5000)

    soft_node = (
        db.query(PlanningNode)
        .filter(PlanningNode.customer_id == soft_customer.id)
        .first()
    )
    blocked_well = _well(soft_node.id, "Well Kittiwake-20")
    db.add(blocked_well)
    db.flush()
    blocked_line = DemandLine(
        well_id=blocked_well.id, product_id=primary.id, quantity=4000,
        ros_date=now + timedelta(days=370),
        profile=DemandProfile.PRIMARY,
    )
    db.add(blocked_line)
    db.flush()
    # All three layers clear, including the well-level approval -- so the ONLY
    # thing standing in the way is the hard assignment.
    approval = request_approval(db, blocked_line, primary.id, substitute.id)
    decide_approval(db, approval.id, approved=True)

    recompute_well(db, blocked_well)
    recompute_well(db, claim_well)
    return primary, substitute, blocked_well, claim_well


def _seed_soft_blocked_by_foreign_assignment(
    db, now, bu_north, soft_customer, hard_customer
):
    """A SOFT line short ONLY because ANOTHER CUSTOMER holds a hard assignment.

    The last hole where the platform silently overrode an Oracle hard assignment,
    made visible in the demo. A soft customer's own-product allocation used to
    ignore assignment rows outright, so this line came out COVERED off steel
    reserved to somebody else's demand -- the platform telling a planner "you are
    covered" with material that is spoken for.

        product   CSG 5-1/2 23.0 L80 VAM 21   6000 on hand in BU North

        Well Storm-21  (Vestfjord Petroleum, HARD) 6000 @ ROS +440d
            6000 hard-assigned to it -> Covered. This is the Oracle fact.
        Well Gadwall-22 (Norheim Energy, SOFT) 4000 @ ROS +420d
            -> Uncovered. 6000 tubulars are on the shelf and every one of them is
               reserved to Storm-21, so none is free. The reason NAMES the
               assignment rather than blaming the shelf, and -- because releasing
               it genuinely would close the 4000 gap -- carries the same Oracle
               release action the substitute path uses.

    Contrast this deliberately with Merlin-05 above. Merlin-05 is Uncovered beside
    8000 UNASSIGNED tubulars because its own policy is HARD. Gadwall-22 is
    Uncovered beside 6000 ASSIGNED tubulars although its own policy is SOFT. The
    two together are the whole rule: what a line may draw on depends on its own
    policy for UNASSIGNED stock, and on Oracle alone for ASSIGNED stock.

    And note what is NOT demonstrated here, because it must not be: a soft
    customer's OWN assignment. Norheim Energy holds none anywhere in this seed,
    and if it did it would change none of its verdicts -- pooling means "do not tie
    my steel to specific wells of mine". Only FOREIGN assignments bite.

    Own product, so this helper cannot perturb any verdict above. Values synthetic.
    """
    product = Product(
        type="CSG", size="5-1/2", weight=23.0, grade="L80", grade_type="Carbon",
        connection="VAM 21", commodity="SMLS",
        description="CSG 5-1/2 23.0 L80 VAM 21 SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add(product)
    db.flush()
    _on_hand(db, bu_north, product, 6000)

    hard_node = (
        db.query(PlanningNode)
        .filter(PlanningNode.customer_id == hard_customer.id)
        .first()
    )
    claim_well = _well(hard_node.id, "Well Storm-21")
    db.add(claim_well)
    db.flush()
    claim_line = DemandLine(
        well_id=claim_well.id, product_id=product.id, quantity=6000,
        ros_date=now + timedelta(days=440),
        profile=DemandProfile.PRIMARY,
    )
    db.add(claim_line)
    db.flush()
    _assign(db, claim_line, product, 6000)

    soft_node = (
        db.query(PlanningNode)
        .filter(PlanningNode.customer_id == soft_customer.id)
        .first()
    )
    blocked_well = _well(soft_node.id, "Well Gadwall-22")
    db.add(blocked_well)
    db.flush()
    db.add(
        DemandLine(
            well_id=blocked_well.id, product_id=product.id, quantity=4000,
            ros_date=now + timedelta(days=420),
            profile=DemandProfile.PRIMARY,
        )
    )
    db.flush()

    recompute_well(db, claim_well)
    recompute_well(db, blocked_well)
    return product, blocked_well, claim_well


def _seed_insufficient_inventory_substitute(db, now, bu_north, soft_customer):
    """An APPROVED substitute blocked purely because the steel is not there.

    The counterpart to `_seed_oracle_release_blocked_substitute`, and the reason
    both exist: `blocking_layer` distinguishes two quantity answers that send a
    planner to two DIFFERENT places, and until this case existed only one of them
    could be produced by real data. Kittiwake-20 says "the steel is on the dock,
    go to Oracle and release it"; Dunlin-23 says "the steel does not exist, go to
    the mill". A UI that rendered them identically would be wrong and nothing in
    the demo could have shown it.

        primary    CSG 10-3/4 55.5 L80 VAM 21     0 on hand
        substitute CSG 10-3/4 65.7 L80 VAM 21   500 on hand, NONE of it assigned

        Well Dunlin-23 (Norheim Energy, SOFT) 4000 @ ROS +370d
            -> Uncovered, blocking_layer "insufficient-inventory"

    Three details are load-bearing:

      * The well approval is APPROVED, not Pending. `find_candidates` reports one
        blocking layer in the priority order
        customer > well-approval > hard-assigned-elsewhere > insufficient-inventory,
        so a Pending approval would mask the inventory answer entirely and the line
        would come out PendingApproval instead.
      * The substitute has 500 on hand, not 0 -- an explicit row saying "this BU
        holds 500", short of the 4000 required. That proves the layer is decided by
        a QUANTITY COMPARISON rather than by an absent row (an absent row raises
        `InventoryRowMissing`, which is a different honesty path entirely).
      * NOTHING is assigned to the substitute, so `hard_assigned_qty` is 0 and
        `release_would_close` is false. That is exactly what makes
        insufficient-inventory the honest answer rather than the strictly more
        informative hard-assigned-elsewhere.

    ROS is +370d, the SAME date as Kittiwake-20, so the two sit side by side on any
    ROS-ordered screen. Own primary and own substitute, so this helper cannot
    perturb any verdict above. Values synthetic.
    """
    primary = Product(
        type="CSG", size="10-3/4", weight=55.5, grade="L80", grade_type="Carbon",
        connection="VAM 21", commodity="SMLS",
        description="CSG 10-3/4 55.5 L80 VAM 21 SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    substitute = Product(
        type="CSG", size="10-3/4", weight=65.7, grade="L80", grade_type="Carbon",
        connection="VAM 21", commodity="SMLS",
        description="CSG 10-3/4 65.7 L80 VAM 21 SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add_all([primary, substitute])
    db.flush()
    _on_hand(db, bu_north, primary, 0)
    # Present, countable, and nowhere near enough.
    _on_hand(db, bu_north, substitute, 500)

    db.add(TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id))
    db.add(
        CustomerSubstitutionRule(
            customer_id=soft_customer.id, from_product_id=primary.id,
            to_product_id=substitute.id, allowed=True,
        )
    )
    db.flush()

    soft_node = (
        db.query(PlanningNode)
        .filter(PlanningNode.customer_id == soft_customer.id)
        .first()
    )
    blocked_well = _well(soft_node.id, "Well Dunlin-23")
    db.add(blocked_well)
    db.flush()
    blocked_line = DemandLine(
        well_id=blocked_well.id, product_id=primary.id, quantity=4000,
        ros_date=now + timedelta(days=370),
        profile=DemandProfile.PRIMARY,
    )
    db.add(blocked_line)
    db.flush()
    # Layers 1-3 ALL clear. Only the quantity is missing.
    approval = request_approval(db, blocked_line, primary.id, substitute.id)
    decide_approval(db, approval.id, approved=True)

    recompute_well(db, blocked_well)
    return primary, substitute, blocked_well


def _seed_covered_via_substitute(db, now, bu_north, soft_customer):
    """Substitution actually WORKING -- the one substitution outcome the demo lacked.

    Every other substitution case here stops short of success: Falcon-04 sits at
    PendingApproval (deliberately, to show the pending state), Kittiwake-20 is
    blocked pending an Oracle release, Dunlin-23 is blocked because the steel does
    not exist, and Razorbill-17/Shearwater-18 are over-subscribed. So
    `CoverageStatus.COVERED_VIA_SUBSTITUTE` -- one of the five coverage statuses,
    and the outcome the whole substitution engine exists to reach -- never appeared
    in seeded data at all.

    Two things were therefore undemonstrable, both of which a planner relies on:

      * A well that is Covered *by something other than what it ordered*. Its
        rollup counts as covered, and the reason names the substitute.
      * `CoverageResult.fulfilled_by_product_id` pointing somewhere other than the
        line's own product, which is what makes the runout projection charge the
        consumption to the steel that was really drawn. Without a case like this,
        the substitute's By Item page never lists a demand line belonging to a
        DIFFERENT product -- the exact situation `ByItemDemandLineOut.product_id`
        exists to disambiguate, and the exact situation where assuming every listed
        row ordered the page's own product would mislead someone into committing
        the same steel twice.

        primary    TBG 3-1/2 10.2 13CR80 VAM TOP      0 on hand
        substitute TBG 3-1/2 12.95 13CR80 VAM TOP  7000 on hand, none assigned

        Well Auklet-24 (Norheim Energy, SOFT) 4000 @ ROS +380d
            -> CoveredViaSubstitute, and the WELL rolls up Covered

    Layers 1-3 all clear AND the quantity is there, which is what separates this
    from Dunlin-23: identical shape, opposite outcome, so the pair shows what the
    approval chain is actually for. Own primary and own substitute, so this helper
    cannot move a verdict above. Values synthetic.
    """
    primary = Product(
        type="TBG", size="3-1/2", weight=10.2, grade="13CR80", grade_type="13CR",
        connection="VAM TOP", commodity="SMLS",
        description="TBG 3-1/2 10.2 13CR80 VAM TOP SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    substitute = Product(
        type="TBG", size="3-1/2", weight=12.95, grade="13CR80", grade_type="13CR",
        connection="VAM TOP", commodity="SMLS",
        description="TBG 3-1/2 12.95 13CR80 VAM TOP SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add_all([primary, substitute])
    db.flush()
    _on_hand(db, bu_north, primary, 0)
    # Enough, and unencumbered -- nothing assigned to it, so no Oracle release is
    # in the way. This is the case where substitution simply works.
    _on_hand(db, bu_north, substitute, 7000)

    db.add(TechnicalSubstitution(from_product_id=primary.id, to_product_id=substitute.id))
    db.add(
        CustomerSubstitutionRule(
            customer_id=soft_customer.id, from_product_id=primary.id,
            to_product_id=substitute.id, allowed=True,
        )
    )
    db.flush()

    soft_node = (
        db.query(PlanningNode)
        .filter(PlanningNode.customer_id == soft_customer.id)
        .first()
    )
    well = _well(soft_node.id, "Well Auklet-24")
    db.add(well)
    db.flush()
    line = DemandLine(
        well_id=well.id, product_id=primary.id, quantity=4000,
        ros_date=now + timedelta(days=380),
        profile=DemandProfile.PRIMARY,
    )
    db.add(line)
    db.flush()
    approval = request_approval(db, line, primary.id, substitute.id)
    decide_approval(db, approval.id, approved=True)

    recompute_well(db, well)
    return primary, substitute, well


def _seed_status_excluded_wells(db, now, campaign, csg):
    """The two wells the DEFAULT status filter leaves out -- Planned and Budgeted.

    Why this exists as its own helper, and why the wells are NEW
    -----------------------------------------------------------
    Demand status is a property of the WELL, so the status filter takes or leaves a
    whole well. Every well that demonstrates a coverage rule therefore has to be
    Confirmed or it has no verdict to demonstrate -- which would have left the demo
    with no excluded well at all, and the exclusion is half of what the filter does.

    Two wells were ADDED rather than an existing scenario well demoted:

        Well Shelduck-26  BUDGETED   1500 of CSG 9-5/8 53.5 P110 @ ROS +15d
        Well Merganser-25 PLANNED    1200 of the same product   @ ROS +18d

    Shelduck-26's line is the one that used to sit on Eagle-01 as a Budgeted line
    inside a Confirmed well -- exactly the state that no longer exists, and exactly
    the state that produced the unreadable coverage grid the product owner reported
    (filtering to Confirmed dropped a LINE and left its WELL). Moving it to a well
    of its own preserves both facts it was carrying:

      * out of default scope, so Falcon-04 still draws the WHOLE 3000 CSG pool and
        is still PendingApproval for the same reason as before -- no verdict above
        moved;
      * switching the Coverage Workspace to all three statuses brings TWO whole
        wells into scope which TAKE 2700 of that 3000 pool ahead of Falcon-04 (ROS
        +15d and +18d against Falcon-04's +20d). So "excluding a well frees its
        inventory for other wells" is visible in demo data and not only in a unit
        test. Falcon-04 stays PendingApproval either way -- its approved-pending
        substitute holds 6000, which covers its 4000 regardless -- so widening the
        filter demonstrates the arithmetic without breaking a scenario.

    They use the EXISTING csg product on purpose: the point is competition for a
    scarce pool, and a well with its own amply-stocked product would compete for
    nothing and demonstrate nothing.

    Values synthetic.
    """
    budgeted = _well(campaign.id, "Well Shelduck-26")
    planned = _well(campaign.id, "Well Merganser-25")
    db.add_all([budgeted, planned])
    db.flush()
    db.add_all([
        DemandLine(
            well_id=budgeted.id, product_id=csg.id, quantity=1500,
            ros_date=now + timedelta(days=15),
            profile=DemandProfile.PRIMARY,
        ),
        DemandLine(
            well_id=planned.id, product_id=csg.id, quantity=1200,
            ros_date=now + timedelta(days=18),
            profile=DemandProfile.PRIMARY,
        ),
    ])
    db.flush()
    # No recompute here on purpose: both wells are out of the default scope, so the
    # pass would only confirm they have no verdict. `_seed_well_programmes` runs a
    # pool-wide recompute per customer at the end regardless.
    return budgeted, planned


def _seed_unmodelled_lead_time_product(db, bu_north):
    """A catalogue product whose lead time is NOT MODELLED.

    Every other product in this seed resolves all four required dimensions, so the
    `modelled = False` branch of `app.engines.lead_time.resolve_lead_time` -- the
    one that must render as "not modelled" and never as `0 mo`, because 0 reads as
    instant delivery -- had never been produced by real data.

    HOW it is unmodelled matters. The OD/WT and Logistics dimensions both carry a
    "*" wildcard row, so they can never be missed; Grade and Connection carry no
    wildcard by design. This product therefore uses a CONNECTION that the seeded
    component set does not list -- "Hydril 563" -- and nothing else about it is
    unusual:

        OD/WT     "7 32"      -> matches the "*" row          (3.0)
        Grade     "Carbon"    -> matches the Carbon row       (0.5)
        Connection "Hydril 563" -> NO ROW, NO WILDCARD        MISSING
        Logistics "*"         -> matches the Sailing row      (2.0)

        => total_months 0.0, modelled False, missing_dimensions ("Connection",),
           matched_months 5.5 reported for information and deliberately NOT
           totalled.

    CATALOGUE-ONLY: no well demands it. That is a deliberate choice, not an
    omission.

    An unmodelled lead time means coverage can never call the line UNRECOVERABLE
    (absent data means "cannot judge", not "hopeless" --
    `app.engines.order_dates.is_recoverable`). So a demanded copy of this product
    would have to be either Covered (in which case the lead time is never consulted
    and nothing is demonstrated) or Uncovered-with-an-indicative-order-date, which
    would put a well on the Uncovered list and an MRP row on the summary screen
    whose recommended order date is explicitly not to be trusted -- a number a
    planner could act on by mistake. `GET /mrp/lead-time/{product_id}` answers for
    a product with no demand at all, and that is precisely the case that endpoint
    documents itself as existing for, so the honesty path is reachable without
    planting a misleading date on a planning screen.

    It DOES get an InventoryOnHand row (0 in BU North -- an explicit "this BU holds
    none of it"), so `GET /mrp/by-item/{product_id}` answers too and the
    not-modelled breakdown is visible on the By Item screen beside a real runout
    curve. Keeping the unmodelled-lead-time case and the missing-inventory-row case
    in two SEPARATE products is what lets either be observed without the other.

    Values synthetic.
    """
    product = Product(
        type="CSG", size="7", weight=32.0, grade="L80", grade_type="Carbon",
        connection="Hydril 563", commodity="SMLS",
        description="CSG 7 32.0 L80 Hydril 563 SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add(product)
    db.flush()
    _on_hand(db, bu_north, product, 0)
    return product


def _seed_unstocked_product(db):
    """A catalogue product with NO InventoryOnHand row in ANY Business Unit.

    The 424 path. `app.engines.inventory.total_on_hand_all_bus` raises
    `InventoryRowMissing` for it -- unknown is not zero, and an MRP runout curve
    opening at a fabricated 0 is exactly the confident wrong number the rule
    exists to prevent -- so `GET /mrp/by-item/{product_id}` returns 424 Failed
    Dependency while `GET /mrp/lead-time/{product_id}` still returns 200, because
    a lead time is knowable with no inventory data at all.

    Its lead time is deliberately COMPLETE (4-1/2 15.1 -> "*" 3.0 + 13CR 1.0 +
    VAM TOP 0.5 + Sailing 2.0 = 6.5 months, modelled), so the two honesty paths
    stay independent: this product proves the inventory refusal alone, and the
    `modelled = False` refusal is proved by its own product elsewhere.

    CATALOGUE-ONLY, and this one is not a preference but a requirement. A product
    with no inventory row that is DEMANDED by an in-scope line makes
    `compute_customer_coverage` raise `InventoryRowMissing` for that customer's
    entire pool, taking down every screen that shows them -- the coverage grid, the
    home dashboard, the customer's whole pass. That is a landmine, not a demo.
    Nothing demands it, so nothing depends on the quantity that is unknown.

    Deliberately NO `_on_hand` call here. Its absence IS the scenario, so do not
    "fix" it by adding a row -- adding one silently deletes the only 424 case in
    the demo data. Values synthetic.
    """
    product = Product(
        type="TBG", size="4-1/2", weight=15.1, grade="13CR110", grade_type="13CR",
        connection="VAM TOP", commodity="SMLS",
        description="TBG 4-1/2 15.1 13CR110 VAM TOP SMLS",
        unit_of_measure=UnitOfMeasure.MTR,
    )
    db.add(product)
    db.flush()
    return product


def _seed_scenarios(db, now, customer, hawk_line, osprey_line, tbg, bu_north):
    """Three SHARED scenarios against the existing demo data -- Phase 4.

    Shared is the point: no owner, no visibility field, anyone with project
    access sees and reviews all three. `created_by` is attribution for the UI.

    None of them is applied. They exist so the Scenario List and Scenario Editor
    have real before/after arithmetic to render, and between them they exercise
    all three headings the spec asks for.

    The demand pool they act on (Norheim Energy's 13CR TBG, 8000 on hand):

        Eagle-01   5000 @ ROS +30d   Covered      (earliest ROS wins the pool)
        Osprey-09  9000 @ ROS +60d   Unrecoverable (inside the 6.5-month lead time)
        Hawk-07    9000 @ ROS +300d  Uncovered    (3000 left in the pool, needs 9000)

    1. "Hawk-07 scope reduction" (Draft) -- COVERAGE IMPACT.
       Hawk-07's 9000 drops to 3000, exactly the pool remainder. The well FLIPS
       Uncovered -> Covered and its MRP row disappears. This is the scenario that
       demonstrably moves a well, and being a pure demand override it is also
       APPLICABLE, so the "Apply to base plan" path can be walked end to end in
       the demo.

    2. "Osprey-09 rig slips to next year" (Review) -- RISK IMPACT.
       An ROS push-out from +60d to +430d. The well stays Uncovered, so the
       coverage headline barely moves -- but the line stops being UNRECOVERABLE,
       because a mill order placed today can now physically make the date. That
       is the distinction the Risk panel exists to show, and it is invisible on
       the coverage badge alone.

    3. "Merlin-05: assign the unassigned stock" (Discussion) -- THE ORACLE
       BOUNDARY, shown rather than described.
       Merlin-05 is Uncovered under HARD allocation even though 8000 unassigned
       tubulars sit on the shelf, because hard allocation covers a line only from
       inventory explicitly assigned to it. An ASSIGNMENT override of 5000 flips
       it Uncovered -> Covered in the preview.

       And then the editor shows it as NOT APPLICABLE, because InventoryAssignment
       is a read-only projection of Oracle-owned data. Seeded on purpose: the
       refusal is a real product decision and it should be visible in the demo
       rather than only in a docstring. Both halves matter -- the question is
       answerable, the answer just cannot be written here.

    Values synthetic throughout.
    """
    reduce_hawk = Scenario(
        name="Hawk-07 scope reduction",
        description=(
            "Customer asked what happens if Hawk-07 is drilled with a shorter "
            "string. Reducing the 13CR tubing requirement from 9000 to 3000 fits "
            "the tubing left in the pool after Eagle-01 and Osprey-09."
        ),
        business_unit_id=customer.business_unit_id,
        status=ScenarioStatus.DRAFT,
        created_by="demo.planner",
    )
    slip_osprey = Scenario(
        name="Osprey-09 rig slips to next year",
        description=(
            "Rig availability moved. Does pushing Osprey-09's ROS out take it out "
            "of the unrecoverable bucket? (It is unrecoverable today because the "
            "ROS sits inside the 6.5-month 13CR lead time.)"
        ),
        business_unit_id=customer.business_unit_id,
        status=ScenarioStatus.REVIEW,
        created_by="demo.planner",
    )
    db.add_all([reduce_hawk, slip_osprey])
    db.flush()

    db.add(
        ScenarioOverride(
            scenario_id=reduce_hawk.id,
            target_kind=ScenarioTargetKind.DEMAND_LINE,
            target_demand_line_id=hawk_line.id,
            field_name="quantity",
            value_number=3000,
            note="Shorter completion string agreed on the call.",
        )
    )
    db.add(
        ScenarioOverride(
            scenario_id=slip_osprey.id,
            target_kind=ScenarioTargetKind.DEMAND_LINE,
            target_demand_line_id=osprey_line.id,
            field_name="ros_date",
            value_date=now + timedelta(days=430),
            note="Rig now expected 14 months out.",
        )
    )
    db.flush()
    return reduce_hawk, slip_osprey


def _seed_supply_scenario(db, hard_customer, merlin_well, merlin_product):
    """The Oracle-boundary scenario -- see `_seed_scenarios` docstring, case 3.

    `merlin_product` is passed IN rather than the well's single line being looked
    up, because Merlin-05 no longer HAS a single line: every well now carries a
    realistic 3-5 line casing programme (see `_seed_well_programmes`). The override
    has to target the line that actually carries the scenario -- the unassigned
    CSG 7 29.0 line -- and picking "the one line" would either raise or, worse,
    silently attach the scenario to a stage line and quietly stop demonstrating
    anything.
    """
    line = (
        db.query(DemandLine)
        .filter(
            DemandLine.well_id == merlin_well.id,
            DemandLine.product_id == merlin_product.id,
        )
        .one()
    )
    scenario = Scenario(
        name=f"{merlin_well.name}: assign the unassigned stock",
        description=(
            "8000 tubulars are on the shelf unassigned and Merlin-05 is still "
            "Uncovered, because hard allocation only covers a line from inventory "
            "assigned to it. What if 5000 were assigned? Previews cleanly -- but "
            "CANNOT be applied: inventory assignments are an Oracle-owned "
            "projection, so the agreed change has to be raised in Oracle and "
            "synced back."
        ),
        business_unit_id=hard_customer.business_unit_id,
        status=ScenarioStatus.DISCUSSION,
        created_by="demo.buyer",
    )
    db.add(scenario)
    db.flush()
    db.add(
        ScenarioOverride(
            scenario_id=scenario.id,
            target_kind=ScenarioTargetKind.ASSIGNMENT,
            target_demand_line_id=line.id,
            target_product_id=line.product_id,
            field_name="quantity",
            value_number=5000,
            note="Assignment agreed verbally -- pending Oracle confirmation.",
        )
    )
    db.flush()
    return scenario


# ---------------------------------------------------------------------------
# Realistic well programmes -- 3-5 demand lines per well
# ---------------------------------------------------------------------------
#
# THE PROBLEM THIS FIXES
# ----------------------
# Almost every well in this seed carried exactly ONE demand line, because each was
# built to demonstrate one coverage rule and one line is the smallest thing that
# demonstrates a rule. Real wells are nothing like that: a well is cased in stages
# as it is drilled, and each stage is a different size of pipe needed on a
# different date. The product owner's words: "most of wells has only 1 line. in
# reality most of wells should have multiple lines. and not just 2 lines but 3-5
# lines."
#
# The one-line shape also made a screen actively misleading rather than merely
# thin. The Coverage Workspace shows in-scope and covered LINE COUNTS per well;
# with one line per well every count was 1 of 1 or 0 of 1, so changing a filter
# moved the numbers by whole wells at a time and there was no way to read what the
# filter had actually done. Counts only become legible when wells carry several
# lines in a mix of statuses and profiles, which is what this function creates.
#
# HOW EVERY DEMONSTRATED SCENARIO SURVIVES
# ----------------------------------------
# The additional lines are given their OWN products, stocked AMPLY in both Business
# Units -- the same isolation discipline every helper above already follows, and for
# the same reason. Consequences, stated rather than hoped for:
#
#   * A stage line is always Covered, so it never changes a WELL ROLLUP. A covered
#     well stays covered (all of its lines are covered); an uncovered well stays
#     uncovered (its scenario line is still not covered, and one bad line is enough
#     under the rollup rule).
#   * A stage line never competes with a scenario line, because no scenario product
#     appears in the stage catalogue. Every existing verdict is still decided by
#     exactly the fact that decided it before -- not by a re-tuned quantity.
#   * The ample stock is a deliberate, stated choice, NOT a number tuned until the
#     answers came back right. If a stage product were short, its line would go
#     Uncovered and flip a covered well -- creating a new scenario nobody asked for,
#     competing for attention with the one that well exists to show.
#
# WHY THE STAGES LADDER FORWARD FROM THE SCENARIO LINE, NOT BACKWARD
# -----------------------------------------------------------------
# Stages are drilled in sequence, so their ROS dates must ladder. They ladder
# FORWARD from each well's existing scenario ROS, i.e. the scenario line is treated
# as the shallowest stage and the additions are the deeper ones drilled after it.
#
# Laddering backward would be equally realistic and is not available: several
# scenario ROS dates are only 15-60 days out (Eagle-01 at +30d, Osprey-09 at +60d),
# so earlier stages would land in the past or before the seed's own "now". Forward
# laddering also keeps each well's EARLIEST ROS equal to its scenario line's ROS,
# which is worth having on purpose -- `earliest_ros_date` and `first_runout_date`
# (see `app.engines.well_dates`) then stay anchored to the fact the well exists to
# demonstrate, instead of being decided by filler.
#
# HARD allocation needs the assignments too
# -----------------------------------------
# Under HARD, unassigned pool stock provides no coverage however much of it there
# is -- that is the whole point of Well Merlin-05. So every stage line belonging to
# the HARD customer is given a matching InventoryAssignment row. Without them
# Kestrel-02, Fratercula-19 and Storm-21 would flip to Uncovered for a reason that
# has nothing to do with what they demonstrate. HYBRID needs no assignment (it tops
# up from the unassigned pool by ROS, which is Petrel-03's whole point) and SOFT
# pools by definition.
#
# ALL VALUES SYNTHETIC -- not AkerBP figures.

#: (type, size, weight, grade, grade_type, connection, stage label, quantity)
#:
#: Every entry resolves ALL FOUR lead-time dimensions against the component set
#: seeded in `run()` -- OD/WT via the "*" wildcard, Grade via Carbon, Connection via
#: VAM 21 / VAM TOP, Logistics via the shared "*" sailing row. That is a
#: requirement, not a coincidence: `tests/test_seed_honesty_paths` pins that the
#: CSG 7 32.0 "Hydril 563" product is the ONLY unmodelled one in the catalogue, so a
#: stage product on an unlisted connection would silently create a second one and
#: break the honesty demonstration.
_STAGE_CATALOGUE = (
    ("CSG", "20", 133.0, "K55", "Carbon", "VAM 21", "conductor", 900.0),
    ("CSG", "13-3/8", 72.0, "L80", "Carbon", "VAM 21", "surface casing", 1800.0),
    ("CSG", "9-5/8", 47.0, "L80", "Carbon", "VAM 21", "intermediate casing", 2600.0),
    ("CSG", "7", 26.0, "L80", "Carbon", "VAM 21", "production casing", 3100.0),
    ("TBG", "4-1/2", 11.6, "L80", "Carbon", "VAM TOP", "production tubing", 3400.0),
    ("CSG", "5-1/2", 20.0, "L80", "Carbon", "VAM 21", "contingency liner", 1200.0),
)

#: Stage stock per (Business Unit, stage product). Ample on purpose -- see above.
#: Total stage demand across the whole seed is on the order of 30 000 per product,
#: and the HARD customer's assignments carve a further few thousand out of the SOFT
#: customer's view of the same Business Unit pool, so this leaves the stage lines
#: constrained by nothing at all.
_STAGE_ON_HAND = 500_000.0

#: {well name: (stage indices, the subset of those indices that are CONTINGENCY)}
#:
#: Hand-written rather than generated, so the line count and profile mix of every
#: well is a visible, reviewable fact. Counts are chosen to land each well at 3-5
#: TOTAL lines once its existing scenario line(s) are counted, which is what was
#: asked for.
#:
#: The CONTINGENCY entries matter more than they look. Contingency is IN SCOPE under
#: the new default profile filter (`app.engines.coverage.DEFAULT_PROFILE_FILTER`),
#: and before this the seed contained not one contingency line -- so the half of the
#: owner's filter change that WIDENED scope was undemonstrable, and the Coverage
#: Workspace's profile toggle had nothing to move. A contingency liner is also
#: exactly the right thing to model: it is steel the well may genuinely need, which
#: is why planning as though it will never be called off was the behaviour fixed.
_WELL_PROGRAMMES = {
    # -- Norheim Energy (SOFT, BU North) ----------------------------------
    "Well Eagle-01": ((2, 3, 5), (5,)),  # + its own 13CR TBG line = 4 lines
    # The two wells the default status filter EXCLUDES. They get programmes for the
    # same reason every other well does -- a one-line well is not what a well looks
    # like -- and it also makes the exclusion arithmetic worth reading: widening the
    # status filter brings 4 + 4 lines in, not 1 + 1.
    "Well Merganser-25": ((1, 2, 5), (5,)),
    "Well Shelduck-26": ((0, 2, 5), (5,)),
    "Well Falcon-04": ((0, 2, 4), ()),
    "Well Hawk-07": ((0, 1, 3, 5), (5,)),
    "Well Osprey-09": ((1, 2, 3), ()),
    "Well Puffin-12": ((0, 1, 4), ()),
    "Well Tern-08": ((1, 3, 5), (5,)),
    "Well Fulmar-13": ((0, 2, 4), ()),
    "Well Guillemot-16": ((1, 2, 3, 4), ()),
    "Well Razorbill-17": ((0, 1, 2), ()),
    "Well Shearwater-18": ((1, 3, 4), ()),
    "Well Kittiwake-20": ((0, 2, 4, 5), (5,)),
    "Well Gadwall-22": ((1, 2, 3), ()),
    "Well Dunlin-23": ((0, 1, 4), ()),
    "Well Auklet-24": ((0, 1, 2, 3), ()),
    # The customer-owned priority wells. Stage products again, so the stage lines
    # cannot touch the priority products and the demonstration stays decided by
    # ownership alone. Note Norheim Energy now has an upload record, so its
    # customer-owned quantity of every STAGE product reads as a measured ZERO rather
    # than as unknown -- which is exactly the distinction the model exists to keep,
    # and it draws nothing either way.
    "Well Wigeon-27": ((0, 1, 5), (5,)),
    "Well Pintail-28": ((1, 2, 3), ()),
    "Well Garganey-29": ((0, 2, 4), ()),
    # -- Vestfjord Petroleum (HARD, BU North) ----------------------------------
    "Well Kestrel-02": ((1, 2, 4), (4,)),
    "Well Merlin-05": ((0, 1, 3), ()),
    "Well Gannet-11": ((1, 2, 3), ()),
    "Well Fratercula-19": ((0, 2, 4), ()),
    "Well Storm-21": ((1, 3, 4), ()),
    # -- Pelican Gulf Drilling (HYBRID, BU Gulf) ---------------------------------
    "Well Petrel-03": ((0, 1, 4, 5), (5,)),
    "Well Skua-06": ((1, 2, 3), ()),
    "Well Skerry-14": ((0, 2, 4), ()),
}

#: Days between consecutive stages of one well. Stages are drilled in sequence, so
#: their ROS dates must not coincide -- and the gap is what makes the Well Workspace
#: show a ladder rather than a pile.
_STAGE_ROS_GAP_DAYS = 25


def _seed_well_programmes(db, bu_north, bu_gulf):
    """Give most wells a realistic 3-5 line casing programme. See the block above.

    Returns {well name: total line count} so the seed can print what it built.

    Runs LAST, after every scenario helper: the wells must all exist first, and the
    coverage recompute is done ONCE per customer at the end rather than per well.
    That is not merely faster, it is the same answer -- `recompute_well` already
    widens to the whole customer pool, because inventory cannot honestly be
    allocated one well at a time.
    """
    stage_products = []
    for type_, size, weight, grade, grade_type, connection, label, _qty in (
        _STAGE_CATALOGUE
    ):
        product = Product(
            type=type_, size=size, weight=weight, grade=grade,
            grade_type=grade_type, connection=connection, commodity="SMLS",
            description=f"{type_} {size} {weight:g} {grade} {connection} SMLS",
            unit_of_measure=UnitOfMeasure.MTR,
        )
        db.add(product)
        db.flush()
        # Stocked in BOTH Business Units. A stage product demanded from BU Gulf
        # (Petrel-03, Skua-06, Skerry-14) needs a Gulf row of its own -- there is no
        # cross-BU fallback and `app.engines.inventory` raises rather than inventing
        # one, so a missing row here would take down that customer's entire pass.
        _on_hand(db, bu_north, product, _STAGE_ON_HAND)
        _on_hand(db, bu_gulf, product, _STAGE_ON_HAND)
        stage_products.append((product, label))

    line_counts = {}
    for well_name, (stage_indices, contingency_indices) in _WELL_PROGRAMMES.items():
        well = db.query(Well).filter(Well.name == well_name).one()
        customer = well.planning_node.customer
        # The anchor is the well's EARLIEST existing ROS, so the additions ladder
        # forward from the scenario line rather than in front of it.
        existing = db.query(DemandLine).filter(DemandLine.well_id == well.id).all()
        anchor = min(line.ros_date for line in existing)

        for position, stage_index in enumerate(stage_indices, start=1):
            product, _label = stage_products[stage_index]
            quantity = _STAGE_CATALOGUE[stage_index][7]
            line = DemandLine(
                well_id=well.id,
                product_id=product.id,
                quantity=quantity,
                ros_date=anchor + timedelta(days=_STAGE_ROS_GAP_DAYS * position),
                profile=(
                    DemandProfile.CONTINGENCY
                    if stage_index in contingency_indices
                    else DemandProfile.PRIMARY
                ),
            )
            db.add(line)
            db.flush()
            # HARD covers a line ONLY from inventory assigned to that line, so the
            # HARD customer's stage lines need explicit assignments or they would go
            # Uncovered beside half a million metres of unassigned pipe -- flipping
            # Kestrel-02, Fratercula-19 and Storm-21 for a reason unrelated to what
            # they demonstrate. SOFT pools and HYBRID tops up from the pool, so
            # neither needs one.
            if customer.allocation_policy == AllocationPolicy.HARD:
                _assign(db, line, product, quantity)

        line_counts[well_name] = (
            db.query(DemandLine).filter(DemandLine.well_id == well.id).count()
        )

    for customer in db.query(Customer).all():
        recompute_customer(db, customer)
    return line_counts


class SeedRefused(SystemExit):
    """Raised (as a non-zero exit) when seeding would be unsafe or misleading.

    Deliberately an EXIT, not a quiet return. The previous behaviour -- print
    "already present, skipping" and return 0 -- is what let three separate
    schema-drift incidents look like healthy runs.
    """

    def __init__(self, message: str):
        print(f"\nSEED REFUSED: {message}\n", file=sys.stderr)
        super().__init__(1)


def _require_migrated_schema() -> None:
    """The seed no longer creates the schema. Alembic owns it.

    Calling create_all() here was half of the drift problem: it papered over a
    database that was missing COLUMNS by silently succeeding (create_all adds
    missing tables, never missing columns), so the first symptom was a
    `no such column` crash somewhere deep in the seed.
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    expected = set(Base.metadata.tables)
    missing = sorted(expected - tables)
    if missing:
        raise SeedRefused(
            "the database schema is missing "
            f"{len(missing)} table(s): {', '.join(missing)}.\n"
            "  The schema is owned by Alembic, not by this script. Run:\n"
            "      alembic upgrade head"
        )

    if "alembic_version" not in tables:
        raise SeedRefused(
            "this database has tables but NO alembic_version row, so it is not\n"
            "  under migration control and its columns cannot be trusted to match the\n"
            "  models. Either stamp it (`alembic stamp head`, only if you are certain it\n"
            "  is current) or rebuild it from scratch with `alembic upgrade head`."
        )


# Child-before-parent order, used by --reset. Kept explicit rather than derived
# so that a new table is a visible, deliberate edit here.
#
# `users` is deliberately ABSENT: logins are provisioned by seed/seed_users.py
# and must survive a demo-data reset -- wiping them would lock everyone out of
# the freshly reseeded instance.
_DELETE_ORDER = (
    # scenario_overrides carries FKs to demand_lines, products, business_units,
    # inventory_assignments and well_substitution_approvals, so it has to go
    # before all of them -- i.e. first.
    "scenario_overrides",
    "scenarios",
    # demand_import_rows carries FKs to demand_lines, wells and products, so it
    # also has to precede all of them; its parent batch follows immediately.
    "demand_import_rows",
    "demand_import_batches",
    "well_substitution_approvals",
    # Customer-owned inventory rows FK their upload and their customer/product,
    # so rows go first, then the upload audit trail. Added when the tables were
    # -- these two and company_inventory_uploads postdate the original list.
    "customer_owned_inventory",
    "customer_owned_inventory_uploads",
    "company_inventory_uploads",
    # Safety stocks FK products, so they must go before products. Missing from
    # the original list; each --reset was orphaning the previous world's rows.
    "safety_stocks",
    "inventory_assignments",
    "impact_records",
    "demand_revisions",
    "coverage_results",
    "demand_lines",
    "wells",
    "planning_nodes",
    "customer_substitution_rules",
    "technical_substitutions",
    "inventory_on_hand",
    # The purchase-order projection: FKs to business_units and products, so it goes
    # before both. Listed explicitly rather than derived, like every other table
    # here, so adding one stays a visible edit.
    "inventory_on_order",
    "customers",
    "products",
    "lead_time_components",
    "business_units",
)


def _reset(db) -> None:
    """Delete ALL rows (schema untouched) so the seed can be re-applied.

    This wipes real data as well as demo data -- it is only ever reached via an
    explicit --reset on the command line.
    """
    print("--reset: deleting all existing rows before re-seeding")
    for table_name in _DELETE_ORDER:
        table = Base.metadata.tables[table_name]
        deleted = db.execute(table.delete()).rowcount
        if deleted:
            print(f"  cleared {table_name} ({deleted} rows)")
    db.flush()


def run(reset: bool = False):
    _require_migrated_schema()
    db = SessionLocal()
    try:
        if db.query(Customer).first() is not None:
            if not reset:
                raise SeedRefused(
                    "this database already contains customer rows.\n"
                    "  Nothing was written and nothing was verified -- do NOT read this as\n"
                    "  'the data is fine'. If the models have changed since this database was\n"
                    "  built, the schema is still stale until you run `alembic upgrade head`.\n"
                    "  To rebuild the demo data from scratch (DESTRUCTIVE -- deletes all rows):\n"
                    "      python -m seed.seed_from_workbook --reset"
                )
            _reset(db)

        # Business Units -- the outermost inventory boundary, never crossed.
        #
        #   North Sea Operations : Norheim Energy (SOFT) + Vestfjord Petroleum (HARD)
        #                          -> two customers in ONE BU, so they are
        #                             allocated together and compete for its pool.
        #   Gulf Operations      : Pelican Gulf Drilling (HYBRID)
        #                          -> a DIFFERENT BU holding ample stock of the
        #                             very product BU North is short of, which no
        #                             code path may ever offer across.
        bu_north = BusinessUnit(name="North Sea Operations")
        bu_gulf = BusinessUnit(name="Gulf Operations")
        db.add_all([bu_north, bu_gulf])
        db.flush()

        customer = Customer(
            name="Norheim Energy",
            allocation_policy=AllocationPolicy.SOFT,
            business_unit_id=bu_north.id,
        )
        db.add(customer)
        db.flush()

        campaign = PlanningNode(
            customer_id=customer.id, parent_id=None, node_type="Campaign", name="Norheim 2026 Drilling Campaign"
        )
        db.add(campaign)
        db.flush()

        products = [
            Product(
                type="TBG", size="4-1/2", weight=12.6, grade="13CR80", grade_type="13CR",
                connection="VAM TOP", commodity="SMLS",
                description="TBG 4-1/2 12.6 13CR80 VAM TOP SMLS",
                unit_of_measure=UnitOfMeasure.MTR,
            ),
            Product(
                type="CSG", size="9-5/8", weight=53.5, grade="P110", grade_type="Carbon",
                connection="VAM 21", commodity="SMLS",
                description="CSG 9-5/8 53.5 P110 VAM 21 SMLS",
                unit_of_measure=UnitOfMeasure.MTR,
            ),
            # Substitution demo stock: a heavier-wall CSG that engineering
            # accepts in place of the P110 above, with enough on-hand qty to
            # close Falcon-04's shortfall once the well-level approval lands.
            Product(
                type="CSG", size="9-5/8", weight=58.4, grade="Q125", grade_type="Carbon",
                connection="VAM 21", commodity="SMLS",
                description="CSG 9-5/8 58.4 Q125 VAM 21 SMLS",
                unit_of_measure=UnitOfMeasure.MTR,
            ),
        ]
        db.add_all(products)
        db.flush()
        tbg, csg, csg_sub = products

        # Norheim Energy's quantities, stated per Business Unit because that is
        # the only place a quantity can live. These three numbers are the ones the
        # original seed carried on `Product.on_hand_qty`, so every Eagle-01 /
        # Falcon-04 / Hawk-07 / Osprey-09 verdict below is unchanged -- what
        # changed is that they are now attributed to a BU instead of being global.
        _on_hand(db, bu_north, tbg, 8000)
        _on_hand(db, bu_north, csg, 3000)
        _on_hand(db, bu_north, csg_sub, 6000)

        # ---------------------------------------------------------------
        # Attribute lead-time components (spec: OD/WT + Grade + Connection
        # + Logistics). ALL VALUES SYNTHETIC -- not AkerBP figures.
        #
        # Seeded ONCE for the whole demo database, because these rows are
        # attribute-keyed, not product-keyed: every product seeded by every
        # helper below resolves against this same set. All four dimensions are
        # required (an incomplete set resolves to "not modelled", total 0 --
        # see app.engines.lead_time), so each dimension either lists the
        # attribute values in use or carries a "*" wildcard default.
        #
        # The Logistics/sailing allowance is stored EXACTLY ONCE, as a "*" row.
        # It used to be duplicated per grade_type, and the shape it was
        # duplicated into had no way to express "applies to everything" at all.
        # Both consumers -- total_lead_time_months and
        # order_dates.transit_months -- now find this single row, because both
        # are views of one resolver.
        #
        # The point of the OD/WT and Connection rows is that lead time now
        # actually VARIES by them. Worked examples from this exact set:
        #
        #   TBG 4-1/2 12.6 13CR80 VAM TOP  = 3.0 + 1.0 + 0.5 + 2 = 6.5 months
        #   CSG 9-5/8 53.5 P110  VAM 21    = 4.0 + 0.5 + 1.0 + 2 = 7.5 months
        #   CSG 13-3/8 68 P110   VAM 21    = 6.0 + 0.5 + 1.0 + 2 = 9.5 months
        #     ^ same grade family as the 9-5/8, TWO months longer purely on
        #       OD/WT. Under the old grade_type-only model these two were
        #       GUARANTEED identical -- that was the defect.
        #   TBG 3-1/2 9.2 L80 VAM TOP      = 3.0 + 0.5 + 0.5 + 2 = 6.0 months
        #   CSG 7 29 L80 VAM 21            = 3.0 + 0.5 + 1.0 + 2 = 6.5 months
        #     ^ same grade family, same (wildcard) OD/WT term: they differ ONLY
        #       by connection.
        #   TBG 2-7/8 6.5 L80 VAM TOP      = 3.0 + 0.5 + 0.5 + 2 = 6.0 months
        #   TBG 2-7/8 7.9 L80 VAM TOP      = 3.5 + 0.5 + 0.5 + 2 = 6.5 months
        #     ^ identical size, grade and connection: differ only on WALL,
        #       which the old model could not see at all.
        #
        # The 13CR TBG deliberately still totals 6.5 months, the figure the old
        # model produced for it, so Hawk-07 stays Uncovered-but-recoverable and
        # Osprey-09 stays Unrecoverable for the same reason as before rather
        # than by accident.
        lead_time_components = [
            # OD/WT -- keyed by the canonical "size weight" pair (lead_time
            # .od_wt_key). "*" is the default for pairs not listed.
            LeadTimeComponent(
                dimension=LeadTimeDimension.OD_WT, attribute_value=ANY_ATTRIBUTE_VALUE,
                months=3.0, label="Ex-mill (standard sizes)",
            ),
            LeadTimeComponent(
                dimension=LeadTimeDimension.OD_WT, attribute_value="4-1/2 12.6",
                months=3.0, label="Ex-mill 4-1/2 12.6",
            ),
            LeadTimeComponent(
                dimension=LeadTimeDimension.OD_WT, attribute_value="9-5/8 53.5",
                months=4.0, label="Ex-mill 9-5/8 53.5",
            ),
            LeadTimeComponent(
                dimension=LeadTimeDimension.OD_WT, attribute_value="13-3/8 68",
                months=6.0, label="Ex-mill 13-3/8 68 (large OD, long mill queue)",
            ),
            LeadTimeComponent(
                dimension=LeadTimeDimension.OD_WT, attribute_value="2-7/8 7.9",
                months=3.5, label="Ex-mill 2-7/8 7.9 (heavy wall)",
            ),
            # Grade -- keyed by grade FAMILY (Product.grade_type). No wildcard:
            # an unlisted grade family should surface as "not modelled" rather
            # than quietly inherit somebody else's metallurgy allowance.
            LeadTimeComponent(
                dimension=LeadTimeDimension.GRADE, attribute_value="13CR",
                months=1.0, label="13CR melt slot",
            ),
            LeadTimeComponent(
                dimension=LeadTimeDimension.GRADE, attribute_value="Carbon",
                months=0.5, label="Carbon melt slot",
            ),
            # Connection -- keyed by Product.connection. Both connections in
            # this demo catalogue are listed explicitly.
            LeadTimeComponent(
                dimension=LeadTimeDimension.CONNECTION, attribute_value="VAM TOP",
                months=0.5, label="VAM TOP threading",
            ),
            LeadTimeComponent(
                dimension=LeadTimeDimension.CONNECTION, attribute_value="VAM 21",
                months=1.0, label="VAM 21 threading",
            ),
            # Logistics -- ONE row for everything. This is the sailing leg that
            # order_dates strips off the ROS date to get required_ship_date.
            LeadTimeComponent(
                dimension=LeadTimeDimension.LOGISTICS,
                attribute_value=ANY_ATTRIBUTE_VALUE,
                months=2.0, label="Sailing",
            ),
        ]
        db.add_all(lead_time_components)
        db.flush()

        well_a = _well(campaign.id, "Well Eagle-01")
        well_b = _well(campaign.id, "Well Falcon-04")
        # MRP demo wells: both consume the 13CR TBG, which has no substitute
        # registered, so their shortfalls fall all the way through to a mill
        # order recommendation (Layer 1) and drive the TBG runout curve.
        well_c = _well(campaign.id, "Well Hawk-07")
        well_d = _well(campaign.id, "Well Osprey-09")
        db.add_all([well_a, well_b, well_c, well_d])
        db.flush()

        now = datetime.utcnow()
        demand_lines = [
            # Well Eagle-01: both lines fit within on-hand qty -> Covered
            DemandLine(
                well_id=well_a.id, product_id=tbg.id, quantity=5000,
                ros_date=now + timedelta(days=30),
                profile=DemandProfile.PRIMARY,
            ),
            # NOTE: Eagle-01's second line -- 1500 of CSG at ROS +15d, Budgeted --
            # HAS MOVED, to Well Shelduck-26 (see
            # `_seed_status_excluded_wells`). It was a Budgeted line inside a
            # Confirmed well, which is precisely the state that stopped existing when
            # demand status became a property of the WELL. Keeping it here would have
            # meant confirming it along with the well, putting 1500 of the 3000 CSG
            # pool ahead of Falcon-04 and changing Falcon-04's shortfall arithmetic
            # for no reason anybody asked for. It now sits on a Budgeted well of its
            # own, so it is out of default scope exactly as it was before -- and
            # widening the status filter now moves a whole well.
            # Well Falcon-04: CSG line exceeds on-hand qty -> Uncovered
            DemandLine(
                well_id=well_b.id, product_id=csg.id, quantity=4000,
                ros_date=now + timedelta(days=20),
                profile=DemandProfile.PRIMARY,
            ),
            # Well Hawk-07: TBG demand exceeds the whole 8000 on-hand pool with
            # a far-out ROS -> Uncovered with a RECOVERABLE mill order
            # recommendation (13CR total lead time is 6.5 months, so the
            # recommended order date still lies in the future).
            DemandLine(
                well_id=well_c.id, product_id=tbg.id, quantity=9000,
                ros_date=now + timedelta(days=300),
                profile=DemandProfile.PRIMARY,
            ),
            # Well Osprey-09: same TBG but ROS inside the lead time -> even
            # ordering today misses it, so coverage resolves UNRECOVERABLE.
            DemandLine(
                well_id=well_d.id, product_id=tbg.id, quantity=9000,
                ros_date=now + timedelta(days=60),
                profile=DemandProfile.PRIMARY,
            ),
        ]
        db.add_all(demand_lines)
        db.flush()

        falcon_csg_line = demand_lines[1]

        # Substitution layers 1 and 2 clear; layer 3 is deliberately left
        # Pending so the Home Dashboard "Pending Approvals" card has real data.
        db.add(
            TechnicalSubstitution(from_product_id=csg.id, to_product_id=csg_sub.id)
        )
        db.add(
            CustomerSubstitutionRule(
                customer_id=customer.id,
                from_product_id=csg.id,
                to_product_id=csg_sub.id,
                allowed=True,
            )
        )
        db.flush()
        request_approval(db, falcon_csg_line, csg.id, csg_sub.id)

        for well in (well_a, well_b, well_c, well_d):
            recompute_well(db, well)

        hard_customer, hard_wells, hard_product = _seed_hard_allocation_customer(
            db, now, bu_north
        )
        hybrid_customer, hybrid_wells = _seed_hybrid_allocation_customer(
            db, now, bu_gulf
        )

        share_product, surplus_well, needy_well = _seed_bu_boundary_scenario(
            db, now, bu_north, bu_gulf, customer, hard_customer
        )

        # The case that would have caught the cross-BU leak. Seeded after the
        # boundary demo and given its OWN product, so it cannot perturb any
        # verdict above.
        indep_product, tern_well, skerry_well = _seed_two_bu_independence(
            db, now, bu_north, bu_gulf, customer, hybrid_customer
        )

        # THE CUSTOMER-OWNED CONSUMPTION PRIORITY. Own products, so it cannot move a
        # verdict above -- and seeded BEFORE the well programmes like every other
        # scenario helper, so its wells get their casing programmes too.
        (
            co_priority_product,
            co_shortfall_product,
            wigeon_well,
            pintail_well,
            garganey_well,
        ) = _seed_customer_owned_priority(db, now, bu_north, customer)

        # The three behaviours introduced by the allocation-semantics reversal.
        # Each has its OWN product, so none of them can move a verdict above.
        _pc_product, fulmar_well, guillemot_well = _seed_partial_consumption(
            db, now, bu_north, customer
        )
        (
            _os_primary,
            os_substitute,
            razorbill_well,
            shearwater_well,
        ) = _seed_oversubscribed_pending_substitute(db, now, bu_north, customer)
        (
            _rel_primary,
            rel_substitute,
            kittiwake_well,
            fratercula_well,
        ) = _seed_oracle_release_blocked_substitute(
            db, now, bu_north, customer, hard_customer
        )

        # The fourth and last point-fix under "the platform never overrides a hard
        # reservation": a SOFT customer's own-product allocation. Own product again,
        # so it cannot move a verdict above.
        (
            _fa_product,
            gadwall_well,
            storm_well,
        ) = _seed_soft_blocked_by_foreign_assignment(
            db, now, bu_north, customer, hard_customer
        )

        # The three "honesty" paths that no seeded data could previously reach, so
        # that the platform's refusals to invent a number are demonstrable rather
        # than only unit-tested. Each has its OWN product(s), so none of them can
        # move a verdict above.
        (
            _ins_primary,
            ins_substitute,
            dunlin_well,
        ) = _seed_insufficient_inventory_substitute(db, now, bu_north, customer)
        # The two wells the DEFAULT status filter EXCLUDES. Seeded here, before the
        # well programmes, and using the existing scarce CSG product on purpose --
        # see `_seed_status_excluded_wells`.
        (
            budgeted_well,
            planned_well,
        ) = _seed_status_excluded_wells(db, now, campaign, csg)
        unmodelled_product = _seed_unmodelled_lead_time_product(db, bu_north)
        unstocked_product = _seed_unstocked_product(db)
        # Substitution SUCCEEDING -- the only substitution outcome the demo lacked,
        # and the one that produces CoveredViaSubstitute plus a By Item page listing
        # a demand line for a different product.
        (
            _sub_primary,
            auklet_substitute,
            auklet_well,
        ) = _seed_covered_via_substitute(db, now, bu_north, customer)

        # Realistic 3-5 line casing programmes on top of every scenario well.
        # Seeded after every scenario helper -- the wells must all exist -- and
        # BEFORE the scenarios below, so a scenario preview is a what-if against the
        # final settled base plan rather than against an interim one. Own products,
        # amply stocked, so no verdict above can move; see `_seed_well_programmes`.
        programme_line_counts = _seed_well_programmes(db, bu_north, bu_gulf)

        # Phase 4 -- SHARED scenarios. Seeded LAST, after every recompute, so the
        # base plan they are a what-if against is the settled one. None of them is
        # applied: a scenario is a conversation artefact until somebody agrees it.
        hawk_line = demand_lines[2]    # Well Hawk-07, 9000 of 13CR TBG @ +300d
        osprey_line = demand_lines[3]  # Well Osprey-09, 9000 of 13CR TBG @ +60d
        reduce_hawk, slip_osprey = _seed_scenarios(
            db, now, customer, hawk_line, osprey_line, tbg, bu_north
        )
        # hard_wells == (Kestrel-02, Merlin-05); Merlin-05 is the uncovered one.
        supply_scenario = _seed_supply_scenario(
            db, hard_customer, hard_wells[1], hard_product
        )

        # Incoming supply. Seeded after every recompute on purpose: on-order material
        # is NOT an input to coverage (nothing nets it against demand), so it cannot
        # move a verdict -- and seeding it last makes that impossible to get wrong by
        # accident rather than merely true by argument.
        _seed_on_order(db, bu_north, tbg, csg, hard_product)

        # SYNTHETIC DEMO HISTORY, last of all. It rewrites `created_at` and revision
        # numbers on every demand line and writes backdated `DemandRevision` rows;
        # coverage reads none of those, so no verdict above can move. See
        # `_seed_synthetic_history` for why this is real history rather than an
        # injected total.
        history_line_count = _seed_synthetic_history(db, now)

        db.commit()
        print(
            f"Seeded customer={customer.id}, "
            f"wells=[{well_a.id}, {well_b.id}, {well_c.id}, {well_d.id}]"
        )
        print(f"  BU North Sea Operations = {bu_north.id}")
        print(f"  BU Gulf Operations      = {bu_gulf.id}")
        for label, wells in (("hard", hard_wells), ("hybrid", hybrid_wells)):
            for well in wells:
                print(f"  {label}: {well.name} -> {well.coverage_status}")
        print(
            f"  bu-boundary: product={share_product.id} "
            f"(BU North 9000 / BU Gulf 50000 -- no unscoped figure exists)"
        )
        for well in (surplus_well, needy_well):
            print(f"  bu-boundary: {well.name} -> {well.coverage_status}")
        print(
            f"  two-BU independence: product={indep_product.id} "
            "(BU North 7000 / BU Gulf 2000, both demanding 6000)"
        )
        for well in (tern_well, skerry_well):
            print(f"    {well.name} -> {well.coverage_status}")
        print(
            "    expect: Well Tern-08 Covered (North holds 7000), "
            "Well Skerry-14 Uncovered (Gulf holds 2000) -- same product, "
            "independent verdicts"
        )
        print("  allocation semantics (partial consumption / no hard reservations):")
        for well in (fulmar_well, guillemot_well):
            print(f"    partial consumption: {well.name} -> {well.coverage_status}")
        print(
            "      expect BOTH Uncovered: Fulmar-13 (earlier ROS) draws all 2000 "
            "without completing its 5000, so Guillemot-16's 2000 is gone. Under the "
            "old all-or-nothing rule Guillemot-16 was Covered -- the drop in covered "
            "wells is the accepted trade-off, not a bug."
        )
        for well in (razorbill_well, shearwater_well):
            print(f"    over-subscribed pending: {well.name} -> {well.coverage_status}")
        print(
            f"      expect BOTH PendingApproval on {os_substitute.description}, each "
            "reason carrying OVER-SUBSCRIBED: 2 lines need 6000 against 4000 "
            "available (short by 2000). Nothing is reserved."
        )
        print(f"    oracle release: {kittiwake_well.name} -> {kittiwake_well.coverage_status}")
        print(
            f"      expect Uncovered: {rel_substitute.description} is fully approved "
            f"and 5000 sit on the shelf, but all of it is hard-assigned to "
            f"{fratercula_well.name}. The candidate IS offered with blocking_layer "
            "'hard-assigned-elsewhere' and an action naming the Oracle release -- it "
            "never becomes CoveredViaSubstitute."
        )
        print(
            f"    soft blocked by foreign assignment: {gadwall_well.name} -> "
            f"{gadwall_well.coverage_status} / {storm_well.name} -> "
            f"{storm_well.coverage_status}"
        )
        print(
            "      expect Gadwall-22 Uncovered (SOFT) even though 6000 sit on the "
            "shelf: all 6000 are hard-assigned to Storm-21, another CUSTOMER's "
            "demand line. The reason names the assignment and the Oracle release "
            "action. Norheim Energy's OWN assignments would change nothing -- "
            "pooling is untouched; only foreign reservations bite."
        )
        print(
            f"    substitution SUCCEEDING: {auklet_well.name} -> "
            f"{auklet_well.coverage_status}"
        )
        print(
            f"      expect Covered: the line ordered TBG 3-1/2 10.2 (0 on hand) and is "
            f"satisfied by {auklet_substitute.description} (7000 on hand, nothing "
            "assigned). The only substitution SUCCESS in the seed -- every other case "
            "stops at pending, blocked or over-subscribed -- so this is the only "
            "CoveredViaSubstitute line, and the only By Item page that lists a demand "
            f"line for a DIFFERENT product. GET /mrp/by-item/{auklet_substitute.id} "
            "shows it; the Product column is what stops a planner reading that row as "
            "an order for the page's own product and committing the steel twice."
        )
        print("  CUSTOMER-OWNED INVENTORY -- consumption priority over company stock:")
        for well in (wigeon_well, pintail_well, garganey_well):
            print(f"    {well.name} -> {well.coverage_status}")
        print(
            f"      {co_priority_product.description}: BU North (company) holds 2000, "
            f"{customer.name} OWNS 3000. Wigeon-27 (3000 @ +370d) is Covered entirely "
            "from its own material and leaves the company 2000 UNTOUCHED, so "
            "Pintail-28 (2000 @ +400d) is Covered from it. Company-owned-first, or one "
            "undifferentiated pool of 2000, would leave BOTH Uncovered -- Wigeon-27 "
            "would drain the 2000 in ROS order and still be 1000 short. The tier is "
            "worth exactly two wells here."
        )
        print(
            f"      {co_shortfall_product.description}: company holds an explicit 0, "
            f"{customer.name} owns 1000, Garganey-29 needs 2500 -> Uncovered, and its "
            "REASON says the customer-owned stock was drawn FIRST and by how much it "
            "is still short. Without that wording a planner whose own 1000 metres were "
            "on the dock would conclude the upload had been ignored."
        )
        print(
            "      customer-owned stock is NEVER shareable: no other customer's "
            "line can draw these quantities, in either direction, even though the "
            "company pool around them is shared across the whole Business Unit. "
            "It is that customer's PROPERTY -- a boundary tighter than the "
            "Business Unit boundary."
        )
        print(
            "      'owns none' vs 'no upload' is demo-reachable BOTH ways: "
            f"{customer.name} has an upload record, so every product it did NOT "
            "declare reads as a MEASURED zero; Vestfjord Petroleum and C have no upload "
            "at all, so their customer-owned figure is UNKNOWN and every screen must "
            "render it unavailable rather than 0. GET /customer-owned-inventory/"
            f"{{customer_id}} -> has_uploaded tells them apart."
        )
        print(
            "      provenance: source_system='customer-upload' (never 'oracle' -- "
            "Oracle does not hold this data at all) and uploaded_at is backdated ~45 "
            "days, because this figure is only as current as the last spreadsheet "
            "somebody sent and a demo that hid that would hide the point."
        )
        print("  honesty paths (the platform refusing to invent a number):")
        print(
            f"    insufficient inventory: {dunlin_well.name} -> "
            f"{dunlin_well.coverage_status}"
        )
        print(
            f"      expect Uncovered: {ins_substitute.description} is fully APPROVED "
            "and nothing is assigned to it, but only 500 of the 4000 required exist. "
            "blocking_layer 'insufficient-inventory' -- go to the MILL. Compare with "
            "Kittiwake-20 at the same ROS, whose approved substitute IS on the dock "
            "and whose action is 'go to ORACLE'."
        )
        print(
            f"    lead time NOT MODELLED: product={unmodelled_product.id} "
            f"({unmodelled_product.description})"
        )
        print(
            "      catalogue-only, no demand. Connection 'Hydril 563' has no "
            "component row and Connection has no wildcard, so "
            f"GET /mrp/lead-time/{unmodelled_product.id} answers modelled=false, "
            "total_months=0.0, missing_dimensions=['Connection'] -- render as 'not "
            "modelled', NEVER as '0 mo'."
        )
        print(
            f"    on-hand UNKNOWN: product={unstocked_product.id} "
            f"({unstocked_product.description})"
        )
        print(
            "      catalogue-only, no demand, and NO InventoryOnHand row in any BU. "
            f"GET /mrp/by-item/{unstocked_product.id} -> 424 inventory_row_missing; "
            f"GET /mrp/lead-time/{unstocked_product.id} -> 200 (6.5 months) -- a "
            "lead time is knowable without stock."
        )
        print(
            "    NOT seeded: an unmapped customer (409 inventory_scope_missing). "
            "GET /coverage with non-default filters recomputes EVERY customer "
            "(app.engines.coverage_view.project_coverage), and coverage for a "
            "customer with no Business Unit raises even when it has no demand at "
            "all -- so one unmapped row would 409 that endpoint for everybody. "
            "Covered by tests only, by design."
        )
        print(
            f"  try: GET /coverage?customer_id={hard_customer.id}  (expect "
            "Gannet-11 Uncovered: a hard line never draws on the shared pool, "
            "however much of it BU North holds)"
        )
        print(
            f"  try: GET /coverage?customer_id={hybrid_customer.id}  (expect BU "
            "Gulf judged against its own stock alone -- the boundary D01 did not "
            "move)"
        )
        counts = sorted(programme_line_counts.values())
        multi = [n for n in counts if n >= 3]
        print(
            f"  well programmes: {len(counts)} wells now carry "
            f"{min(counts)}-{max(counts)} demand lines each "
            f"({len(multi)} of {len(counts)} have 3 or more); stage products are "
            f"amply stocked in BOTH BUs so no scenario verdict above moved."
        )
        print(
            "    the Coverage Workspace's line counts are legible again: with one "
            "line per well every count was 1-of-1 or 0-of-1, so a filter change "
            "moved whole wells at a time and said nothing about what it did."
        )
        print(
            "    CONTINGENCY lines are seeded for the first time -- Contingency is "
            "now IN the default profile scope, and without one the widening half of "
            "that change would be undemonstrable."
        )
        print("  demand status is a WELL property (Well.demand_status):")
        for well in (planned_well, budgeted_well):
            print(
                f"    {well.name} -> demand_status={well.demand_status.value}, "
                f"coverage_status={well.coverage_status}"
            )
        print(
            "      expect coverage_status=None for BOTH: the default status filter "
            "is Confirmed only, so neither well is evaluated at all -- 'not "
            "evaluated', never 'covered'. Every other well in this seed is "
            "Confirmed, because a well the engine does not evaluate has no verdict "
            "to demonstrate."
        )
        print(
            "      GET /coverage (default, Confirmed only) vs "
            "GET /coverage?status=Planned&status=Budgeted&status=Confirmed for "
            f"customer {customer.name}: the counts move by WHOLE WELLS -- these two "
            "wells and all of their lines appear together. That legibility is the "
            "point of moving the column: a Budgeted LINE inside a Confirmed well "
            "used to drop on its own and leave its well behind, so the well count "
            "and the line count moved by unrelated amounts."
        )
        print(
            "      widening the filter also FREES/TAKES inventory: these two wells "
            "want 2700 of the 3000 CSG 9-5/8 53.5 P110 pool at ROS +15d/+18d, ahead "
            "of Falcon-04 at +20d. Excluding a whole well frees its steel for other "
            "wells by exactly the arithmetic that excluding a line does."
        )
        print("  incoming supply (on order) -- the three states kept apart:")
        print(
            f"    MEASURED, POSITIVE: {tbg.description} in BU North -- 7000 across "
            "two purchase orders (4000 in ~4 months, 3000 in ~8). Same product Well "
            "Hawk-07 and Well Osprey-09 are short of, so the dashboard's incoming-"
            "supply figure and its supply-risk figure are about the same steel."
        )
        print(
            f"    MEASURED, ZERO:     {csg.description} in BU North -- an explicit "
            "quantity=0 row, i.e. the FACT 'nothing is on order'. Well Falcon-04 is "
            "short of exactly this product, so the demo shows a shortfall with a "
            "confirmed empty pipeline."
        )
        print(
            f"    UNDATED:            {hard_product.description} in BU North -- 2000 "
            "with NO expected_arrival_date (raised, not acknowledged). Counted in "
            "the total, in NO arrival horizon; dating it to today would make "
            "unscheduled steel look imminent."
        )
        print(
            "    UNKNOWN:            every other product, by having no row at all. "
            "Absence is NOT zero -- GET /dashboard/executive reports "
            "incoming_supply.available=false with a reason, and "
            "products_with_no_data says how partial a partly-known total is."
        )
        print(
            "    BU Gulf gets NOTHING on order, deliberately: a purchase order into "
            "another Business Unit is another Business Unit's steel, and the "
            "boundary holds on this screen too."
        )
        print(
            "    oracle_integrated stays FALSE and source='synthetic'. Two "
            "independent flags: seeding a projection table does not make the Oracle "
            "purchase-order feed exist. NOTHING nets on-order material against "
            "demand -- no coverage or supply-risk verdict moved by one metre."
        )
        print("  SYNTHETIC DEMO HISTORY (demand revisions) -- READ THIS:")
        print(
            f"    {history_line_count} demand lines were given backdated created_at "
            "(~30 months) and TWO backdated DemandRevision rows each (~30 and ~14 "
            "months ago). THIS HISTORY IS SYNTHETIC DEMO DATA. Nobody revised "
            "anything; it is a stand-in for the accumulation a real deployment gets "
            "for free by running for a year."
        )
        print(
            "    It is REAL history, not a faked figure. The rows are ordinary "
            "DemandRevision rows and app.engines.executive._state_as_of reads them "
            "through its normal path -- no engine special-case, and no injected "
            "'previous total' anywhere. Remove the rows and the prior-period "
            "comparison correctly goes back to unavailable."
        )
        print(
            "    Effect: GET /dashboard/executive -> demand_trend[*].previous is now "
            "available=true with a non-flat, mixed-direction change_pct, so the "
            "UI's up / down / unchanged rendering is all exercised."
        )
        print("  scenarios (SHARED, none applied):")
        for scenario in (reduce_hawk, slip_osprey, supply_scenario):
            print(
                f"    {scenario.status.value:<10} {scenario.name}  -> "
                f"GET /scenarios/{scenario.id}/preview"
            )
        print(
            f"    expect: {reduce_hawk.name!r} flips Hawk-07 Uncovered -> Covered "
            "(and is applicable);"
        )
        print(
            f"            {slip_osprey.name!r} takes Osprey-09 out of "
            "Unrecoverable (risk impact, well stays Uncovered);"
        )
        print(
            f"            {supply_scenario.name!r} flips Merlin-05 in the preview "
            "but is NOT applicable (Oracle owns inventory assignments)."
        )
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Seed synthetic demo data. Requires an already-migrated database "
            "(`alembic upgrade head`) -- this script does not create schema."
        )
    )
    parser.add_argument(
        "--reset",
        "--force",
        dest="reset",
        action="store_true",
        help=(
            "DESTRUCTIVE: delete every row in every table, then re-seed. Use "
            "this to pick up new seed scenarios after a model change."
        ),
    )
    args = parser.parse_args(argv)
    run(reset=args.reset)


if __name__ == "__main__":
    main()
