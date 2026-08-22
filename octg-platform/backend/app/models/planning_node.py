from sqlalchemy import Column, ForeignKey, String
from sqlalchemy.orm import relationship

from app.db import Base
from app.models.customer import _uuid


class PlanningNode(Base):
    """Generic hierarchy node between Customer and Well.

    node_type is a free-text label (Project, Campaign, Pad, Development Phase,
    Rig Program, ...) -- deliberately not an enum, per discovery: "Planning
    Nodes are generic. The application should not hardcode these."
    """

    __tablename__ = "planning_nodes"

    id = Column(String(36), primary_key=True, default=_uuid)
    customer_id = Column(String(36), ForeignKey("customers.id"), nullable=False)
    parent_id = Column(String(36), ForeignKey("planning_nodes.id"), nullable=True)
    node_type = Column(String, nullable=False)
    name = Column(String, nullable=False)

    customer = relationship("Customer")
    parent = relationship("PlanningNode", remote_side=[id])
    wells = relationship("Well", back_populates="planning_node")
