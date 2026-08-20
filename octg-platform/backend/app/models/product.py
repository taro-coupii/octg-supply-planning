import enum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.business_unit import _uuid


class UnitOfMeasure(enum.Enum):
    MTR = "Mtr"
    PC = "PC"
    MT = "MT"


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    unit_of_measure: Mapped[UnitOfMeasure] = mapped_column(
        SAEnum(UnitOfMeasure, values_callable=lambda e: [m.value for m in e])
    )
    # COMPROMISE[C-06R]: nullable weight means MT conversion is not guaranteed for every product
    weight_kg: Mapped[float | None] = mapped_column(Float)
