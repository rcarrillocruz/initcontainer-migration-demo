#!/usr/bin/env python3
"""
Database migration runner using pg_advisory_lock to serialize concurrent
Alembic migrations across multiple replicas.

When multiple pods start simultaneously (e.g. during a rolling update), each
pod's initContainer runs this script. pg_advisory_lock ensures only one
migration runs at a time. Subsequent replicas that acquire the lock after the
first see the schema already at head and Alembic no-ops silently.

If migration fails the lock is released in the finally block, then the script
exits non-zero. The initContainer exits non-zero and Kubernetes keeps the pod
in Init:Error — the main app container never starts.
"""
import logging
import os
import sys
from pathlib import Path

import psycopg2
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("migrate")
# Set level explicitly so Alembic's fileConfig(disable_existing_loggers=False)
# doesn't silence us by resetting the root logger to WARN.
log.setLevel(logging.INFO)

# Stable advisory lock ID for this application.
# Pick any consistent int64 and never change it — all replicas must agree.
MIGRATION_ADVISORY_LOCK_ID = 7243911227

# alembic.ini lives at the project root, one level above this file.
_DEFAULT_ALEMBIC_INI = str(Path(__file__).parent.parent / "alembic.ini")


def get_pg_connection(database_url: str) -> psycopg2.extensions.connection:
    """Open a psycopg2 connection for advisory lock management.

    psycopg2 supports postgresql:// URIs directly.  Strip any SQLAlchemy
    driver prefix (e.g. +psycopg2) that testcontainers or SQLAlchemy may add.
    """
    pg_url = database_url.replace("+psycopg2", "").replace("+asyncpg", "")
    conn = psycopg2.connect(pg_url)
    conn.autocommit = True
    return conn


def run_migrations(alembic_ini: str | None = None) -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        log.error("DATABASE_URL environment variable is not set")
        sys.exit(1)

    if alembic_ini is None:
        alembic_ini = os.environ.get("ALEMBIC_INI", _DEFAULT_ALEMBIC_INI)

    log.info("Connecting for advisory lock (lock_id=%d)...", MIGRATION_ADVISORY_LOCK_ID)
    conn = get_pg_connection(database_url)
    cursor = conn.cursor()

    # pg_advisory_lock blocks until the lock is acquired — no busy-wait needed.
    # All replicas serialise here; the first one runs migrations, the rest
    # acquire the lock afterwards and see a no-op upgrade.
    log.info("Waiting for pg_advisory_lock(%d)...", MIGRATION_ADVISORY_LOCK_ID)
    cursor.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_ADVISORY_LOCK_ID,))
    log.info("Lock acquired.")

    migration_error: Exception | None = None
    try:
        alembic_cfg = Config(alembic_ini)
        pg_url = database_url.replace("+psycopg2", "").replace("+asyncpg", "")
        alembic_cfg.set_main_option("sqlalchemy.url", pg_url)

        # Check the current revision before upgrading so we can log clearly
        # whether this replica applied DDL or just confirmed schema was current.
        engine = create_engine(pg_url)
        with engine.connect() as sa_conn:
            ctx = MigrationContext.configure(sa_conn)
            revision_before = ctx.get_current_revision()
        engine.dispose()

        log.info("Current revision: %s — running 'alembic upgrade head'...", revision_before or "None (fresh db)")
        command.upgrade(alembic_cfg, "head")

        engine = create_engine(pg_url)
        with engine.connect() as sa_conn:
            ctx = MigrationContext.configure(sa_conn)
            revision_after = ctx.get_current_revision()
        engine.dispose()

        if revision_before == revision_after:
            log.info("Schema already at head (%s) — no migrations applied (no-op).", revision_after)
        else:
            log.info("Migrations applied: %s -> %s.", revision_before or "None", revision_after)
    except Exception as exc:
        log.exception("Migration failed: %s", exc)
        migration_error = exc
    finally:
        try:
            cursor.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_ADVISORY_LOCK_ID,))
            log.info("Advisory lock released.")
        except Exception:
            # Connection may already be broken; Postgres releases session-level
            # advisory locks automatically when the connection closes.
            log.warning("Could not explicitly unlock; lock will be released on disconnect.")
        cursor.close()
        conn.close()

    if migration_error is not None:
        sys.exit(1)


if __name__ == "__main__":
    run_migrations()
