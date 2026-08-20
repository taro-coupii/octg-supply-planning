# OCTG Rebuild — Stage 1: Foundation (no auth) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the `octg-platform/` skeleton — FastAPI backend with the three master tables and read-only APIs, React frontend shell with design tokens and shared primitives — all gated by tests.

**Architecture:** Backend is a 3-layer FastAPI app (`app/models` / `app/engines` / `app/api`); this stage builds models + read APIs only (engines stay empty). Frontend is Vite+React+TS with plain CSS, a dark-navy sidebar rail, SPA routing with a 404 catch-all, and shared primitives (`ConfirmButton`, `Freshness`, URL state, sort, enums) that every later stage reuses. No authentication in this stage (recorded as compromise C-01R).

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, Alembic, pytest, SQLite (dev) / Postgres-capable; Node 20, Vite, React 18, TypeScript, react-router-dom, vitest.

**Spec:** `docs/specs/2026-08-19-octg-platform-rebuild-design.md`

## Global Constraints

- **Clean-room:** do NOT read any file under the `claude/octg-supply-planning-hpej8e` branch. The spec above and `octg-platform/REQUIREMENTS.md` (fetch from that branch is already summarized INTO the spec) are the only requirements sources.
- Work on branch `claude/using-superpowers-skill-0ix8nt` only.
- Frontend: plain CSS only, NO external UI component libraries. `react-router-dom` and `vitest` are allowed (routing/test tooling, not UI).
- No dark mode. Semantic colors are tokens: `--ok`, `--warn`, `--bad`, `--unmodelled`; red and amber are never merged.
- All quantity payloads in any API must carry a `unit` field — no quantities appear in this stage, but do not add any quantity field without `unit`.
- No authentication on any endpoint in this stage (C-01R in the compromise register).
- Backend commands run from `octg-platform/backend/` with `.venv/bin/python`; frontend commands from `octg-platform/frontend/`.
- Every commit message ends with exactly these two lines (after a blank line):
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01M6AN1sQ1C6EVGxqZQERE14`
- Stage gate (also end of every task): backend `pytest -q` green; frontend `npx tsc --noEmit` exit 0 and `npm run build` success (once the frontend exists).

---

### Task 1: Backend scaffold + smoke test

**Files:**
- Create: `octg-platform/backend/requirements.txt`
- Create: `octg-platform/backend/app/__init__.py`, `app/db.py`, `app/main.py`
- Create: `octg-platform/backend/app/models/__init__.py`, `app/engines/__init__.py`, `app/api/__init__.py`, `app/auth/__init__.py` (all empty packages except models re-exports later)
- Create: `octg-platform/backend/pytest.ini`
- Test: `octg-platform/backend/tests/__init__.py`, `tests/conftest.py`, `tests/test_app.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces: `app.db.Base` (DeclarativeBase), `app.db.get_db()` (FastAPI dependency yielding a Session), `app.main.app` (FastAPI instance), test fixtures `db` (Session on in-memory SQLite) and `client` (TestClient with `get_db` overridden)

- [ ] **Step 1: Create venv and install dependencies**

`octg-platform/backend/requirements.txt`:

```
fastapi>=0.115
uvicorn[standard]>=0.32
sqlalchemy>=2.0
psycopg2-binary>=2.9
pydantic>=2.9
pytest>=8.3
httpx>=0.27
alembic>=1.14
openpyxl>=3.1
python-multipart>=0.0.9
```

Run:
```bash
cd octg-platform/backend
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
```

- [ ] **Step 2: Write the failing smoke test**

`tests/test_app.py`:

```python
def test_app_serves_openapi(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["info"]["title"] == "OCTG Supply Readiness Platform"
```

`tests/conftest.py`:

```python
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app.main import app


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    session = TestSession()
    yield session
    session.close()


@pytest.fixture()
def client(db):
    def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()
```

`pytest.ini`:

```ini
[pytest]
testpaths = tests
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_app.py -v`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'app'` (or `app.db`)

- [ ] **Step 4: Write minimal implementation**

`app/db.py`:

```python
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./dev.db")


class Base(DeclarativeBase):
    pass


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

`app/main.py`:

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="OCTG Supply Readiness Platform")

# Dev convenience: the Vite dev server runs cross-origin. Production serves the
# SPA from the same origin (VITE_API_BASE=""), so this list stays localhost-only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
```

Create empty `app/__init__.py`, `app/models/__init__.py`, `app/engines/__init__.py`, `app/api/__init__.py`, `app/auth/__init__.py`, `tests/__init__.py`.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: 1 passed

- [ ] **Step 6: Commit**

```bash
git add octg-platform/backend
git commit -m "feat(octg): backend scaffold with FastAPI app and test fixtures"
```
(Trailer lines per Global Constraints — same for every commit below.)

---

### Task 2: Master data models + initial Alembic migration

**Files:**
- Create: `octg-platform/backend/app/models/business_unit.py`, `app/models/customer.py`, `app/models/product.py`
- Modify: `octg-platform/backend/app/models/__init__.py`
- Create: `octg-platform/backend/alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/` (via `alembic init`)
- Test: `octg-platform/backend/tests/test_models.py`

**Interfaces:**
- Consumes: `app.db.Base`, fixtures from Task 1
- Produces: `BusinessUnit(id: str, name: str, parent_id: str|None, parent, children)`, `Customer(id: str, name: str, business_unit_id: str|None)`, `Product(id: str, name: str, unit_of_measure: UnitOfMeasure, weight_kg: float|None)`, `UnitOfMeasure` enum with values `"Mtr" | "PC" | "MT"` — all importable from `app.models`

- [ ] **Step 1: Write the failing tests**

`tests/test_models.py`:

```python
import pytest
from sqlalchemy.exc import IntegrityError

from app.models import BusinessUnit, Customer, Product, UnitOfMeasure


def test_business_unit_hierarchy(db):
    parent = BusinessUnit(name="SC Global")
    child = BusinessUnit(name="SCEU Norway", parent=parent)
    db.add_all([parent, child])
    db.commit()
    assert child.parent_id == parent.id
    assert parent.children == [child]


def test_business_unit_name_unique(db):
    db.add(BusinessUnit(name="SCEU Norway"))
    db.commit()
    db.add(BusinessUnit(name="SCEU Norway"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_customer_may_lack_business_unit(db):
    # BU-less customers are representable; engines decide how to treat them (spec §4)
    db.add(Customer(name="Orphan Oil"))
    db.commit()
    assert db.query(Customer).one().business_unit_id is None


def test_product_requires_unit_of_measure(db):
    db.add(Product(name="9-5/8 casing"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_product_weight_nullable_c06(db):
    # C-06R: weight_kg nullable is a recorded compromise, not an accident
    db.add(Product(name="9-5/8 casing", unit_of_measure=UnitOfMeasure.MTR))
    db.commit()
    assert db.query(Product).one().weight_kg is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'BusinessUnit' from 'app.models'`

- [ ] **Step 3: Write the models**

`app/models/business_unit.py`:

```python
import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class BusinessUnit(Base):
    """Absolute inventory boundary (spec §3): stock never crosses a BU."""

    __tablename__ = "business_units"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("business_units.id"))

    parent: Mapped["BusinessUnit | None"] = relationship(
        back_populates="children", remote_side=[id]
    )
    children: Mapped[list["BusinessUnit"]] = relationship(back_populates="parent")
```

`app/models/customer.py`:

```python
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.business_unit import _uuid


class Customer(Base):
    """Default planning boundary (spec §4). Allocation policy column arrives in stage 4."""

    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    business_unit_id: Mapped[str | None] = mapped_column(ForeignKey("business_units.id"))
```

`app/models/product.py`:

```python
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
```

`app/models/__init__.py`:

```python
from app.models.business_unit import BusinessUnit
from app.models.customer import Customer
from app.models.product import Product, UnitOfMeasure

__all__ = ["BusinessUnit", "Customer", "Product", "UnitOfMeasure"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: 6 passed

- [ ] **Step 5: Initialize Alembic and autogenerate the initial migration**

```bash
cd octg-platform/backend
.venv/bin/alembic init alembic
```

Edit `alembic.ini`: set `sqlalchemy.url = sqlite:///./dev.db`.

Edit `alembic/env.py` — after the existing `config = context.config` line, add:

```python
import os

from app.db import Base
from app import models  # noqa: F401  (registers tables on Base.metadata)

if os.environ.get("DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])

target_metadata = Base.metadata
```

(Replace the default `target_metadata = None` line.)

```bash
.venv/bin/alembic revision --autogenerate -m "initial master tables"
.venv/bin/alembic upgrade head
```

Expected: a new file in `alembic/versions/` creating `business_units`, `customers`, `products`; `dev.db` created at `octg-platform/backend/dev.db`.

- [ ] **Step 6: Run full suite, then commit (including dev.db)**

Run: `.venv/bin/python -m pytest -q` — Expected: 6 passed

```bash
git add octg-platform/backend
git commit -m "feat(octg): master data models with initial Alembic migration"
```

---

### Task 3: GET /business-units hierarchy endpoint

**Files:**
- Create: `octg-platform/backend/app/schemas/__init__.py`
- Create: `octg-platform/backend/app/api/business_units.py`
- Modify: `octg-platform/backend/app/main.py`
- Test: `octg-platform/backend/tests/test_master_api.py`

**Interfaces:**
- Consumes: `BusinessUnit`, `get_db`, `client`/`db` fixtures
- Produces: `GET /business-units` → `[{id, name, children: [...]}]` (roots only, children nested recursively); `app.schemas.BusinessUnitNode`

- [ ] **Step 1: Write the failing test**

`tests/test_master_api.py`:

```python
from app.models import BusinessUnit


def test_business_units_returns_nested_hierarchy(client, db):
    root = BusinessUnit(name="SC Global")
    child = BusinessUnit(name="SCEU Norway", parent=root)
    db.add_all([root, child])
    db.commit()

    resp = client.get("/business-units")
    assert resp.status_code == 200
    data = resp.json()
    assert [n["name"] for n in data] == ["SC Global"]
    assert [c["name"] for c in data[0]["children"]] == ["SCEU Norway"]
    assert data[0]["children"][0]["children"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_master_api.py -v`
Expected: FAIL with 404 (route not registered)

- [ ] **Step 3: Implement**

`app/schemas/__init__.py`:

```python
from pydantic import BaseModel


class BusinessUnitNode(BaseModel):
    id: str
    name: str
    children: list["BusinessUnitNode"] = []


BusinessUnitNode.model_rebuild()
```

`app/api/business_units.py`:

```python
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import BusinessUnit
from app.schemas import BusinessUnitNode

router = APIRouter()


def _node(bu: BusinessUnit) -> BusinessUnitNode:
    return BusinessUnitNode(
        id=bu.id,
        name=bu.name,
        children=[_node(c) for c in sorted(bu.children, key=lambda c: c.name)],
    )


@router.get("/business-units", response_model=list[BusinessUnitNode])
def list_business_units(db: Session = Depends(get_db)):
    roots = (
        db.query(BusinessUnit)
        .filter(BusinessUnit.parent_id.is_(None))
        .order_by(BusinessUnit.name)
        .all()
    )
    return [_node(r) for r in roots]
```

`app/main.py` becomes:

```python
from fastapi import FastAPI

from app.api import business_units

app = FastAPI(title="OCTG Supply Readiness Platform")
app.include_router(business_units.router)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add octg-platform/backend
git commit -m "feat(octg): business units hierarchy endpoint"
```

---

### Task 4: GET /customers endpoint

**Files:**
- Modify: `octg-platform/backend/app/schemas/__init__.py`
- Create: `octg-platform/backend/app/api/customers.py`
- Modify: `octg-platform/backend/app/main.py`
- Test: `octg-platform/backend/tests/test_master_api.py`

**Interfaces:**
- Consumes: `Customer`, `get_db`
- Produces: `GET /customers` → `[{id, name, business_unit_id}]` ordered by name; `app.schemas.CustomerOut`

- [ ] **Step 1: Write the failing test** (append to `tests/test_master_api.py`)

```python
from app.models import BusinessUnit, Customer


def test_customers_list_includes_bu_id_and_null_bu(client, db):
    bu = BusinessUnit(name="SCEU Norway")
    db.add(bu)
    db.flush()
    db.add_all(
        [
            Customer(name="Equinor Norway", business_unit_id=bu.id),
            Customer(name="Orphan Oil"),
        ]
    )
    db.commit()

    resp = client.get("/customers")
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["name"] for r in rows] == ["Equinor Norway", "Orphan Oil"]
    assert rows[0]["business_unit_id"] == bu.id
    assert rows[1]["business_unit_id"] is None
```

(The first line merges with the existing import at the top of the file — keep one `from app.models import BusinessUnit, Customer` import.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_master_api.py -v`
Expected: new test FAILS with 404

- [ ] **Step 3: Implement**

In `app/schemas/__init__.py`, change the import line to `from pydantic import BaseModel, ConfigDict` and append:

```python
class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    business_unit_id: str | None
```

`app/api/customers.py`:

```python
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Customer
from app.schemas import CustomerOut

router = APIRouter()


@router.get("/customers", response_model=list[CustomerOut])
def list_customers(db: Session = Depends(get_db)):
    return db.query(Customer).order_by(Customer.name).all()
```

In `app/main.py`, import and include: `from app.api import business_units, customers` / `app.include_router(customers.router)`.

Add `model_config = ConfigDict(from_attributes=True)` to `CustomerOut` (and import `ConfigDict` from pydantic) so ORM rows serialize.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add octg-platform/backend
git commit -m "feat(octg): customers list endpoint"
```

---

### Task 5: GET /products + 404 contract

**Files:**
- Modify: `octg-platform/backend/app/schemas/__init__.py`
- Create: `octg-platform/backend/app/api/products.py`
- Modify: `octg-platform/backend/app/main.py`
- Test: `octg-platform/backend/tests/test_master_api.py`

**Interfaces:**
- Consumes: `Product`, `UnitOfMeasure`, `get_db`
- Produces: `GET /products` → `[{id, name, unit_of_measure, weight_kg}]`; `GET /products/{product_id}` → same shape or 404 `{"detail": "Product not found"}`; `app.schemas.ProductOut` (`unit_of_measure` serialized as its string value, e.g. `"Mtr"`)

- [ ] **Step 1: Write the failing tests** (append to `tests/test_master_api.py`)

```python
from app.models import Product, UnitOfMeasure


def test_products_list_serializes_unit_value(client, db):
    db.add(Product(name="9-5/8 casing", unit_of_measure=UnitOfMeasure.MTR, weight_kg=53.5))
    db.commit()

    resp = client.get("/products")
    assert resp.status_code == 200
    [row] = resp.json()
    assert row["unit_of_measure"] == "Mtr"
    assert row["weight_kg"] == 53.5


def test_product_detail_404_for_unknown_id(client):
    resp = client.get("/products/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Product not found"}


def test_product_detail_returns_row(client, db):
    p = Product(name="7in tubing", unit_of_measure=UnitOfMeasure.PC)
    db.add(p)
    db.commit()

    resp = client.get(f"/products/{p.id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "7in tubing"
    assert resp.json()["weight_kg"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_master_api.py -v`
Expected: 3 new tests FAIL with 404 / assertion errors

- [ ] **Step 3: Implement**

Append to `app/schemas/__init__.py`:

```python
class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    unit_of_measure: str
    weight_kg: float | None
```

`app/api/products.py`:

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Product
from app.schemas import ProductOut

router = APIRouter()


def _out(p: Product) -> ProductOut:
    return ProductOut(
        id=p.id, name=p.name, unit_of_measure=p.unit_of_measure.value, weight_kg=p.weight_kg
    )


@router.get("/products", response_model=list[ProductOut])
def list_products(db: Session = Depends(get_db)):
    return [_out(p) for p in db.query(Product).order_by(Product.name).all()]


@router.get("/products/{product_id}", response_model=ProductOut)
def get_product(product_id: str, db: Session = Depends(get_db)):
    p = db.get(Product, product_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return _out(p)
```

Register in `app/main.py`: `from app.api import business_units, customers, products` / `app.include_router(products.router)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add octg-platform/backend
git commit -m "feat(octg): products endpoints with 404 contract"
```

---

### Task 6: Frontend scaffold + API wrapper

**Files:**
- Create: `octg-platform/frontend/` via Vite (package.json, tsconfig, vite.config.ts, index.html, src/)
- Create: `octg-platform/frontend/src/lib/api.ts`
- Modify: `octg-platform/frontend/src/main.tsx`, delete Vite demo boilerplate (`App.css`, logo assets, counter demo)

**Interfaces:**
- Consumes: backend endpoints from Tasks 3-5
- Produces: `apiGet<T>(path: string): Promise<T>` in `src/lib/api.ts` using `import.meta.env.VITE_API_BASE ?? ""`; npm scripts `dev`, `build`, `typecheck`, `test`

- [ ] **Step 1: Scaffold**

```bash
cd octg-platform
npm create vite@latest frontend -- --template react-ts
cd frontend
npm install
npm install react-router-dom
npm install -D vitest
```

- [ ] **Step 2: Replace demo boilerplate**

Delete `src/App.css`, `src/assets/react.svg`, `public/vite.svg` references. `src/App.tsx` will be replaced in Task 7; for now:

```tsx
export default function App() {
  return <div>OCTG Supply Readiness Platform</div>;
}
```

`src/lib/api.ts`:

```ts
const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

export async function apiGet<T>(path: string): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, { headers: { Accept: "application/json" } });
  if (!resp.ok) {
    throw new Error(`GET ${path} failed: ${resp.status}`);
  }
  return (await resp.json()) as T;
}
```

`index.html`: set `<title>OCTG Supply Readiness Platform</title>`.

Add npm scripts to `package.json`:

```json
"typecheck": "tsc --noEmit",
"test": "vitest run"
```

- [ ] **Step 3: Verify gates**

Run: `npm run typecheck` — Expected: exit 0
Run: `npm run build` — Expected: build succeeds

- [ ] **Step 4: Commit**

```bash
git add octg-platform/frontend
git commit -m "feat(octg): frontend scaffold with typed API wrapper"
```

---

### Task 7: Design tokens, layout shell, routing + 404

**Files:**
- Modify: `octg-platform/frontend/src/index.css` (tokens + shell styles)
- Create: `octg-platform/frontend/src/components/Layout.tsx`, `src/components/Breadcrumbs.tsx`, `src/pages/NotFound.tsx`, `src/pages/Home.tsx`
- Modify: `octg-platform/frontend/src/main.tsx`, `src/App.tsx` (delete App.tsx, routing moves to main.tsx)
- Create: `octg-platform/frontend/src/lib/enums.ts`

**Interfaces:**
- Consumes: `apiGet` (Home shows master data counts as a wiring proof)
- Produces: `<Layout>` (dark-navy sidebar rail + content outlet) used by every later page; `<Breadcrumbs items={{label: string; to?: string}[]}>` (pages pass "…" as the label while a name loads — a raw UUID must never render, spec §4.7); CSS custom properties `--rail-bg --rail-text --rail-active --bg --surface --text --muted --accent --ok --warn --bad --unmodelled --radius --shadow`; `src/lib/enums.ts` exporting `DEMAND_STATUSES`, `DEMAND_PROFILES`, `UNITS`; route table with `*` → `NotFound`

- [ ] **Step 1: Design tokens**

Top of `src/index.css` (replace Vite defaults entirely):

```css
:root {
  /* rail = dark navy sidebar (spec §3, Ascend SCM concept) */
  --rail-bg: #1c2536;
  --rail-text: #c9d2e3;
  --rail-active: #33415c;
  /* pastel base */
  --bg: #f7f8fb;
  --surface: #ffffff;
  --text: #26303f;
  --muted: #6b7688;
  --accent: #6d7fc9;
  /* semantic colors — red and amber are NEVER merged (spec §3) */
  --ok: #4a7c59;
  --warn: #a8833b;
  --bad: #b04a4a;
  --unmodelled: #7a7a7d;
  --radius: 10px;
  --shadow: 0 1px 3px rgba(28, 37, 54, 0.08), 0 4px 14px rgba(28, 37, 54, 0.06);
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, "Segoe UI", Roboto, "Hiragino Sans", sans-serif;
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
}

.layout { display: flex; min-height: 100vh; }
.rail {
  width: 220px; flex: none; background: var(--rail-bg); color: var(--rail-text);
  padding: 16px 0;
}
.rail a {
  display: block; padding: 8px 20px; color: inherit; text-decoration: none;
  border-left: 3px solid transparent;
}
.rail a.active { background: var(--rail-active); border-left-color: var(--accent); }
.rail .rail-title { padding: 0 20px 16px; font-weight: 600; color: #fff; }
.content { flex: 1; padding: 24px 32px; min-width: 0; }
.breadcrumbs { color: var(--muted); font-size: 13px; margin-bottom: 16px; }
.card {
  background: var(--surface); border-radius: var(--radius); box-shadow: var(--shadow);
  padding: 16px 20px;
}
```

- [ ] **Step 2: Layout + pages**

`src/components/Layout.tsx`:

```tsx
import { NavLink, Outlet } from "react-router-dom";

const NAV = [{ to: "/", label: "Home" }];

export default function Layout() {
  return (
    <div className="layout">
      <nav className="rail">
        <div className="rail-title">OCTG Readiness</div>
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} className={({ isActive }) => (isActive ? "active" : "")}>
            {n.label}
          </NavLink>
        ))}
      </nav>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
```

`src/components/Breadcrumbs.tsx`:

```tsx
import { Fragment } from "react";
import { Link } from "react-router-dom";

type Crumb = { label: string; to?: string };

// Convention (spec §4.7): while an entity name is still loading, pages pass
// label "…" — a raw UUID must never appear in the breadcrumb trail.
export default function Breadcrumbs({ items }: { items: Crumb[] }) {
  return (
    <nav className="breadcrumbs">
      {items.map((c, i) => (
        <Fragment key={i}>
          {i > 0 && " / "}
          {c.to ? <Link to={c.to}>{c.label}</Link> : <span>{c.label}</span>}
        </Fragment>
      ))}
    </nav>
  );
}
```

`src/pages/NotFound.tsx`:

```tsx
import { Link } from "react-router-dom";

export default function NotFound() {
  return (
    <div className="card">
      <h1>Page not found</h1>
      <p>The page you requested does not exist.</p>
      <Link to="/">Back to Home</Link>
    </div>
  );
}
```

`src/pages/Home.tsx` (placeholder shell — real Home Dashboard is stage 8; this proves API wiring):

```tsx
import { useEffect, useState } from "react";
import { apiGet } from "../lib/api";

type Product = { id: string; name: string; unit_of_measure: string; weight_kg: number | null };

export default function Home() {
  const [products, setProducts] = useState<Product[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    apiGet<Product[]>("/products")
      .then(setProducts)
      .catch((e: Error) => setError(e.message));
  }, []);

  return (
    <div className="card">
      <h1>OCTG Supply Readiness Platform</h1>
      {error && <p style={{ color: "var(--bad)" }}>{error}</p>}
      {products === null && !error && <p>Loading…</p>}
      {products !== null && <p>{products.length} products in the catalog.</p>}
    </div>
  );
}
```

`src/main.tsx`:

```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import Home from "./pages/Home";
import NotFound from "./pages/NotFound";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<Home />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </React.StrictMode>
);
```

Delete `src/App.tsx`.

`src/lib/enums.ts`:

```ts
// Single source for status/profile/unit constants (spec §3, ruling: 5-file hardcoding is a known smell)
export const DEMAND_STATUSES = ["Planned", "Budgeted", "Confirmed"] as const;
export type DemandStatus = (typeof DEMAND_STATUSES)[number];

export const DEMAND_PROFILES = ["Primary", "Contingency"] as const;
export type DemandProfile = (typeof DEMAND_PROFILES)[number];

export const UNITS = ["Mtr", "PC", "MT"] as const;
export type Unit = (typeof UNITS)[number];
```

- [ ] **Step 3: Verify gates**

Run: `npm run typecheck` — Expected: exit 0
Run: `npm run build` — Expected: success

- [ ] **Step 4: Commit**

```bash
git add octg-platform/frontend
git commit -m "feat(octg): design tokens, sidebar layout, routing with 404"
```

---

### Task 8: lib/sort — 3-state sorting with nulls last (TDD)

**Files:**
- Create: `octg-platform/frontend/src/lib/sort.ts`
- Test: `octg-platform/frontend/src/lib/sort.test.ts`

**Interfaces:**
- Consumes: nothing
- Produces: `type SortDir = "asc" | "desc" | null`; `nextSortState(current: SortDir): SortDir` (null→asc→desc→null); `sortRows<T>(rows: T[], key: keyof T, dir: SortDir): T[]` (dir null = server order copy; nulls last in BOTH directions; numbers numeric, strings localeCompare)

- [ ] **Step 1: Write the failing tests**

`src/lib/sort.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { nextSortState, sortRows } from "./sort";

describe("nextSortState", () => {
  it("cycles null -> asc -> desc -> null (third click returns to server order)", () => {
    expect(nextSortState(null)).toBe("asc");
    expect(nextSortState("asc")).toBe("desc");
    expect(nextSortState("desc")).toBeNull();
  });
});

describe("sortRows", () => {
  const rows = [
    { name: "b", qty: 2 },
    { name: "a", qty: null },
    { name: "c", qty: 1 },
  ];

  it("returns server order untouched when dir is null", () => {
    expect(sortRows(rows, "qty", null).map((r) => r.name)).toEqual(["b", "a", "c"]);
  });

  it("sorts asc with nulls last", () => {
    expect(sortRows(rows, "qty", "asc").map((r) => r.name)).toEqual(["c", "b", "a"]);
  });

  it("sorts desc with nulls STILL last", () => {
    expect(sortRows(rows, "qty", "desc").map((r) => r.name)).toEqual(["b", "c", "a"]);
  });

  it("does not mutate the input array", () => {
    sortRows(rows, "qty", "asc");
    expect(rows.map((r) => r.name)).toEqual(["b", "a", "c"]);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npm run test`
Expected: FAIL — cannot resolve `./sort`

- [ ] **Step 3: Implement**

`src/lib/sort.ts`:

```ts
export type SortDir = "asc" | "desc" | null;

export function nextSortState(current: SortDir): SortDir {
  if (current === null) return "asc";
  if (current === "asc") return "desc";
  return null;
}

function compare(a: unknown, b: unknown): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b));
}

export function sortRows<T>(rows: T[], key: keyof T, dir: SortDir): T[] {
  if (dir === null) return rows.slice();
  const nonNull = rows.filter((r) => r[key] != null);
  const nulls = rows.filter((r) => r[key] == null);
  nonNull.sort((x, y) => compare(x[key], y[key]));
  if (dir === "desc") nonNull.reverse();
  return [...nonNull, ...nulls];
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npm run test` — Expected: 5 passed
Run: `npm run typecheck` — Expected: exit 0

- [ ] **Step 5: Commit**

```bash
git add octg-platform/frontend/src/lib/sort.ts octg-platform/frontend/src/lib/sort.test.ts
git commit -m "feat(octg): three-state table sort helper with nulls last"
```

---

### Task 9: lib/urlState — URL param helpers (TDD)

**Files:**
- Create: `octg-platform/frontend/src/lib/urlState.ts`
- Test: `octg-platform/frontend/src/lib/urlState.test.ts`

**Interfaces:**
- Consumes: nothing
- Produces: `writeParam(params: URLSearchParams, key: string, value: string | null): URLSearchParams` (immutable copy; null/"" deletes); `readListParam(params: URLSearchParams, key: string): string[]`; `writeListParam(params: URLSearchParams, key: string, values: string[]): URLSearchParams`

- [ ] **Step 1: Write the failing tests**

`src/lib/urlState.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { readListParam, writeListParam, writeParam } from "./urlState";

describe("writeParam", () => {
  it("sets a value without mutating the input", () => {
    const before = new URLSearchParams("a=1");
    const after = writeParam(before, "b", "2");
    expect(after.get("b")).toBe("2");
    expect(before.get("b")).toBeNull();
  });

  it("deletes the key when value is null or empty", () => {
    const params = new URLSearchParams("a=1");
    expect(writeParam(params, "a", null).has("a")).toBe(false);
    expect(writeParam(params, "a", "").has("a")).toBe(false);
  });
});

describe("list params", () => {
  it("round-trips multiple values", () => {
    const params = writeListParam(new URLSearchParams(), "status", ["Planned", "Confirmed"]);
    expect(readListParam(params, "status")).toEqual(["Planned", "Confirmed"]);
  });

  it("writing an empty list clears the key", () => {
    const params = writeListParam(new URLSearchParams("status=Planned"), "status", []);
    expect(params.has("status")).toBe(false);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npm run test` — Expected: FAIL — cannot resolve `./urlState`

- [ ] **Step 3: Implement**

`src/lib/urlState.ts`:

```ts
export function writeParam(
  params: URLSearchParams,
  key: string,
  value: string | null
): URLSearchParams {
  const next = new URLSearchParams(params);
  if (value === null || value === "") next.delete(key);
  else next.set(key, value);
  return next;
}

export function readListParam(params: URLSearchParams, key: string): string[] {
  return params.getAll(key);
}

export function writeListParam(
  params: URLSearchParams,
  key: string,
  values: string[]
): URLSearchParams {
  const next = new URLSearchParams(params);
  next.delete(key);
  for (const v of values) next.append(key, v);
  return next;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npm run test` — Expected: 9 passed (5 sort + 4 urlState)

- [ ] **Step 5: Commit**

```bash
git add octg-platform/frontend/src/lib/urlState.ts octg-platform/frontend/src/lib/urlState.test.ts
git commit -m "feat(octg): URL state helpers for filter persistence"
```

---

### Task 10: ConfirmButton (2-step confirm) + Freshness

**Files:**
- Create: `octg-platform/frontend/src/lib/confirm.ts`
- Create: `octg-platform/frontend/src/components/ConfirmButton.tsx`, `src/components/Freshness.tsx`
- Modify: `octg-platform/frontend/src/index.css` (component styles)
- Test: `octg-platform/frontend/src/lib/confirm.test.ts`

**Interfaces:**
- Consumes: nothing
- Produces: `press(state: ConfirmState): {state: ConfirmState; fire: boolean}` and `ARM_TIMEOUT_MS = 4000` in `lib/confirm.ts`; `<ConfirmButton label armedLabel onConfirm className?>` (first click arms, second fires `onConfirm`; auto-disarms after 4s or on blur); `<Freshness fetchedAt={Date|null} onRefresh={() => void}>` showing `Fetched HH:MM` + Refresh button

- [ ] **Step 1: Write the failing tests**

`src/lib/confirm.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { ARM_TIMEOUT_MS, press } from "./confirm";

describe("two-step confirm state machine", () => {
  it("first press arms without firing", () => {
    expect(press("idle")).toEqual({ state: "armed", fire: false });
  });

  it("second press fires and returns to idle", () => {
    expect(press("armed")).toEqual({ state: "idle", fire: true });
  });

  it("arm timeout is exactly 4 seconds (spec §3 cross-cutting rule)", () => {
    expect(ARM_TIMEOUT_MS).toBe(4000);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npm run test` — Expected: FAIL — cannot resolve `./confirm`

- [ ] **Step 3: Implement**

`src/lib/confirm.ts`:

```ts
export type ConfirmState = "idle" | "armed";

export const ARM_TIMEOUT_MS = 4000;

export function press(state: ConfirmState): { state: ConfirmState; fire: boolean } {
  if (state === "idle") return { state: "armed", fire: false };
  return { state: "idle", fire: true };
}
```

`src/components/ConfirmButton.tsx`:

```tsx
import { useEffect, useRef, useState } from "react";
import { ARM_TIMEOUT_MS, ConfirmState, press } from "../lib/confirm";

type Props = {
  label: string;
  armedLabel: string;
  onConfirm: () => void;
  className?: string;
};

export default function ConfirmButton({ label, armedLabel, onConfirm, className }: Props) {
  const [state, setState] = useState<ConfirmState>("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (state === "armed") {
      timer.current = setTimeout(() => setState("idle"), ARM_TIMEOUT_MS);
    }
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [state]);

  const handleClick = () => {
    const next = press(state);
    setState(next.state);
    if (next.fire) onConfirm();
  };

  return (
    <button
      type="button"
      className={`confirm-btn ${state === "armed" ? "armed" : ""} ${className ?? ""}`}
      onClick={handleClick}
      onBlur={() => setState("idle")}
    >
      {state === "armed" ? armedLabel : label}
    </button>
  );
}
```

`src/components/Freshness.tsx`:

```tsx
type Props = { fetchedAt: Date | null; onRefresh: () => void };

export default function Freshness({ fetchedAt, onRefresh }: Props) {
  const time = fetchedAt
    ? fetchedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : "—";
  return (
    <span className="freshness">
      Fetched {time}{" "}
      <button type="button" onClick={onRefresh}>
        Refresh
      </button>
    </span>
  );
}
```

Append to `src/index.css`:

```css
.confirm-btn {
  border: 1px solid var(--accent); background: var(--surface); color: var(--accent);
  border-radius: var(--radius); padding: 6px 14px; cursor: pointer;
}
.confirm-btn.armed { background: var(--warn); border-color: var(--warn); color: #fff; }
.freshness { color: var(--muted); font-size: 13px; }
.freshness button {
  border: none; background: none; color: var(--accent); cursor: pointer; padding: 0;
}
```

- [ ] **Step 4: Run tests + gates**

Run: `npm run test` — Expected: 12 passed
Run: `npm run typecheck` — Expected: exit 0
Run: `npm run build` — Expected: success

- [ ] **Step 5: Commit**

```bash
git add octg-platform/frontend/src
git commit -m "feat(octg): ConfirmButton two-step primitive and Freshness indicator"
```

---

### Task 11: Compromise register + stage gate

**Files:**
- Create: `docs/COMPROMISES.md`

**Interfaces:**
- Consumes: everything above
- Produces: the rebuild's compromise register, seeded with C-01R and C-06R; a fully green stage

- [ ] **Step 1: Write the register**

`docs/COMPROMISES.md`:

```markdown
# Rebuild Compromise Register

Serves the same role as the original implementation's MVP_COMPROMISES.md. Whenever a principle is bent, place a marker in the code and add a line here.
Search: `grep -rn "COMPROMISE\[" octg-platform/`

| ID | Description | Stage introduced | Planned resolution |
|---|---|---|---|
| C-01R | All APIs exposed with no authentication (introduced per-provider structure in Stage 7) | 1 | Stage 7 |
| C-06R | `Product.weight_kg` nullable (MT conversion not guaranteed for every product) | 1 | Alongside monetary valuation (out of scope) |
```

- [ ] **Step 2: Run the full stage gate**

```bash
cd octg-platform/backend && .venv/bin/python -m pytest -q
cd ../frontend && npm run test && npm run typecheck && npm run build
```

Expected: backend 11 passed; frontend 12 passed; tsc exit 0; build success.

- [ ] **Step 3: Manual smoke (both servers)**

```bash
cd octg-platform/backend && .venv/bin/uvicorn app.main:app --port 8000 &
cd octg-platform/frontend && VITE_API_BASE=http://localhost:8000 npm run dev &
```

Verify: `curl -s http://localhost:8000/products` returns `[]`; frontend dev server renders Home with "0 products in the catalog."; an unknown path like `/nope` renders the 404 page. Kill both servers afterwards.

- [ ] **Step 4: Commit and push**

```bash
git add docs/COMPROMISES.md
git commit -m "docs(octg): compromise register seeded with C-01R and C-06R"
git push -u origin claude/using-superpowers-skill-0ix8nt
```
