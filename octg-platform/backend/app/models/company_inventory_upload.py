from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db import Base
from app.models.customer import _uuid


class CompanyInventoryUpload(Base):
    """One accepted spreadsheet restating ON-HAND quantities for ONE Business Unit.

    THE AUDIT TRAIL FOR A MAINTENANCE WRITE ONTO WHAT IS OTHERWISE AN ORACLE TABLE
    -------------------------------------------------------------------------------
    `InventoryOnHand` is a read-only Oracle projection everywhere else in this
    platform (see its docstring). This upload exists only because there is, in the
    MVP, no Oracle feed at all -- see `app.engines.company_inventory` for the full
    statement of the gate that keeps this from becoming a second write path once a
    real feed lands: a row whose `source_system` is not `"synthetic"` or `"manual"`
    can never be touched by this upload, so an Oracle-fed row becomes automatically
    read-only again the moment the feed is connected, with no code change here.

    Modelled directly on `app.models.customer_owned_inventory
    .CustomerOwnedInventoryUpload`: same columns, same meaning, same reason for
    existing (it is the audit trail AND the record that an upload happened at all).
    A separate table rather than a shared one because the two uploads restate
    completely different rows (customer-owned stock vs. a Business Unit's own
    on-hand figure) and mixing their history would make "which BU/customer does
    this row belong to" a nullable-column question instead of a schema fact.
    """

    __tablename__ = "company_inventory_uploads"

    id = Column(String(36), primary_key=True, default=_uuid)
    business_unit_id = Column(
        String(36), ForeignKey("business_units.id"), nullable=False
    )
    filename = Column(String, nullable=True)
    sheet_name = Column(String, nullable=True)
    #: Rows the file contained, rows that produced a position, and rows refused
    #: with a per-row error. `row_count == applied_count + error_count` always.
    row_count = Column(Integer, nullable=False, default=0)
    applied_count = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)
    replaced_count = Column(Integer, nullable=False, default=0)
    created_count = Column(Integer, nullable=False, default=0)
    uploaded_at = Column(DateTime, nullable=False, server_default=func.now())
    #: Always "manual" -- a human produced this file. Present anyway, and named the
    #: same as every other provenance column in the platform, so a reader does not
    #: have to remember that this one table's value is a foregone conclusion.
    source_system = Column(String, nullable=False, default="manual")

    business_unit = relationship("BusinessUnit")
