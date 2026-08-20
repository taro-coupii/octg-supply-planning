from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.business_unit import _uuid


class TechnicalSubstitution(Base):
    """spec §データモデル: technical_substitutions. Directed pair; self-substitution forbidden."""

    __tablename__ = "technical_substitutions"
    __table_args__ = (
        UniqueConstraint("from_product_id", "to_product_id", name="uq_technical_substitutions_from_to"),
        CheckConstraint("from_product_id != to_product_id", name="from_ne_to"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    from_product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    to_product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))


class CustomerSubstitutionRule(Base):
    """spec §データモデル: customer_substitution_rules."""

    __tablename__ = "customer_substitution_rules"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "technical_substitution_id", name="uq_customer_substitution_rules_customer_substitution"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"))
    technical_substitution_id: Mapped[str] = mapped_column(ForeignKey("technical_substitutions.id"))
    allowed: Mapped[bool] = mapped_column(Boolean)
