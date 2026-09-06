import enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db import Base
from app.models.customer import _uuid


class SubstitutionApprovalStatus(str, enum.Enum):
    APPROVED = "Approved"
    PENDING = "Pending"
    REJECTED = "Rejected"


class TechnicalSubstitution(Base):
    """Layer 1 -- engineering says from_product may be substituted by
    to_product. Customer-agnostic and DIRECTIONAL: A->B does not imply B->A.

    ONE ROW PER ORDERED PAIR
    ------------------------
    `uq_technical_substitution_pair` exists because this table is now WRITEABLE from
    `app.api.admin` and a duplicate is not merely untidy. `find_candidates` iterates
    `tech_rows` and appends one `SubstitutionCandidate` per row, so a second row for
    (A, B) makes B appear TWICE in the candidate list for every demand line on A --
    the same substitute offered as though it were two independent options, with the
    same quantity counted against each. A planner reading that list would see twice
    the choice that physically exists.

    The constraint is on the ORDERED pair, deliberately: the directionality in the
    first paragraph is real, so (A, B) and (B, A) are two different engineering
    claims and both may legitimately exist. The pre-check in `app.api.admin` turns a
    collision into a 409 that names the existing row; this constraint is what makes
    that guarantee true rather than usually true when two requests race.
    """

    __tablename__ = "technical_substitutions"
    __table_args__ = (
        UniqueConstraint(
            "from_product_id",
            "to_product_id",
            name="uq_technical_substitution_pair",
        ),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    from_product_id = Column(String(36), ForeignKey("products.id"), nullable=False)
    to_product_id = Column(String(36), ForeignKey("products.id"), nullable=False)

    from_product = relationship("Product", foreign_keys=[from_product_id])
    to_product = relationship("Product", foreign_keys=[to_product_id])


class CustomerSubstitutionRule(Base):
    """Layer 2 -- whether this customer generally permits a substitution pair.
    `allowed=False` is an explicit veto, not merely an absent rule.

    A TRUE ALLOW-LIST: absence means NOT PERMITTED
    ----------------------------------------------
    `app.engines.substitution.find_candidates` computes
    ``customer_allowed = bool(rule is not None and rule.allowed)``, so a pair with no
    row at all is blocked with `BLOCK_CUSTOMER` exactly as an `allowed=False` row is.
    That is why DELETE of a rule is a real state change and not a cleanup: removing an
    `allowed=True` row reverts the pair to blocked. `allowed=False` and "no row" reach
    the same verdict by different routes -- the first is a recorded customer decision,
    the second is silence -- which is why PATCH exists alongside DELETE.

    ONE ROW PER (customer, from, to)
    --------------------------------
    `uq_customer_substitution_rule_pair` matters more here than on
    `TechnicalSubstitution`. `find_candidates` builds a dict keyed on
    `to_product_id`, so with two rows for one triple the surviving one is whichever
    the query happened to return LAST -- meaning an explicit customer veto could be
    silently overridden by a stale `allowed=True` row, or vice versa, with no way to
    tell from any screen which one was in force.
    """

    __tablename__ = "customer_substitution_rules"
    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "from_product_id",
            "to_product_id",
            name="uq_customer_substitution_rule_pair",
        ),
    )

    id = Column(String(36), primary_key=True, default=_uuid)
    customer_id = Column(String(36), ForeignKey("customers.id"), nullable=False)
    from_product_id = Column(String(36), ForeignKey("products.id"), nullable=False)
    to_product_id = Column(String(36), ForeignKey("products.id"), nullable=False)
    allowed = Column(Boolean, nullable=False, default=False)

    customer = relationship("Customer")
    from_product = relationship("Product", foreign_keys=[from_product_id])
    to_product = relationship("Product", foreign_keys=[to_product_id])


class WellSubstitutionApproval(Base):
    """Layer 3 -- approval for one SPECIFIC demand line to use one specific
    substitution pair."""

    __tablename__ = "well_substitution_approvals"

    id = Column(String(36), primary_key=True, default=_uuid)
    demand_line_id = Column(String(36), ForeignKey("demand_lines.id"), nullable=False)
    from_product_id = Column(String(36), ForeignKey("products.id"), nullable=False)
    to_product_id = Column(String(36), ForeignKey("products.id"), nullable=False)
    status = Column(
        SAEnum(SubstitutionApprovalStatus),
        nullable=False,
        default=SubstitutionApprovalStatus.PENDING,
    )
    requested_at = Column(DateTime, nullable=False, server_default=func.now())
    decided_at = Column(DateTime, nullable=True)
    #: Server-recorded actors (F09): the authenticated user who raised the
    #: request and the one who decided it. Never taken from a request body.
    # No FOREIGN KEY on purpose: this is the RECORD of who acted, and it must
    # survive that user later being removed. Resolved view-only.
    requested_by_user_id = Column(String(36), nullable=True)
    decided_by_user_id = Column(String(36), nullable=True)

    demand_line = relationship("DemandLine")
    requested_by_user = relationship(
        "User",
        primaryjoin="foreign(WellSubstitutionApproval.requested_by_user_id) == User.id",
        viewonly=True,
    )
    decided_by_user = relationship(
        "User",
        primaryjoin="foreign(WellSubstitutionApproval.decided_by_user_id) == User.id",
        viewonly=True,
    )

    @property
    def requested_by_user_name(self) -> str | None:
        u = self.requested_by_user
        return u.display_name if u else None

    @property
    def decided_by_user_name(self) -> str | None:
        u = self.decided_by_user
        return u.display_name if u else None
    from_product = relationship("Product", foreign_keys=[from_product_id])
    to_product = relationship("Product", foreign_keys=[to_product_id])
