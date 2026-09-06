"""Who changed a company-inventory row inline, and what it was before.

`InventoryOnHand`, `InventoryOnOrder` and `InventoryAssignment` are read-only
projections of Oracle-owned data with a documented manual-maintenance exception
(MVP_COMPROMISES C-03). Until this table, an inline edit through that exception
left NO record of who made it: the write returned before/after to the caller and
forgot it (adversarial review 2026-09-06, F09). Every inline set / create / delete
now appends one row here, with the AUTHENTICATED user -- never a name from the
request body -- and the row's before/after as the write saw them.

Append-only. Nothing reads it for a business figure; it exists so a number that
surprises someone can be traced to a person and a moment.
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.orm import relationship

from app.db import Base
from app.models.customer import _uuid


class CompanyInventoryEdit(Base):
    __tablename__ = "company_inventory_edits"

    id = Column(String(36), primary_key=True, default=_uuid)
    #: "InventoryOnHand" | "InventoryOnOrder" | "InventoryAssignment"
    row_kind = Column(String, nullable=False)
    row_id = Column(String(36), nullable=False)
    #: "set" | "create" | "delete"
    action = Column(String, nullable=False)
    before_json = Column(Text, nullable=True)
    after_json = Column(Text, nullable=True)
    # No FOREIGN KEY on purpose: this is the RECORD of who acted, and it must
    # survive that user later being removed. Resolved view-only.
    user_id = Column(String(36), nullable=True)
    edited_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    user = relationship(
        "User",
        primaryjoin="foreign(CompanyInventoryEdit.user_id) == User.id",
        viewonly=True,
    )
