"""Shared corpus for the Phase 5 API tests (demand list, coverage grid,
executive dashboard, demand import).

One fixture builder rather than four copies, because the four screens have to
agree about the same data -- the coverage grid's counts and the executive
dashboard's coverage percentage are only meaningfully testable against a shared,
documented world.

The world it builds
-------------------
DEMAND STATUS IS A PROPERTY OF THE WELL
---------------------------------------
`Well.demand_status`, not `DemandLine.status` -- so each well below has ONE status
and every line of it is at that status. The corpus previously put a Planned line
inside WELL-1 alongside a Confirmed one, which is exactly the state that no longer
exists (and exactly the state that made the product owner's coverage grid
unreadable). That line now has a well of its own, WELL-5, at Planned.

    BU-1
      Acme  (SOFT)
        Project Alpha
          Pad A
            WELL-1  [Confirmed]
                     L1  P-A  5000  ROS +30d   Primary  -> Covered
            WELL-5  [Planned]   <- out of scope, WHOLE WELL
                     L3  P-A  2000  ROS +10d   Primary  -> no verdict at all
          WELL-2  [Confirmed]
                     L2  P-B  3000  ROS +400d  Primary  -> Uncovered
                     L5  P-B  1000  ROS +20d   Primary  -> Unrecoverable
          WELL-4  [Budgeted]    <- out of scope, WHOLE WELL
                     L6  P-A   500  ROS +45d   Primary  -> no verdict at all
      Beta  (SOFT)
        Project Beta
          WELL-3  [Confirmed]
                     L4  P-A  1000  ROS +200d  Primary  -> Covered

  P-A InventoryOnHand(BU-1) = 6000, grade_type "Carbon", NO lead-time component
      matches it on ANY dimension (so a shortfall is Uncovered, never
      Unrecoverable -- absent lead-time data means "cannot judge", per
      app.engines.order_dates.is_recoverable).
  P-B InventoryOnHand(BU-1) = 0 (an explicit "this BU holds none", which is a
      fact -- not the same as no row, which is unknown and raises),
      grade_type "13CR", 12 months of lead time from a COMPLETE set of the four
      attribute dimensions (so a near-ROS shortfall IS Unrecoverable).

Three facts this shape is designed to expose:

  * WELL-4 and WELL-5 have demand but none of it IN SCOPE, so their rollups are
    None -- unevaluated, neither covered nor uncovered.
  * Under the DEFAULT filters WELL-1 is Covered (5000 of 6000 consumed). Widen the
    status filter to include Planned and the WHOLE of WELL-5 joins the pool; L3's
    ROS (+10d) is EARLIER than L1's (+30d), so under earliest-ROS-first L3 takes
    2000 and L1 is left 4000 of the 5000 it needs -- WELL-1 becomes UNCOVERED. The
    filters do not merely hide rows, they change who competes for the same steel,
    which is exactly why the coverage grid must not answer a non-default request
    from the stored default-filter rows.

    L3's ROS was moved from +60d to +10d when it moved off WELL-1, deliberately, to
    KEEP that flip. On its own well at +60d it would have lost the race to L1 and
    WELL-1 would have stayed Covered, quietly deleting the only case in this corpus
    where widening a filter changes an existing well's verdict.
  * A status filter moves WHOLE WELLS. Widening to Planned adds WELL-5 and all of
    its lines together; there is no longer any way for a line to leave its well
    behind, which is the readability the move to `Well.demand_status` bought.
"""

from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.engines.coverage import recompute_customer
from app.main import app
from app.models import (
    BusinessUnit,
    Customer,
    DemandLine,
    DemandProfile,
    DemandStatus,
    InventoryOnHand,
    LeadTimeComponent,
    LeadTimeDimension,
    PlanningNode,
    Product,
    UnitOfMeasure,
    Well,
    AllocationPolicy,
)

NOW = datetime.utcnow()


class World:
    """Ids of everything the tests need, so no test re-queries by name."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def build_world(session_factory) -> World:
    db = session_factory()
    try:
        bu = BusinessUnit(name="BU-1")
        db.add(bu)
        db.flush()

        acme = Customer(
            name="Acme", business_unit_id=bu.id, allocation_policy=AllocationPolicy.SOFT
        )
        beta = Customer(
            name="Beta", business_unit_id=bu.id, allocation_policy=AllocationPolicy.SOFT
        )
        db.add_all([acme, beta])
        db.flush()

        alpha = PlanningNode(customer_id=acme.id, node_type="Project", name="Project Alpha")
        beta_node = PlanningNode(
            customer_id=beta.id, node_type="Project", name="Project Beta"
        )
        db.add_all([alpha, beta_node])
        db.flush()
        pad_a = PlanningNode(
            customer_id=acme.id, parent_id=alpha.id, node_type="Pad", name="Pad A"
        )
        db.add(pad_a)
        db.flush()

        p_a = Product(
            unit_of_measure=UnitOfMeasure.MTR,
            type="CSG", size="9-5/8", grade="P110", grade_type="Carbon",
            connection="VAM 21", description="CSG 9-5/8 P110 VAM 21",
        )
        p_b = Product(
            unit_of_measure=UnitOfMeasure.MTR,
            type="TBG", size="4-1/2", grade="13CR80", grade_type="13CR",
            connection="VAM TOP", description="TBG 4-1/2 13CR80 VAM TOP",
        )
        db.add_all([p_a, p_b])
        db.flush()
        # The ONLY place a quantity can live. There is no unscoped column left to
        # mirror, and a product with no row for this BU would make coverage raise.
        db.add_all(
            [
                InventoryOnHand(business_unit_id=bu.id, product_id=p_a.id, quantity=6000),
                InventoryOnHand(business_unit_id=bu.id, product_id=p_b.id, quantity=0),
            ]
        )
        # 12 months of lead time for P-B, as a COMPLETE attribute component set
        # (OD/WT + Grade + Connection + Logistics). All four are required: an
        # incomplete set resolves to "not modelled", total 0, which would make
        # L5 merely Uncovered and silently gut the Unrecoverable half of this
        # world. See app.engines.lead_time.
        #
        # Every value is keyed to a P-B attribute and NONE of them is a "*"
        # wildcard, which is what keeps P-A (9-5/8, Carbon, VAM 21) matching
        # nothing on any dimension -- i.e. still deliberately unmodelled.
        # P-B has no weight, so its OD/WT key is its size alone.
        db.add_all([
            LeadTimeComponent(
                dimension=LeadTimeDimension.OD_WT, attribute_value="4-1/2",
                months=10, label="Ex-mill",
            ),
            LeadTimeComponent(
                dimension=LeadTimeDimension.GRADE, attribute_value="13CR", months=0
            ),
            LeadTimeComponent(
                dimension=LeadTimeDimension.CONNECTION, attribute_value="VAM TOP",
                months=0,
            ),
            LeadTimeComponent(
                dimension=LeadTimeDimension.LOGISTICS, attribute_value="13CR",
                months=2, label="Sailing",
            ),
        ])
        db.flush()

        w1 = Well(planning_node_id=pad_a.id, name="WELL-1", demand_status=DemandStatus.CONFIRMED)
        w2 = Well(planning_node_id=alpha.id, name="WELL-2", demand_status=DemandStatus.CONFIRMED)
        w3 = Well(planning_node_id=beta_node.id, name="WELL-3", demand_status=DemandStatus.CONFIRMED)
        # BUDGETED, so its whole demand is out of the default scope -- the ONLY
        # thing WELL-4 is for (an evaluated=False rollup). The status is on the WELL
        # now, so it is stated here rather than on its line.
        w4 = Well(planning_node_id=alpha.id, name="WELL-4", demand_status=DemandStatus.BUDGETED)
        # WELL-5 is NEW. It carries L3, the Planned line that used to sit inside
        # WELL-1 beside a Confirmed one -- a shape that is now unrepresentable, since
        # a well has one status. See the module docstring.
        w5 = Well(planning_node_id=pad_a.id, name="WELL-5", demand_status=DemandStatus.PLANNED)
        db.add_all([w1, w2, w3, w4, w5])
        db.flush()

        def line(well, product, qty, days, profile):
            obj = DemandLine(
                well_id=well.id,
                product_id=product.id,
                quantity=qty,
                ros_date=NOW + timedelta(days=days),
                profile=profile,
            )
            db.add(obj)
            return obj

        l1 = line(w1, p_a, 5000, 30, DemandProfile.PRIMARY)
        l2 = line(w2, p_b, 3000, 400, DemandProfile.PRIMARY)
        # On WELL-5 (Planned) at ROS +10d -- EARLIER than L1, so widening the status
        # filter still flips WELL-1 to Uncovered. See the module docstring.
        l3 = line(w5, p_a, 2000, 10, DemandProfile.PRIMARY)
        l4 = line(w3, p_a, 1000, 200, DemandProfile.PRIMARY)
        l5 = line(w2, p_b, 1000, 20, DemandProfile.PRIMARY)
        # WELL-4's only line. The well is BUDGETED, so this is out of scope under the
        # platform defaults -- which is the ONLY thing it is for (WELL-4 exists to be
        # a well with demand but no in-scope demand, i.e. an UNEVALUATED rollup).
        #
        # The exclusion used to be expressed on the line (a Budgeted line, and before
        # that a Contingency one). It is expressed on the WELL now, which is where
        # status lives; the fixture's design intent is unchanged.
        #
        # Contingency's IN-scope behaviour is pinned directly, in
        # tests/test_coverage_engine.py, where it belongs -- it is an engine rule.
        l6 = line(w4, p_a, 500, 45, DemandProfile.PRIMARY)
        db.flush()

        recompute_customer(db, acme)
        recompute_customer(db, beta)
        db.commit()

        return World(
            bu_id=bu.id,
            acme_id=acme.id,
            beta_id=beta.id,
            alpha_id=alpha.id,
            pad_a_id=pad_a.id,
            p_a_id=p_a.id,
            p_b_id=p_b.id,
            p_a_desc=p_a.description,
            p_b_desc=p_b.description,
            w1_id=w1.id,
            w2_id=w2.id,
            w3_id=w3.id,
            w4_id=w4.id,
            w5_id=w5.id,
            l1_id=l1.id,
            l2_id=l2.id,
            l3_id=l3.id,
            l4_id=l4.id,
            l5_id=l5.id,
            l6_id=l6.id,
        )
    finally:
        db.close()


def build_client(with_world: bool = True):
    """(TestClient, session_factory, World|None) over a fresh in-memory sqlite DB."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    world = build_world(session_factory) if with_world else None
    return TestClient(app), session_factory, world
