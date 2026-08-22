from sqlalchemy import Column, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import relationship

from app.db import Base
from app.models.customer import _uuid


class InventoryAssignment(Base):
    """A quantity of a product physically assigned to ONE demand line.

    SYSTEM BOUNDARY -- this table is a read-only LOCAL PROJECTION of
    Oracle-owned data, not a system of record. Inventory Assignments sit under
    Oracle's ownership in the spec alongside Product Master, Inventory, POs and
    Receipts. Rows are expected to arrive via the Oracle inventory feed; this
    platform reads them to decide coverage under the HARD and HYBRID allocation
    policies and must never treat its own copy as authoritative. Until that feed
    is built the rows present are synthetic demo data -- see
    `source_system` / `synced_at`, and `InventoryPosition.assigned_source` in
    app.engines.mrp, which keeps that distinction visible to the UI.

    No user-facing mutation endpoints are exposed for this table beyond what the
    demo seed needs; writes belong in Oracle.
    """

    __tablename__ = "inventory_assignments"

    id = Column(String(36), primary_key=True, default=_uuid)
    demand_line_id = Column(String(36), ForeignKey("demand_lines.id"), nullable=False)
    product_id = Column(String(36), ForeignKey("products.id"), nullable=False)
    quantity = Column(Float, nullable=False, default=0)

    # Provenance of this projected row. "oracle" once the real feed lands;
    # "synthetic" for seeded demo data. Never inferred -- always written by
    # whatever produced the row.
    source_system = Column(String, nullable=False, default="synthetic")
    # Oracle's own assignment identifier, when known.
    source_reference = Column(String, nullable=True)
    # When this projection was last refreshed from the source system.
    synced_at = Column(DateTime, nullable=True)

    demand_line = relationship("DemandLine")
    product = relationship("Product")
