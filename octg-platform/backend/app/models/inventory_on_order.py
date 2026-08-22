from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import relationship

from app.db import Base
from app.models.customer import _uuid


class InventoryOnOrder(Base):
    """Material ALREADY ON ORDER for ONE product into ONE Business Unit.

    One row per expected arrival, i.e. per purchase-order line, so the
    `expected_arrival_date` horizon is a real property of a row rather than a
    single date squeezed onto a per-product total. Several rows for one
    (Business Unit, product) pair are normal and correct -- that is what a
    delivery schedule is -- which is exactly why this table carries NO unique
    constraint on the pair, unlike
    `app.models.inventory_on_hand.InventoryOnHand`, where the pair IS the fact.

    SYSTEM BOUNDARY -- read-only local projection
    --------------------------------------------
    Oracle owns Purchase Orders and Receipts, alongside Product Master,
    Inventory and Inventory Assignments. Rows here are a PROJECTION of that
    Oracle-owned data, exactly like `InventoryOnHand` and
    `app.models.inventory_assignment.InventoryAssignment`: this platform READS
    them and never writes them. No user-facing mutation endpoint is exposed --
    a purchase order is raised in Oracle, and a platform that let a planner
    edit its own copy would be claiming ownership of a fact it does not own.
    Until the feed exists the rows present are synthetic demo data, which is
    what `source_system` / `synced_at` keep visible.

    A MISSING ROW MEANS UNKNOWN, AND THAT IS NOT THE SAME AS ZERO
    ------------------------------------------------------------
    Same convention as `InventoryOnHand`: absence is silence, not a measured
    zero. "This BU has nothing on order of this product" is a FACT and is
    recorded as an explicit `quantity = 0` row; "we hold no on-order data for
    this pair" is the absence of any row. `app.engines.inventory.on_order_map`
    keeps the two apart with `known`, and the Executive Dashboard renders them
    differently -- "nothing on order" versus "no on-order data".

    WHY ABSENCE DOES NOT RAISE, WHERE A MISSING on_hand ROW DOES
    -----------------------------------------------------------
    `on_hand_for` raises `InventoryRowMissing` because coverage CANNOT be
    decided without the quantity: every honest partial verdict would still be a
    verdict computed from an invented number. On-order is different in kind. It
    is not an input to any verdict -- coverage, substitution and allocation all
    ignore it -- and "nothing is on order" is the ordinary state of most
    products in a catalogue, so demanding a row for every (BU, product) pair
    would mean seeding tens of thousands of rows whose only content is "no".
    Raising would therefore convert a normal, expected absence into an outage of
    the dashboard, MRP and every screen behind them.

    So the refusal is expressed at the level where it belongs: the figure is
    reported UNAVAILABLE with a reason, per the Executive Dashboard's rule that
    a number is either measured or explicitly absent. Nothing anywhere reads a
    missing row as 0.

    PROVENANCE STAYS TWO INDEPENDENT FLAGS
    --------------------------------------
    `source_system` distinguishes "synthetic" (seeded demo data) from "oracle"
    (the real feed), and it is the ONLY thing that may claim the latter. It is
    deliberately NOT collapsed into `app.engines.mrp.InventoryPosition
    .oracle_integrated`, which continues to mean "the Oracle feed is live" --
    and it is not. The pair mirrors what `InventoryPosition.assigned_source`
    already does for assignments: one flag for whether the integration exists,
    one for where THIS number came from. Collapsing them would force a lie in
    one direction or the other -- either flipping `oracle_integrated` True on
    the strength of demo rows, or discarding data the dashboard is already
    reporting.
    """

    __tablename__ = "inventory_on_order"
    #: An INDEX, not a unique constraint -- see the class docstring. Every read goes
    #: through `app.engines.inventory.on_order_rows`, which filters on
    #: business_unit_id and usually also on product_id.
    __table_args__ = (
        Index("ix_inventory_on_order_bu_product", "business_unit_id", "product_id"),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    business_unit_id = Column(
        String(36), ForeignKey("business_units.id"), nullable=False
    )
    product_id = Column(String(36), ForeignKey("products.id"), nullable=False)
    #: Quantity still expected to arrive, in the product's own unit
    #: (`app.models.product.Product.unit_of_measure` -- there is no second unit
    #: for a purchase order). An explicit 0 states "nothing on order", which is a
    #: fact; see the class docstring.
    quantity = Column(Float, nullable=False, default=0)
    #: When this quantity is expected to land. NULLABLE, and null means the
    #: promise date is genuinely not known yet -- a real state for a PO that has
    #: been raised but not acknowledged. It is not defaulted to today, which
    #: would put unscheduled steel inside every arrival horizon on the dashboard.
    expected_arrival_date = Column(DateTime, nullable=True)
    #: PO'ed vs Booked -- the workbook's distinction between "purchase order
    #: raised" and "shipment booked/confirmed". Read-only projection like every
    #: other column here; NULL means the feed did not state it (older rows),
    #: which is reported as such, never defaulted to either value.
    booking_status = Column(String, nullable=True)

    # Provenance of this projected row -- "oracle" once the real feed lands,
    # "synthetic" for seeded demo data. Never inferred.
    source_system = Column(String, nullable=False, default="synthetic")
    # Oracle's own purchase-order identifier, when known.
    source_reference = Column(String, nullable=True)
    synced_at = Column(DateTime, nullable=True)

    business_unit = relationship("BusinessUnit")
    product = relationship("Product")
