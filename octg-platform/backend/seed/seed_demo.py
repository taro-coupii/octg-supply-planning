# -*- coding: utf-8 -*-
"""Clean demo dataset -- the 10-product SCEU Norway world.

Replaces the old scenario-catalogue seed FOR THE DEMO DATABASE ONLY.
`seed_from_workbook.py` stays untouched because `tests/test_seed_honesty_paths.py`
exercises it directly; dev.db (and therefore the Render deployment) is built
from THIS module instead:

    DATABASE_URL="sqlite:///./dev.db" python -m seed.seed_demo --reset
    DATABASE_URL="sqlite:///./dev.db" python -m seed.seed_users

THE WORLD (user-specified, 2026-08-11)
--------------------------------------
* 10 products (the catalogue below), nothing else.
* One Business Unit: SCEU Norway. Two customers: Equinor Norway (SOFT) and
  AkerBP Norway (HYBRID) -- different policies so both judgement paths stay
  demonstrable.
* ~10 wells per customer per year for 3 years. Year 1 mostly Confirmed,
  year 2 a balance, year 3 mostly Planned (the platform's status set has no
  separate "Forecasted"; Planned covers it).
* Each well uses one of five casing designs (item numbers below); the last
  "+ N contingency" item is a CONTINGENCY-profile line. Quantities grow
  toward the tubing: the big-OD conductor is short, the TBG string longest.
* Inventory (re-baselined 2026-09-06 after D01, owner ruling): on-hand per
  product is a SHARE of the next 12 months of CONFIRMED demand
  (`ON_HAND_SHARE_OF_12M`) -- most items near a year of stock, items 5 / 7 /
  9 / 10 deliberately SHORT, item 1 a mild surplus. Items 3 and 8 hold
  headroom beyond the whole confirmed programme so the substitution and
  approval stories have real steel to point at (see `_seed_inventory` and
  `_seed_hard_assignments`). On-order covers months 7-12 of confirmed demand,
  with exceptions -- item 4 has NO purchase-order row at all (its on-order
  position is UNKNOWN, not zero) and one of item 9's POs has no promised date.
* One DRAFT what-if scenario, "AkerBP full replan", with one override per
  override family, built by `_seed_demo_scenario` so a re-seed keeps it.
* Customer-owned stock: small parcels only, never enough for a full well.
* General (technical) substitutions, reading "A substitute B" as "A can
  serve as the substitute FOR B": 2⇄3, 6→for 5, 8→for 9. Customer rules
  allow these pairs per customer so proposals can clear the customer layer.
* Three PENDING customer-approval proposals and a few APPROVED ones, raised
  on real uncovered lines via the production request/decide engines.

REALISM (user feedback, 2026-08-11)
-----------------------------------
The first cut of this world was too regular to demo -- every well of a design
carried identical quantities, ROS days came from a 3-value cycle so the two
customers kept landing on the same dates, and no revision history existed at
all. A FIXED-SEED RNG now jitters quantities, dates, design/asset choice and
inventory factors (regenerating is still deterministic), casing strings phase
across a well instead of arriving on one day, a couple of products run short
early so Uncovered wells appear within the first months, and a backdated
revision history (written through the production apply_revision /
set_well_demand_status engines) fills the Home dashboard's Demand changes
card and each well's history.
"""

import argparse
import random
import sys
from datetime import datetime, timedelta

from sqlalchemy import inspect

from app.db import Base, SessionLocal, engine
from app.engines.coverage import (
    apply_revision,
    recompute_customer,
    set_well_demand_status,
)
from app.engines.overrides import validate as validate_override
from app.engines.scenario import assert_target_in_scope
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
    ImpactRecord,
    InventoryAssignment,
    InventoryOnHand,
    InventoryOnOrder,
    LeadTimeComponent,
    LeadTimeDimension,
    PlanningNode,
    Product,
    SafetyStock,
    Scenario,
    ScenarioOverride,
    ScenarioStatus,
    ScenarioTargetKind,
    TechnicalSubstitution,
    UnitOfMeasure,
    Well,
)

from seed.seed_from_workbook import _DELETE_ORDER  # same reset order, one source

NOW = datetime.utcnow()

#: Fixed-seed RNG: the data looks organic but every regeneration is identical,
#: so screenshots, tests against dev.db and the Render deployment all agree.
RNG = random.Random(20260811)


def _jitter_qty(base: float, lo: float = 0.85, hi: float = 1.25) -> float:
    """A design's base quantity, per-well: +/- jitter, rounded to 50 m."""
    return float(max(50, round(base * RNG.uniform(lo, hi) / 50) * 50))


# --------------------------------------------------------------------------
# The 10-product catalogue. Keyed 1..10 exactly as the user listed them.
# --------------------------------------------------------------------------
CATALOGUE = {
    1: dict(type="CSG", size="20", weight=133.0, grade="K55", grade_type="Carbon",
            connection="NSMAX-GR", commodity="ERW",
            description='CSG 20" 133.00# K55 NSMAX-GR R-3 ERW'),
    2: dict(type="CSG", size="13-3/8", weight=72.0, grade="L80", grade_type="Carbon",
            connection="VAM21", commodity="SMLS",
            description='CSG 13-3/8" 72.00# L80 VAM21 R-3 SMLS'),
    3: dict(type="CSG", size="13-3/8", weight=72.0, grade="P110", grade_type="Carbon",
            connection="VAM21", commodity="SMLS",
            description='CSG 13-3/8" 72.00# P110 VAM21 R-3 SMLS'),
    4: dict(type="CSG", size="10-3/4", weight=60.7, grade="P110", grade_type="Carbon",
            connection="VAM21", commodity="SMLS",
            description='CSG 10-3/4" 60.70# P110 VAM21 R-3 SMLS'),
    5: dict(type="CSG", size="9-5/8", weight=53.5, grade="P110", grade_type="Carbon",
            connection="VAM21", commodity="SMLS",
            description='CSG 9-5/8" 53.50# P110 VAM21 R-3 SMLS'),
    6: dict(type="CSG", size="9-5/8", weight=53.5, grade="SM110XS", grade_type="Sour",
            connection="VAM21", commodity="SMLS",
            description='CSG 9-5/8" 53.50# SM110XS VAM21 R-3 SMLS'),
    7: dict(type="CSG", size="7", weight=32.0, grade="P110", grade_type="Carbon",
            connection="VAM21", commodity="SMLS",
            description='CSG 7" 32.00# P110 VAM21 R-3 SMLS'),
    8: dict(type="TBG", size="5-1/2", weight=20.0, grade="L80", grade_type="Carbon",
            connection="VAM21", commodity="SMLS",
            description='TBG 5-1/2" 20.00# L80 VAM21 R-3 SMLS'),
    9: dict(type="TBG", size="5-1/2", weight=20.0, grade="L80-13CR", grade_type="13CR",
            connection="VAM21", commodity="SMLS",
            description='TBG 5-1/2" 20.00# L80-13CR VAM21 R-3 SMLS'),
    10: dict(type="TBG", size="4-1/2", weight=12.6, grade="L80-13CR", grade_type="13CR",
             connection="VAM21", commodity="SMLS",
             description='TBG 4-1/2" 12.60# L80-13CR VAM21 R-3 MLS'),
}

# "A substitute B" == A can serve as the substitute FOR B  ->  from=B, to=A.
TECH_SUBS = [(3, 2), (2, 3), (5, 6), (9, 8)]  # (from=original, to=substitute)

# The five casing designs: (primary item numbers ordered big-OD -> tubing,
# contingency item or None). Quantities grow toward the tubing string.
DESIGNS = [
    ((1, 2, 6, 8), 7),
    ((1, 3, 5, 9), 7),
    ((1, 2, 6, 8), 10),
    ((1, 3, 5, 9), 10),
    ((1, 3, 4, 7, 10), None),
]
#: Primary quantities by position for 4-item and 5-item designs (metres).
#: Leftmost (conductor) shortest, tubing longest -- per the user's example.
QTY_4 = (500, 1000, 3000, 3500)
QTY_5 = (500, 1000, 1500, 2500, 3500)
CONTINGENCY_QTY = 800

#: Well status mix per programme year: (mostly-Confirmed, balanced, mostly-Planned).
STATUS_MIX = {
    0: [DemandStatus.CONFIRMED] * 8 + [DemandStatus.BUDGETED, DemandStatus.PLANNED],
    1: [DemandStatus.CONFIRMED] * 4 + [DemandStatus.BUDGETED] * 3 + [DemandStatus.PLANNED] * 3,
    2: [DemandStatus.CONFIRMED] * 1 + [DemandStatus.BUDGETED] * 2 + [DemandStatus.PLANNED] * 7,
}

#: (customer key, asset name, well prefix)
ASSETS = {
    "equinor": [("Johan Sverdrup Phase 3", "JS"), ("Troll West Infill", "TW")],
    "akerbp": [("Valhall Flank North", "VF"), ("Skarv Satellite", "SK")],
}


def _reset(db) -> None:
    print("--reset: deleting all existing rows before re-seeding")
    for table_name in _DELETE_ORDER:
        table = Base.metadata.tables.get(table_name)
        if table is None:
            continue
        deleted = db.execute(table.delete()).rowcount
        if deleted:
            print(f"  cleared {table_name} ({deleted} rows)")


def _seed_lead_times(db) -> None:
    """Attribute lead-time components covering the whole catalogue, so every
    product is fully modelled (Unrecoverable and order-by dates both work)."""
    rows = [
        (LeadTimeDimension.OD_WT, ANY_ATTRIBUTE_VALUE, 3.0, "Ex-mill (standard)"),
        (LeadTimeDimension.OD_WT, "20 133", 4.0, "Ex-mill 20\" conductor"),
        (LeadTimeDimension.GRADE, ANY_ATTRIBUTE_VALUE, 1.0, "Carbon grades"),
        (LeadTimeDimension.GRADE, "SM110XS", 2.5, "Sour-service mill slot"),
        (LeadTimeDimension.GRADE, "L80-13CR", 2.0, "13CR mill slot"),
        (LeadTimeDimension.CONNECTION, ANY_ATTRIBUTE_VALUE, 1.0, "Threading"),
        (LeadTimeDimension.LOGISTICS, ANY_ATTRIBUTE_VALUE, 1.5, "Sailing + yard"),
    ]
    for dimension, value, months, label in rows:
        db.add(LeadTimeComponent(
            dimension=dimension, attribute_value=value, months=months, label=label,
        ))
    db.flush()


def _month_offset(months: int, day: int = 15) -> datetime:
    total = NOW.month - 1 + months
    year = NOW.year + total // 12
    month = total % 12 + 1
    return NOW.replace(year=year, month=month, day=min(day, 28))


def _seed_world(db):
    bu = BusinessUnit(name="SCEU Norway")
    db.add(bu)
    db.flush()

    equinor = Customer(
        name="Equinor Norway", allocation_policy=AllocationPolicy.SOFT,
        business_unit_id=bu.id,
    )
    akerbp = Customer(
        name="AkerBP Norway", allocation_policy=AllocationPolicy.HYBRID,
        business_unit_id=bu.id,
    )
    db.add_all([equinor, akerbp])
    db.flush()

    products = {}
    for number, spec in CATALOGUE.items():
        product = Product(unit_of_measure=UnitOfMeasure.MTR, **spec)
        db.add(product)
        products[number] = product
    db.flush()

    for from_no, to_no in TECH_SUBS:
        db.add(TechnicalSubstitution(
            from_product_id=products[from_no].id,
            to_product_id=products[to_no].id,
        ))
    # Customer rules mirror the technical pairs for BOTH customers, so a
    # proposal can clear the customer layer and reach well-level approval.
    for customer in (equinor, akerbp):
        for from_no, to_no in TECH_SUBS:
            db.add(CustomerSubstitutionRule(
                customer_id=customer.id,
                from_product_id=products[from_no].id,
                to_product_id=products[to_no].id,
                allowed=True,
            ))
    db.flush()

    # ---- wells: ~10 per customer per year, three years -------------------
    wells = []
    for customer_key, customer in (("equinor", equinor), ("akerbp", akerbp)):
        nodes = []
        for asset_name, prefix in ASSETS[customer_key]:
            node = PlanningNode(
                customer_id=customer.id, parent_id=None,
                node_type="Asset", name=asset_name,
            )
            db.add(node)
            nodes.append((node, prefix))
        db.flush()

        for year in range(3):
            statuses = STATUS_MIX[year]
            for i, status in enumerate(statuses):
                # Weighted, not round-robin: real programmes favour some
                # assets and designs over others.
                node, prefix = RNG.choice(nodes)
                well = Well(
                    planning_node_id=node.id,
                    name=f"{prefix}-{chr(65 + year)}{i + 1:02d}",
                    demand_status=status,
                )
                db.add(well)
                db.flush()

                design_primary, contingency = RNG.choices(
                    DESIGNS, weights=(3, 3, 2, 2, 2)
                )[0]
                quantities = QTY_5 if len(design_primary) == 5 else QTY_4
                # ROS spread through the programme year with a +/-1 month
                # wobble and a random day, so the two customers never line up
                # on the same date. Year 1 starts ~2 months in the PAST: a
                # live programme has wells mid-execution, and the Executive
                # demand trend needs demand inside its prior-period windows
                # to have anything to compare against.
                ros = _month_offset(
                    max(-3, year * 12 - 2 + (i * 12) // len(statuses)
                        + RNG.choice((-1, 0, 0, 1))),
                    day=RNG.randint(1, 28),
                )
                # Strings phase across the well: the conductor lands first,
                # each deeper string a few weeks later, tubing last.
                line_ros = ros
                line_qtys = [
                    _jitter_qty(quantities[p]) for p in range(len(design_primary))
                ]
                # TBG longest is a stated principle of this world -- enforce it
                # after the jitter rather than hoping.
                if max(line_qtys) > line_qtys[-1]:
                    line_qtys[-1] = max(line_qtys) + 50 * RNG.randint(1, 6)
                for position, item in enumerate(design_primary):
                    if position:
                        line_ros = line_ros + timedelta(days=RNG.randint(10, 35))
                    db.add(DemandLine(
                        well_id=well.id,
                        product_id=products[item].id,
                        quantity=line_qtys[position],
                        ros_date=line_ros,
                        profile=DemandProfile.PRIMARY,
                    ))
                if contingency is not None:
                    db.add(DemandLine(
                        well_id=well.id,
                        product_id=products[contingency].id,
                        quantity=_jitter_qty(CONTINGENCY_QTY),
                        ros_date=line_ros,   # staged with the last string
                        profile=DemandProfile.CONTINGENCY,
                    ))
                wells.append(well)
        db.flush()

    return bu, equinor, akerbp, products


def _confirmed_demand_between(db, product_id, start, end) -> float:
    lines = (
        db.query(DemandLine)
        .join(Well, DemandLine.well_id == Well.id)
        .filter(
            DemandLine.product_id == product_id,
            Well.demand_status == DemandStatus.CONFIRMED,
            DemandLine.ros_date >= start,
            DemandLine.ros_date < end,
        )
        .all()
    )
    return sum(l.quantity for l in lines)


#: On-hand as a share of the next TWELVE months of confirmed demand, per item
#: (owner ruling 2026-09-06, the demo re-baseline after D01). The old shape --
#: six months of stock with a 60,000 m mountain of the 20" conductor -- left the
#: Business-Unit-wide pass with 2 of 27 confirmed wells covered and nothing for
#: the approval flow to show. The conductor surplus is redistributed into the
#: strings that were short; items still deliberately run SHORT so gaps,
#: Unrecoverable verdicts and the substitution programme all have live cases:
#:
#:   1  mild surplus (a cheap conductor is held ahead of need, not 5 years of it)
#:   2  roughly a year of stock; its own uncovered lines carry the APPROVED 2->3
#:      proposals, which item 3's headroom then turns into CoveredViaSubstitute
#:   5, 7, 9, 10  SHORT -- 5 is the critical string with a safety stock and the
#:      5->6 rule, 9 is the 13CR tubing whose substitute is item 8
#:   4, 8  sized later by `_seed_hard_assignments` for the two Oracle stories
ON_HAND_SHARE_OF_12M = {
    1: 1.10, 2: 1.00, 5: 0.65, 6: 0.95, 7: 0.85, 9: 0.75, 10: 0.65,
    # placeholders, overwritten by the story sizing below
    4: 1.00, 8: 1.00,
}
#: Item 3 is the one substitute-side item that must hold steel AFTER every
#: confirmed line of the three-year programme has drawn (that residual is what an
#: approved substitute draws on, since the pass evaluates every confirmed well at
#: once). Two item-2 lines' worth -- the two approved 2->3 proposals.
ITEM_3_HEADROOM = 2 * QTY_4[1]
#: Item 8's residual after every confirmed line has drawn: what a 9->8
#: substitute candidate can actually see. Just above a typical 13CR tubing line
#: (3,500 m nominal, jittered), so the PENDING 9->8 proposals fit under it and
#: the over-subscription note has several claimants on the same steel.
ITEM_8_RESIDUAL = 4000.0


def _seed_inventory(db, bu, products):
    """On-hand = `ON_HAND_SHARE_OF_12M` x the next 12 months of confirmed demand
    (item 3: all confirmed demand + `ITEM_3_HEADROOM`; items 4 and 8 are resized
    by the hard-assignment stories). On-order = months 7-12 of confirmed demand
    (exceptions: item 4 has NO row -> unknown; one item-9 PO is undated)."""
    six_months = _month_offset(6, day=1)
    twelve_months = _month_offset(12, day=1)

    # On-hand sizing includes the recent-past ROS wells (the programme runs
    # ~2 months into the past): their steel was delivered, so a well that
    # already spudded should read Covered, not as a fake gap.
    window_start = NOW - timedelta(days=95)
    far_future = NOW + timedelta(days=365 * 10)
    for number, product in products.items():
        next12 = _confirmed_demand_between(
            db, product.id, window_start, twelve_months)
        if number == 3:
            every_confirmed = _confirmed_demand_between(
                db, product.id, window_start - timedelta(days=365), far_future)
            on_hand = round(every_confirmed + ITEM_3_HEADROOM)
        else:
            # Uneven on purpose: real stock never sits at exactly the share.
            on_hand = round(
                next12 * ON_HAND_SHARE_OF_12M[number] * RNG.uniform(0.96, 1.04)
            ) or 2000
        db.add(InventoryOnHand(
            business_unit_id=bu.id, product_id=product.id,
            quantity=float(on_hand), source_system="synthetic",
        ))

        m7to12 = _confirmed_demand_between(db, product.id, six_months, twelve_months)
        if number == 4:
            continue  # NO purchase-order row at all: on-order UNKNOWN, not zero
        if m7to12 <= 0 and number != 9:
            # An explicit zero-quantity row would be odd for a demo; absence of
            # need simply means no PO was raised for most items.
            continue
        if number == 9:
            half = max(1000.0, m7to12 / 2)
            db.add(InventoryOnOrder(
                business_unit_id=bu.id, product_id=product.id,
                quantity=half,
                expected_arrival_date=_month_offset(8, day=RNG.randint(2, 26)),
                booking_status="Booked", source_system="synthetic",
            ))
            db.add(InventoryOnOrder(     # raised but not acknowledged: undated
                business_unit_id=bu.id, product_id=product.id,
                quantity=half,
                expected_arrival_date=None,
                booking_status="PO", source_system="synthetic",
            ))
        else:
            # PO sizes and promise dates wobble like real mill confirmations.
            db.add(InventoryOnOrder(
                business_unit_id=bu.id, product_id=product.id,
                quantity=float(round(m7to12 * RNG.uniform(0.8, 1.1))),
                expected_arrival_date=_month_offset(
                    RNG.randint(6, 11), day=RNG.randint(2, 26)),
                booking_status=RNG.choice(("Booked", "PO")),
                source_system="synthetic",
            ))
    db.flush()


def _seed_customer_owned(db, equinor, akerbp, products):
    """Small parcels only -- never enough to cover a single well's line."""
    for customer, items in ((equinor, {5: 400, 9: 300}), (akerbp, {6: 350, 8: 250})):
        upload = CustomerOwnedInventoryUpload(
            customer_id=customer.id,
            filename=f"{customer.name.split()[0].lower()}_owned_inventory_declaration.xlsx",
            sheet_name="Declared positions",
            row_count=len(items), applied_count=len(items), created_count=len(items),
            replaced_count=0, error_count=0, uploaded_at=NOW - timedelta(days=30),
        )
        db.add(upload)
        db.flush()
        for number, quantity in items.items():
            db.add(CustomerOwnedInventory(
                customer_id=customer.id, product_id=products[number].id,
                quantity=float(quantity), source_system="customer-upload",
                upload_id=upload.id,
                # The as-of date of the count -- the screen prints it per row.
                uploaded_at=upload.uploaded_at,
            ))
    db.flush()


def _seed_safety_stocks(db, products):
    """Three products carry an explicit safety stock so the Administration tab
    and the MOR trigger ("orders fire when the balance dips below safety, not
    zero") both have a live demo case. Item 2's floor is the one the demo
    scenario's programme-growth story pushes through (an amber safety dip on
    Order Requirements becoming a physical runout)."""
    for number, quantity, note in (
        (2, 1200.0, "Surface casing floor -- the demo scenario's growth story "
                    "turns this dip into a runout."),
        (5, 1500.0, "Critical string -- runs short; keep a buffer while the "
                    "substitution programme is live."),
        (8, 1000.0, "High-runner TBG; one well's tubing as a floor."),
    ):
        db.add(SafetyStock(
            product_id=products[number].id, quantity=quantity, note=note,
        ))
    db.flush()
    print("safety stocks: items 2, 5 and 8")


def _seed_revision_history(db, products):
    """A believable change history, through the PRODUCTION engines only.

    * Every line's automatic revision 1 is backdated to when its well would
      have entered the plan (the demand did not appear today).
    * ~10 lines then receive a real quantity/ROS revision via apply_revision.
    * One budgeted year-1 well is promoted to CONFIRMED via
      set_well_demand_status -- the dramatic entry on the Home card.

    created_at is server-defaulted by design, so the seed backdates the rows
    it just created by UPDATE afterwards -- acceptable here because this is
    demo data construction, not a production write path.
    """
    # 1. Backdate the automatic initial revisions -- and the line's own
    #    created_at to match, because the Executive demand trend reconstructs
    #    the prior book via `line.created_at > as_of` before it ever reads
    #    revisions (executive._state_as_of). Leaving lines stamped "today"
    #    would make every prior period read as empty.
    for revision in db.query(DemandRevision).all():
        ts = NOW - timedelta(days=RNG.randint(90, 240), hours=RNG.randint(0, 23))
        revision.created_at = ts
        line = db.get(DemandLine, revision.demand_line_id)
        if line is not None and revision.revision_no == 1:
            line.created_at = ts
    db.flush()

    def _backdate_latest(line_id, impact, days_ago):
        ts = NOW - timedelta(days=days_ago, hours=RNG.randint(1, 23))
        impact.created_at = ts
        latest = (
            db.query(DemandRevision)
            .filter(DemandRevision.demand_line_id == line_id)
            .order_by(DemandRevision.revision_no.desc())
            .first()
        )
        if latest is not None:
            latest.created_at = ts

    # 2. Quantity / ROS revisions on confirmed+budgeted lines, spread over
    #    the last two months so the Home card shows a stream, not a burst.
    lines = (
        db.query(DemandLine)
        .join(Well, DemandLine.well_id == Well.id)
        .filter(Well.demand_status.in_(
            [DemandStatus.CONFIRMED, DemandStatus.BUDGETED]))
        .order_by(DemandLine.ros_date)
        .all()
    )
    picked = RNG.sample(lines, min(10, len(lines)))
    revised = 0
    for line in picked:
        if RNG.random() < 0.6:   # the well got deeper / shallower
            new_qty = _jitter_qty(line.quantity, 0.8, 1.3)
            new_ros = line.ros_date
        else:                    # the rig schedule moved
            new_qty = line.quantity
            new_ros = line.ros_date + timedelta(days=RNG.choice((-45, -30, 30, 60)))
        impact = apply_revision(
            db, line, quantity=new_qty, ros_date=new_ros, profile=line.profile)
        _backdate_latest(line.id, impact, days_ago=RNG.randint(2, 60))
        revised += 1

    # 3. One budgeted year-1 well is confirmed -- new near-term demand.
    promoted = (
        db.query(Well)
        .join(DemandLine, DemandLine.well_id == Well.id)
        .filter(Well.demand_status == DemandStatus.BUDGETED)
        .order_by(DemandLine.ros_date)
        .first()
    )
    if promoted is not None:
        change = set_well_demand_status(db, promoted, DemandStatus.CONFIRMED)
        ts = NOW - timedelta(days=1, hours=4)
        for rev_id in change.revision_ids:
            row = db.get(DemandRevision, rev_id)
            if row is not None:
                row.created_at = ts
        for imp_id in change.impact_record_ids:
            row = db.get(ImpactRecord, imp_id)
            if row is not None:
                row.created_at = ts
        print(f"promoted {change.well_name}: "
              f"{change.status_before} -> {change.status_after}")
    db.flush()
    print(f"revision history: {revised} line revisions backdated over 60 days")


def _seed_hard_assignments(db, bu, products, equinor, akerbp):
    """Oracle HARD-ALLOCATION demo cases (InventoryAssignment rows).

    Two stories, both ending in "release the hard assignment in Oracle":

    1. COVERAGE PATH -- a LATER AkerBP (HYBRID) well holds a hard assignment
       while an EARLIER-ROS Equinor (SOFT) well needs the same steel. The
       product's on-hand is resized so the Equinor line comes back UNCOVERED
       with the coverage reason "...hard-assigned to another customer's
       demand line ... releasing it would close the gap -- Release the hard
       assignment in Oracle..." (coverage._shortage_phrase).

    2. SUBSTITUTION PATH -- an AkerBP line hard-claims part of item 8's
       headroom, so an uncovered Equinor item-9 line sees its substitute
       candidate as "Blocked -- Oracle release required"
       (substitution.BLOCK_ORACLE_RELEASE).

    Runs AFTER the revision history so a later revision cannot move the two
    paired wells and break the earlier/later relationship.
    """
    def _confirmed_lines(product_no, customer):
        return (
            db.query(DemandLine)
            .join(Well, DemandLine.well_id == Well.id)
            .join(PlanningNode, Well.planning_node_id == PlanningNode.id)
            .filter(
                PlanningNode.customer_id == customer.id,
                Well.demand_status == DemandStatus.CONFIRMED,
                DemandLine.product_id == products[product_no].id,
                DemandLine.profile == DemandProfile.PRIMARY,
            )
            .order_by(DemandLine.ros_date)
            .all()
        )

    def _assign(line, quantity):
        db.add(InventoryAssignment(
            demand_line_id=line.id,
            product_id=line.product_id,
            quantity=float(quantity),
            source_system="synthetic",   # keeps assigned_source honest
            source_reference=None,
            synced_at=None,
        ))

    # ---- case 1: pick a product both customers confirm on, AkerBP later --
    made_coverage_case = False
    for product_no in (4, 3, 2, 6, 8, 1):
        eq_lines = _confirmed_lines(product_no, equinor)
        ak_lines = _confirmed_lines(product_no, akerbp)
        if not eq_lines or not ak_lines:
            continue
        later_ak = ak_lines[-1]
        earlier_eq = [l for l in eq_lines if l.ros_date < later_ak.ros_date]
        if not earlier_eq:
            continue
        eq_line = earlier_eq[-1]   # latest Equinor line still before AkerBP's
        _assign(later_ak, later_ak.quantity)
        # Resize on-hand so the free pool (on_hand - assignment) runs out at
        # the Equinor line by ~40% of its quantity: short, but the assigned
        # steel would close the gap -- exactly the release-recommendation
        # branch of coverage._shortage_phrase.
        demand_before = sum(
            l.quantity
            for l in _confirmed_lines(product_no, equinor)
            + _confirmed_lines(product_no, akerbp)
            if l.ros_date <= eq_line.ros_date and l.id != later_ak.id
        )
        free = max(0.0, demand_before - round(eq_line.quantity * 0.4))
        row = (
            db.query(InventoryOnHand)
            .filter(
                InventoryOnHand.business_unit_id == bu.id,
                InventoryOnHand.product_id == products[product_no].id,
            )
            .one()
        )
        row.quantity = free + later_ak.quantity
        print(
            f"hard-assignment demo (coverage): item {product_no}, "
            f"{later_ak.quantity:.0f} assigned to well {later_ak.well.name} "
            f"(ROS {later_ak.ros_date.date()}), earlier well "
            f"{eq_line.well.name} (ROS {eq_line.ros_date.date()}) left short"
        )
        made_coverage_case = True
        break
    if not made_coverage_case:
        print("WARNING: no product pair found for the hard-assignment "
              "coverage demo", file=sys.stderr)

    # ---- case 2: item 8 (substitute FOR item 9) scarce + hard-claimed ----
    # One AkerBP well hard-claims an item-8 parcel and the remaining free
    # pool is smaller than any item-9 line needs, so an uncovered item-9
    # line's candidate shows "Blocked -- Oracle release required":
    # available < required <= available + hard_assigned
    # (substitution.BLOCK_ORACLE_RELEASE). This deliberately makes item 8
    # SCARCE -- its own late-ROS wells go short too, which is the same
    # hard-allocation pain seen from the other side.
    ak8 = [
        l for l in _confirmed_lines(8, akerbp)
        if l.ros_date <= _month_offset(8)
    ]
    if ak8:
        claim = ak8[-1]
        _assign(claim, claim.quantity)
        # The Oracle-release block only outranks lower layers once customer
        # AND well approval are already granted (substitution.BLOCK_PRIORITY)
        # -- so grant them on ONE uncovered item-9 line: approved on paper,
        # blocked by the hard assignment, exactly the story to demo.
        # Must be an EQUINOR line: the assignment only blocks a FOREIGN
        # customer's candidate (a same-customer assignment is not "reserved
        # elsewhere" -- see coverage._hard_assigned_for_substitutes).
        # The residual left for substitutes: pending 9->8 proposals on lines
        # no larger than this come back PENDING_APPROVAL (steel exists,
        # approval outstanding), while the ONE approved line is chosen LARGER
        # than it -- so that one is short by less than the claimed parcel and
        # is blocked pending Oracle release.
        residual = ITEM_8_RESIDUAL
        nine_line = max(
            (
                l for l in _confirmed_lines(9, equinor)
                if residual < l.quantity <= residual + claim.quantity
            ),
            key=lambda l: l.quantity,
            default=None,
        )
        # Size item 8 so the story is TRUE under Business-Unit-wide allocation
        # (D01): every confirmed item-8 line of either customer draws the pool
        # first, in ROS order, so what a substitute candidate can see is the
        # residual AFTER all of them. The residual is set just below the
        # approved item-9 line's need, and the hard-claimed parcel would close
        # the gap:  available < required <= available + hard_assigned
        # (substitution.BLOCK_ORACLE_RELEASE). Sizing it to "claim + 3000", as
        # this used to, left nothing after item 8's own wells and the candidate
        # came back blocked by inventory instead -- a different, weaker story.
        own_demand = sum(
            l.quantity
            for l in _confirmed_lines(8, equinor) + _confirmed_lines(8, akerbp)
        ) + sum(
            l.quantity
            for l in db.query(DemandLine)
            .join(Well, DemandLine.well_id == Well.id)
            .filter(
                Well.demand_status == DemandStatus.CONFIRMED,
                DemandLine.product_id == products[8].id,
                DemandLine.profile == DemandProfile.CONTINGENCY,
            )
        )
        row = (
            db.query(InventoryOnHand)
            .filter(
                InventoryOnHand.business_unit_id == bu.id,
                InventoryOnHand.product_id == products[8].id,
            )
            .one()
        )
        row.quantity = own_demand + residual
        print(f"hard-assignment demo (substitution): item 8, "
              f"{claim.quantity:.0f} claimed by well {claim.well.name}, "
              f"on-hand set to own confirmed demand {own_demand:.0f} + "
              f"residual {residual:.0f}")
        if nine_line is not None:
            approval = request_approval(
                db, nine_line,
                from_product_id=products[9].id,
                to_product_id=products[8].id,
            )
            decide_approval(db, approval.id, approved=True)
            print(f"  approved 9->8 on well {nine_line.well.name} -- "
                  "candidate now blocked pending Oracle release")
    else:
        print("WARNING: no AkerBP item-8 line for the substitution-block "
              "demo", file=sys.stderr)
    db.flush()


def _seed_substitution_proposals(db, products):
    """3 PENDING + a few APPROVED, raised through the production engines on
    lines whose product is the FROM side of a rule. Confirmed wells only, so
    every proposal is visible on the default coverage screens."""
    from_ids = {products[f].id: (f, t) for f, t in TECH_SUBS}
    from app.models import CoverageResult, CoverageStatus

    candidates = (
        db.query(DemandLine)
        .join(Well, DemandLine.well_id == Well.id)
        .join(CoverageResult, CoverageResult.demand_line_id == DemandLine.id)
        .filter(
            Well.demand_status == DemandStatus.CONFIRMED,
            DemandLine.product_id.in_(list(from_ids)),
            # Proposals belong on lines that NEED one: the engine has already
            # marked lines PENDING_APPROVAL (substitute stock exists, approval
            # outstanding) or UNCOVERED -- covered lines would leave the
            # approval invisible on every default screen.
            CoverageResult.status.in_(
                [CoverageStatus.UNCOVERED, CoverageStatus.PENDING_APPROVAL]
            ),
        )
        .order_by(DemandLine.ros_date)
        .all()
    )
    pending_target, approved_target = 3, 2
    made_pending = made_approved = 0
    used_wells = set()

    def _raise(line, approve):
        nonlocal made_pending, made_approved
        f, t = from_ids[line.product_id]
        approval = request_approval(
            db, line,
            from_product_id=products[f].id,
            to_product_id=products[t].id,
        )
        used_wells.add(line.well_id)
        if approve:
            decide_approval(db, approval.id, approved=True)
            made_approved += 1
        else:
            made_pending += 1

    # Every confirmed well of the three-year programme draws in the one pass,
    # so a substitute only has steel for a proposal if its product holds a
    # residual AFTER all of them: item 3 does (`ITEM_3_HEADROOM`, for the
    # APPROVED 2->3 proposals -> CoveredViaSubstitute) and item 8 does
    # (`ITEM_8_RESIDUAL`, for the PENDING 9->8 proposals -> PendingApproval).
    # Proposals raised anywhere else would be honest but invisible: the line
    # would stay Uncovered with "substitute also short", which is not what a
    # demo of the approval flow needs to show first.
    preferred = {
        True: [l for l in candidates if from_ids[l.product_id][0] == 2],
        False: [
            l for l in candidates
            if from_ids[l.product_id][0] == 9 and l.quantity <= ITEM_8_RESIDUAL
        ],
    }
    for approve, target in ((True, approved_target), (False, pending_target)):
        for line in preferred[approve]:
            if (made_approved if approve else made_pending) >= target:
                break
            if line.well_id in used_wells:
                continue
            _raise(line, approve)
    # Fallback for a world where the preferred lines ran out: earliest first.
    for line in candidates:
        if made_approved >= approved_target and made_pending >= pending_target:
            break
        if line.well_id in used_wells:
            continue
        _raise(line, approve=made_approved < approved_target)
    print(f"substitution proposals: {made_pending} pending, {made_approved} approved")
    db.flush()


def _seed_demo_scenario(db, bu, products, equinor, akerbp):
    """ONE draft scenario with an override from every family, resolved by well
    name and product so a re-seed rebuilds it (it used to be hand-made through
    the API against fixed ids, and a `--reset` silently dropped it).

    Each override is checked with the SAME validators the API applies
    (`app.engines.overrides.validate`, `app.engines.scenario
    .assert_target_in_scope`), so nothing here can create a shape the screen
    would refuse. A story whose target does not exist in this world is skipped
    with a note rather than guessed.
    """
    def _well(name):
        return db.query(Well).filter(Well.name == name).one_or_none()

    def _line(well, product_no, profile=DemandProfile.PRIMARY):
        if well is None:
            return None
        return next(
            (l for l in well.demand_lines
             if l.product_id == products[product_no].id and l.profile == profile),
            None,
        )

    scenario = Scenario(
        name="AkerBP full replan — demand, supply & approvals what-if",
        description="",   # written below, from what was actually resolved
        business_unit_id=bu.id,
        status=ScenarioStatus.DRAFT,
        created_by="Demo",
    )
    db.add(scenario)
    db.flush()

    stories: list[str] = []
    overrides: list[ScenarioOverride] = []

    def _add(override, story):
        validate_override(override, bu)
        assert_target_in_scope(db, scenario, override)
        overrides.append(override)
        stories.append(story)

    # (1) programme growth -- two CSG 13-3/8 L80 lines grow, which turns the
    #     amber safety-stock dip on Order Requirements into a physical runout.
    grown = []
    for well_name, new_qty in (("SK-A03", 1600.0), ("VF-A06", 1700.0)):
        line = _line(_well(well_name), 2)
        if line is None:
            print(f"  scenario: no {well_name} item-2 line; growth story skipped")
            continue
        _add(ScenarioOverride(
            scenario_id=scenario.id, target_kind=ScenarioTargetKind.DEMAND_LINE,
            target_demand_line_id=line.id, field_name="quantity",
            value_number=new_qty,
            note=f"{well_name} {line.quantity:.0f} -> {new_qty:.0f}: programme growth",
        ), None)
        grown.append(f"{well_name} {line.quantity:,.0f}→{new_qty:,.0f}")
    if grown:
        stories[-len(grown):] = []
        stories.append(
            f"programme growth — {' and '.join(grown)} Mtr of "
            f"{products[2].description}, turning the amber safety-stock dip on "
            "Order Requirements into a physical runout"
        )

    # (2) acceleration -- a Planned year-3 well's tubing pulled into next year,
    #     and the well itself firmed up so the pull-in enters the confirmed scope.
    fast_well = _well("SK-C09")
    fast_line = _line(fast_well, 8)
    if fast_line is not None:
        pulled_to = _month_offset(12, day=1)
        _add(ScenarioOverride(
            scenario_id=scenario.id, target_kind=ScenarioTargetKind.DEMAND_LINE,
            target_demand_line_id=fast_line.id, field_name="ros_date",
            value_date=pulled_to,
            note=(f"SK-C09 campaign accelerated: "
                  f"{fast_line.ros_date:%b %Y} -> {pulled_to:%b %Y}"),
        ), None)
        _add(ScenarioOverride(
            scenario_id=scenario.id, target_kind=ScenarioTargetKind.WELL,
            target_well_id=fast_well.id, field_name="demand_status",
            value_text=DemandStatus.CONFIRMED.value,
            note=("SK-C09 campaign firms up — enters the confirmed programme "
                  "(pairs with its ROS pull-in)"),
        ), None)
        stories[-2:] = []
        stories.append(
            f"acceleration — SK-C09 {fast_line.quantity:,.0f} Mtr of "
            f"{products[8].description} pulled from {fast_line.ros_date:%b %Y} to "
            f"{pulled_to:%b %Y}, and the well firmed up into the confirmed programme"
        )
    else:
        print("  scenario: no SK-C09 item-8 line; acceleration story skipped")

    # (3) programme slip -- a confirmed well drops to Budgeted and frees its steel.
    slip_well = _well("SK-A04")
    if slip_well is not None and slip_well.demand_status == DemandStatus.CONFIRMED:
        _add(ScenarioOverride(
            scenario_id=scenario.id, target_kind=ScenarioTargetKind.WELL,
            target_well_id=slip_well.id, field_name="demand_status",
            value_text=DemandStatus.BUDGETED.value,
            note="SK-A04 slips out of the firm programme",
        ), "programme slip — well SK-A04 drops from Confirmed to Budgeted, "
           "leaving the firm demand scope and freeing its steel")
    else:
        print("  scenario: SK-A04 not a confirmed well; slip story skipped")

    # (4) mill slip -- item 3's purchase order lands six months late.
    po = (
        db.query(InventoryOnOrder)
        .filter(
            InventoryOnOrder.business_unit_id == bu.id,
            InventoryOnOrder.product_id == products[3].id,
            InventoryOnOrder.expected_arrival_date.isnot(None),
        )
        .order_by(InventoryOnOrder.expected_arrival_date)
        .first()
    )
    if po is not None:
        was = po.expected_arrival_date
        late = datetime(was.year + (was.month + 5) // 12,
                        (was.month + 5) % 12 + 1, 15)
        _add(ScenarioOverride(
            scenario_id=scenario.id, target_kind=ScenarioTargetKind.PO_ARRIVAL,
            target_product_id=products[3].id, field_name="arrival_date",
            value_date=late,
            note=(f"Mill slips the {po.quantity:,.0f} Mtr {products[3].description} "
                  f"delivery {was:%b %Y} -> {late:%b %Y}"),
        ), f"mill slip — the {po.quantity:,.0f} Mtr {products[3].description} PO "
           f"slides from {was:%b %Y} to {late:%b %Y}")
    else:
        print("  scenario: no dated item-3 PO; mill-slip story skipped")

    # (5) Oracle release -- the hard-claimed item-8 parcel goes back to the pool.
    claim = (
        db.query(InventoryAssignment)
        .filter(InventoryAssignment.product_id == products[8].id)
        .first()
    )
    if claim is not None:
        _add(ScenarioOverride(
            scenario_id=scenario.id, target_kind=ScenarioTargetKind.ASSIGNMENT,
            target_demand_line_id=claim.demand_line_id,
            target_product_id=claim.product_id, field_name="quantity",
            value_number=0.0,
            note=(f"What if Oracle releases the {claim.quantity:,.0f} Mtr "
                  f"hard-assigned to {claim.demand_line.well.name}"),
        ), f"Oracle release — the {claim.quantity:,.0f} Mtr hard assignment on "
           f"{claim.demand_line.well.name} released to the shared pool")
    else:
        print("  scenario: no item-8 assignment; Oracle-release story skipped")

    # (6) customer decision -- the earliest pending substitution approved.
    from app.models import SubstitutionApprovalStatus, WellSubstitutionApproval
    pending = (
        db.query(WellSubstitutionApproval)
        .join(DemandLine, DemandLine.id == WellSubstitutionApproval.demand_line_id)
        .filter(WellSubstitutionApproval.status == SubstitutionApprovalStatus.PENDING)
        .order_by(DemandLine.ros_date)
        .first()
    )
    if pending is not None:
        well_name = pending.demand_line.well.name
        _add(ScenarioOverride(
            scenario_id=scenario.id,
            target_kind=ScenarioTargetKind.SUBSTITUTION_APPROVAL,
            target_demand_line_id=pending.demand_line_id,
            target_to_product_id=pending.to_product_id,
            field_name="approval_status",
            value_text=SubstitutionApprovalStatus.APPROVED.value,
            note=f"Customer approves the pending substitute on {well_name}",
        ), f"customer decision — the pending substitution on {well_name} approved")
    else:
        print("  scenario: no pending approval; customer-decision story skipped")

    for override in overrides:
        db.add(override)
    scenario.description = (
        "The platform-wide demo scenario, one override per story: "
        + "; ".join(f"({i}) {text}" for i, text in enumerate(stories, start=1))
        + ". Preview shows how coverage, allocation and MRP move together — "
        "nothing is saved until Apply."
    )
    scenario.version = 1 + len(overrides)
    db.flush()
    print(f"demo scenario: {len(overrides)} overrides across {len(stories)} stories")
    return scenario


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="DESTRUCTIVE: delete every row, then re-seed")
    args = parser.parse_args(argv)

    if not inspect(engine).has_table("products"):
        print("Schema missing. Run `alembic upgrade head` first.", file=sys.stderr)
        return 1

    db = SessionLocal()
    try:
        if args.reset:
            _reset(db)
        elif db.query(Product).count():
            print("Database is not empty. Re-run with --reset to rebuild.",
                  file=sys.stderr)
            return 1

        _seed_lead_times(db)
        bu, equinor, akerbp, products = _seed_world(db)
        _seed_inventory(db, bu, products)
        _seed_customer_owned(db, equinor, akerbp, products)
        _seed_safety_stocks(db, products)
        db.commit()

        # Official verdicts, via the one production implementation.
        for customer in (equinor, akerbp):
            recompute_customer(db, customer)
        db.commit()

        # History before proposals, so proposals target the FINAL demand
        # picture (a revision after the fact could flip a proposal's line).
        _seed_revision_history(db, products)
        db.commit()

        # Hard allocations after the history (so revisions cannot move the
        # paired wells) and before proposals (so candidate blocking is
        # computed against the final assignment picture).
        _seed_hard_assignments(db, bu, products, equinor, akerbp)
        db.commit()
        for customer in (equinor, akerbp):
            recompute_customer(db, customer)
        db.commit()

        _seed_substitution_proposals(db, products)
        db.commit()
        # Proposals change PendingApproval verdicts -> recompute once more.
        for customer in (equinor, akerbp):
            recompute_customer(db, customer)
        db.commit()

        # The what-if scenario LAST, against the final picture (its approval
        # override points at a proposal raised just above).
        _seed_demo_scenario(db, bu, products, equinor, akerbp)
        db.commit()

        wells = db.query(Well).count()
        lines = db.query(DemandLine).count()
        print(f"seeded: 1 BU, 2 customers, {len(products)} products, "
              f"{wells} wells, {lines} demand lines")

        # ---- demo-case audit: every feature must have something to show ----
        from app.models import CoverageResult, CoverageStatus
        # Inside the lead-time fence a gap comes back UNRECOVERABLE, not
        # Uncovered -- both are "the demo opens with a problem", so both count.
        # Six months: the re-baselined world (2026-09-06) deliberately covers
        # the wells already spudded and the next few, so the opening problems
        # sit between the overdue Oracle-release well and month five.
        horizon = NOW + timedelta(days=180)
        early_gaps = (
            db.query(Well.id).distinct()
            .join(DemandLine, DemandLine.well_id == Well.id)
            .join(CoverageResult, CoverageResult.demand_line_id == DemandLine.id)
            .filter(
                CoverageResult.status.in_(
                    [CoverageStatus.UNCOVERED, CoverageStatus.UNRECOVERABLE]),
                DemandLine.ros_date <= horizon,
            )
            .count()
        )
        impacts = db.query(ImpactRecord).count()
        print(f"demo audit: {early_gaps} wells with a coverage gap inside "
              f"180 days, {impacts} impact records")
        if early_gaps < 3:
            print("WARNING: fewer than 3 near-term gap wells -- "
                  "the demo opens flat", file=sys.stderr)
        if impacts < 8:
            print("WARNING: fewer than 8 impact records -- the Demand changes "
                  "card looks empty", file=sys.stderr)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
