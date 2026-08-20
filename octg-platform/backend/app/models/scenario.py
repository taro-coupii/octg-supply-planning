import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.business_unit import _uuid


class ScenarioStatus(enum.Enum):
    DRAFT = "Draft"
    APPLIED = "Applied"


class ScenarioOverrideKind(enum.Enum):
    QUANTITY = "quantity"
    ROS_DATE = "ros_date"
    WELL_STATUS = "well_status"
    PO_ARRIVAL = "po_arrival"
    HARD_RELEASE = "hard_release"
    APPROVAL_FLIP = "approval_flip"


class Scenario(Base):
    """spec §データモデル: scenarios."""

    __tablename__ = "scenarios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[ScenarioStatus] = mapped_column(
        SAEnum(ScenarioStatus, values_callable=lambda e: [m.value for m in e]),
        default=ScenarioStatus.DRAFT,
        server_default=ScenarioStatus.DRAFT.value,
        nullable=False,
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ScenarioOverride(Base):
    """spec §データモデル: scenario_overrides. target_id holds the UUID of the affected row;
    the API response must always resolve and return the target's name."""

    __tablename__ = "scenario_overrides"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scenario_id: Mapped[str] = mapped_column(ForeignKey("scenarios.id"), nullable=False)
    kind: Mapped[ScenarioOverrideKind] = mapped_column(
        SAEnum(ScenarioOverrideKind, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
