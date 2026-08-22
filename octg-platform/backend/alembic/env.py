"""Alembic environment for the OCTG Supply Readiness Platform.

Two things here are load-bearing and should not be "simplified":

1. The database URL comes from the ``DATABASE_URL`` environment variable, with
   the SAME default as ``app.db``. It is deliberately NOT stored in
   alembic.ini -- one source of truth, so `alembic upgrade head` and the running
   app can never point at different databases.

2. ``render_as_batch=True``. sqlite is what dev and the test corpus use, and
   sqlite cannot ALTER most things natively. Without batch mode any future
   column drop / type change / constraint change migration will simply fail on
   sqlite. It is harmless on postgres.
"""

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make the backend package importable when alembic is invoked from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import DATABASE_URL, Base  # noqa: E402
import app.models  # noqa: F401,E402  -- registers EVERY model on Base.metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Same URL resolution as app/db.py. Env var wins; alembic.ini holds no URL.
# '%' is escaped because ConfigParser would otherwise treat it as interpolation.
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url")
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=connect_args,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
