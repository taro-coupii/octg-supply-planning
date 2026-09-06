from sqlalchemy import Column, String, UniqueConstraint

from app.db import Base
from app.models.customer import _uuid


class BusinessUnit(Base):
    """Top of the security / data hierarchy: Business Unit -> Customer -> ...

    A Business Unit is the OUTERMOST inventory boundary and it is never crossed,
    by any code path, under any circumstance -- not by coverage, not by the
    substitution fall-through, and not by the HARD/HYBRID assignment netting.

    It is also the UNIT OF ALLOCATION (product-owner ruling 2026-09-06, D01):
    every in-scope demand line of every customer below it competes for one pool,
    earliest ROS first, so what the BU has promised can never exceed what it
    holds. Customer used to be a second, inner boundary and is not one any more --
    what remains customer-private is ownership, not pooling: a customer's own
    uploaded stock and an Oracle assignment are never drawn by anybody else. The
    only thing allowed to reason across
    Business Units is nothing at all.

    THE NAME IS UNIQUE, and it became so when the table became WRITEABLE
    -------------------------------------------------------------------
    Seed data held one row per BU by construction; nothing could create a second
    "Tubular North America". `POST /business-units` can, so the constraint exists --
    the same division of labour `uq_technical_substitution_pair` has (see revision
    ``d5f2a4b91c70``): the endpoint pre-checks and answers 409 with a sentence naming
    the existing row, and the constraint is the backstop for two concurrent requests
    that both pass the pre-check.

    It is not tidiness. The NAME is the only handle a human has on a Business Unit --
    the id is a uuid, and every screen, every remap dropdown and every coverage
    provenance label renders the name. Two BUs called the same thing would put an
    operator one indistinguishable click away from remapping a customer into the wrong
    inventory pool, which is the one edit on the Administration screen that changes
    which warehouse a customer's coverage is computed from. The constraint is
    case-SENSITIVE (sqlite would need a functional index otherwise); the endpoint's
    pre-check is case-INSENSITIVE, so "TNA" and "tna" are refused there rather than
    stored as two rows that only a database could tell apart.
    """

    __tablename__ = "business_units"
    __table_args__ = (UniqueConstraint("name", name="uq_business_unit_name"),)

    id = Column(String(36), primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
