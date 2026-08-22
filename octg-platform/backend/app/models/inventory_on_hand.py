from sqlalchemy import Column, DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db import Base
from app.models.customer import _uuid


class InventoryOnHand(Base):
    # MVP-COMPROMISE[C-02]: this table is a seeded projection, not a live Oracle feed.
    #     WHY:    no Oracle interface exists yet; the demo seeds these rows.
    #     REMOVE: connect the Oracle inventory feed; rows then arrive with
    #             source_system="oracle" and oracle_integrated flips True.
    """On-hand quantity of ONE product inside ONE Business Unit.

    Why a table and not a column on Product
    ---------------------------------------
    A Product is a CATALOGUE entity -- one row describes "CSG 9-5/8 53.5 P110
    VAM 21 SMLS" for everybody. The quantity standing on the shelf is not a
    property of the catalogue entry, it is a property of (BU, product). Putting
    it on Product forced a single global scalar (`Product.on_hand_qty`) that two
    different BUs both read as their own, which is precisely the leak this table
    closes.

    SYSTEM BOUNDARY -- read-only local projection
    --------------------------------------------
    Oracle owns Inventory, alongside Product Master, Inventory Assignments, POs
    and Receipts. Rows here are a PROJECTION of Oracle-owned data, exactly like
    app.models.inventory_assignment.InventoryAssignment: this platform reads them
    to decide coverage and must never treat its own copy as authoritative. No
    user-facing mutation endpoints are exposed -- writes belong in Oracle. Until
    the feed exists the rows present are synthetic demo data, which is what
    `source_system` / `synced_at` keep visible.

    THE ONLY SOURCE -- the legacy fallback is retired
    ------------------------------------------------
    Resolution is done in exactly one place, app.engines.inventory.on_hand_for,
    and there is now exactly one rule: a row here for
    (business_unit_id, product_id) is the answer. There is nothing to fall back
    to. `Product.on_hand_qty` -- the legacy unscoped scalar this table's earlier
    docstring carried a retirement TODO for -- has been DROPPED, column included,
    so it can no longer be a second source of truth.

    The consequence is that a MISSING row is not zero. Absence means the quantity
    is unknown, and `on_hand_for` raises `InventoryRowMissing` rather than
    inventing a number; a customer with no Business Unit raises
    `InventoryScopeMissing`, because it has no pool at all. Every product a BU's
    demand touches therefore needs a row here -- including an explicit
    `quantity=0` row to state "this BU holds none of it", which is a fact and is
    not the same as silence.
    """

    __tablename__ = "inventory_on_hand"
    __table_args__ = (
        UniqueConstraint(
            "business_unit_id", "product_id", name="uq_inventory_on_hand_bu_product"
        ),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    business_unit_id = Column(
        String(36), ForeignKey("business_units.id"), nullable=False
    )
    product_id = Column(String(36), ForeignKey("products.id"), nullable=False)
    quantity = Column(Float, nullable=False, default=0)

    # Provenance of this projected row -- "oracle" once the real feed lands,
    # "synthetic" for seeded demo data. Never inferred.
    source_system = Column(String, nullable=False, default="synthetic")
    synced_at = Column(DateTime, nullable=True)

    business_unit = relationship("BusinessUnit")
    product = relationship("Product")
