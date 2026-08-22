import enum
import uuid

from sqlalchemy import Boolean, Column, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import relationship

from app.db import Base


class UserRole(str, enum.Enum):
    """Two roles only, on purpose.

    ADMIN    sees every Business Unit and may use the Administration screens.
    PLANNER  is confined to ONE Business Unit (`business_unit_id` is required
             for planners -- see the invariant note on the column).

    There is deliberately no per-customer role yet: the security hierarchy the
    spec names is BU -> Customer, and the BU wall is the one the domain calls
    absolute (see app.models.customer.Customer's docstring). Customer-level
    narrowing can be added as a *further* restriction later without changing
    any enforcement call site, because every check goes through
    `app.auth.deps.visible_customer_ids` rather than comparing roles inline.
    """

    ADMIN = "admin"
    PLANNER = "planner"


def _uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    """A platform login.

    Authentication is pluggable (see app.auth.provider): in dev mode the
    password hash on this row is checked; under Entra ID the row is matched by
    email and `password_hash` is ignored. The row itself -- and the scoping
    columns -- are the part that stays the same across providers, which is the
    whole point of keeping identity in our own table.

    Invariant (enforced in app.auth.deps, not by the schema): a PLANNER must
    have `business_unit_id` set. NULL business_unit_id on an ADMIN means "all
    BUs"; NULL on a planner is a misconfiguration and every request by that
    user is refused with an explanation, never silently widened to all BUs --
    the same conservative-failure choice as Customer.business_unit_id.
    """

    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=_uuid)
    email = Column(String, nullable=False, unique=True)
    display_name = Column(String, nullable=False)
    role = Column(SAEnum(UserRole), nullable=False, default=UserRole.PLANNER)
    # NULL means "all Business Units" for an ADMIN and "misconfigured" for a
    # PLANNER -- see the class docstring.
    business_unit_id = Column(
        String(36), ForeignKey("business_units.id"), nullable=True
    )
    # Dev-mode credential only. PBKDF2 ("pbkdf2_sha256$<iter>$<salt>$<hex>",
    # see app.auth.passwords). Nullable because an Entra-provisioned user has
    # no local password; a NULL hash can never authenticate in dev mode.
    password_hash = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)

    business_unit = relationship("BusinessUnit")
