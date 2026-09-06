import os
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg2://octg:octg@localhost:5432/octg")


def enforce_sqlite_foreign_keys(engine: Engine) -> Engine:
    """Turn foreign-key enforcement ON for every SQLite connection this engine opens.

    SQLite ships with `PRAGMA foreign_keys` OFF and the setting is PER CONNECTION,
    so declaring `ForeignKey(...)` on every model bought exactly nothing at the
    database: a PlanningNode pointing at a customer that does not exist committed
    without complaint (adversarial review 2026-09-06, F10). PostgreSQL enforces
    constraints unconditionally, so this listener is a no-op there and the two
    databases now refuse the same rows.

    Every engine -- the application's, and every one a test builds -- goes through
    this function. A test that constructs its own engine and forgets it would be
    testing against a database that accepts orphans the real one refuses, which is
    the exact blind spot this closes; conftest.py and the fixture modules all call it.
    """
    if engine.dialect.name != "sqlite":
        return engine

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = enforce_sqlite_foreign_keys(create_engine(DATABASE_URL, connect_args=connect_args))
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
