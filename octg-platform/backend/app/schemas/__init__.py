from pydantic import BaseModel, ConfigDict


class BusinessUnitNode(BaseModel):
    id: str
    name: str
    children: list["BusinessUnitNode"] = []


BusinessUnitNode.model_rebuild()


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    business_unit_id: str | None


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    unit_of_measure: str
    weight_kg: float | None
