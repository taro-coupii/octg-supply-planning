"""GET /products -- the catalogue picker.

The point of these tests is the reason the endpoint exists at all: a picker built
from demand cannot reach a product nothing demands, and the two products that
demonstrate the platform refusing to invent a number are exactly that. If this
endpoint ever starts filtering by demand, these fail.
"""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app
from app.models import Product, UnitOfMeasure


def _client():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    db = TestingSessionLocal()
    db.add_all(
        [
            Product(
                unit_of_measure=UnitOfMeasure.MTR,
                type="TBG", size="4-1/2", weight=12.6, grade="13CR80",
                grade_type="13CR", connection="VAM TOP",
                description="TBG 4-1/2 12.6 13CR80 VAM TOP SMLS", active=True,
            ),
            # Demanded by nothing -- the case a demand-derived picker would miss.
            Product(
                unit_of_measure=UnitOfMeasure.MTR,
                type="CSG", size="7", weight=32.0, grade="L80",
                grade_type="Carbon", connection="Hydril 563",
                description="CSG 7 32.0 L80 Hydril 563 SMLS", active=True,
            ),
            Product(
                unit_of_measure=UnitOfMeasure.MTR,
                type="CSG", size="9-5/8", weight=53.5, grade="P110",
                grade_type="Carbon", connection="VAM 21",
                description="CSG 9-5/8 53.5 P110 VAM 21 SMLS", active=False,
            ),
        ]
    )
    db.commit()
    db.close()
    return TestClient(app)


def test_lists_active_catalogue_products_including_undemanded_ones():
    client = _client()
    rows = client.get("/products").json()

    descriptions = [r["description"] for r in rows]
    # No demand line exists anywhere in this fixture, so every row here is proof
    # the endpoint is catalogue-driven rather than demand-driven.
    assert "CSG 7 32.0 L80 Hydril 563 SMLS" in descriptions
    assert "TBG 4-1/2 12.6 13CR80 VAM TOP SMLS" in descriptions
    app.dependency_overrides.clear()


def test_inactive_products_are_excluded_by_default_and_includable_on_request():
    client = _client()

    default_rows = client.get("/products").json()
    assert all("P110" not in (r["description"] or "") for r in default_rows)

    all_rows = client.get("/products", params={"active_only": False}).json()
    assert any("P110" in (r["description"] or "") for r in all_rows)
    assert len(all_rows) == len(default_rows) + 1
    app.dependency_overrides.clear()


def test_q_filters_by_description_case_insensitively():
    client = _client()
    rows = client.get("/products", params={"q": "hydril"}).json()
    assert len(rows) == 1
    assert rows[0]["description"] == "CSG 7 32.0 L80 Hydril 563 SMLS"
    app.dependency_overrides.clear()


def test_payload_carries_no_quantity():
    """A Product is a catalogue entity. A quantity here would be BU-blind, which
    is the leak `InventoryOnHand` exists to close -- see ProductOut's docstring."""
    client = _client()
    row = client.get("/products").json()[0]
    assert "on_hand_qty" not in row
    assert not any("qty" in key or "quantity" in key for key in row)
    app.dependency_overrides.clear()
