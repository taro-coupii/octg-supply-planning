from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.business_unit import _uuid


class Setting(Base):
    """spec §データモデル: settings. Holds coverage_scope_statuses / coverage_scope_profiles defaults
    (裁定SC-1: single settings table)."""

    __tablename__ = "settings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    key: Mapped[str] = mapped_column(String(200), unique=True)
    value: Mapped[str] = mapped_column(String)
