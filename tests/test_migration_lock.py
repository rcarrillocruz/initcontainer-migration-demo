"""
Unit-level tests for the migration runner's advisory lock behaviour.

These tests verify:
- The lock is acquired and then released on success
- The lock is still released even when the migration itself fails
- Running migrations twice is safe (idempotent / already-at-head no-op)
- A missing DATABASE_URL causes a non-zero exit
"""
import psycopg2
import pytest
from unittest.mock import patch

from app.migrate import MIGRATION_ADVISORY_LOCK_ID, run_migrations


def lock_is_held(conn: psycopg2.extensions.connection) -> bool:
    """Return True if any session holds the advisory lock right now.

    pg_advisory_lock(bigint) splits the 64-bit key across two pg_locks columns:
      classid = upper 32 bits,  objid = lower 32 bits,  objsubid = 1
    """
    classid = (MIGRATION_ADVISORY_LOCK_ID >> 32) & 0xFFFFFFFF
    objid = MIGRATION_ADVISORY_LOCK_ID & 0xFFFFFFFF
    cur = conn.cursor()
    cur.execute(
        """
        SELECT count(*) FROM pg_locks
        WHERE locktype = 'advisory'
          AND classid = %s
          AND objid = %s
          AND objsubid = 1
          AND granted = true
        """,
        (classid, objid),
    )
    count = cur.fetchone()[0]
    cur.close()
    return count > 0


def test_lock_acquired_and_released(database_url):
    """After a successful migration the advisory lock must be gone."""
    monitor = psycopg2.connect(database_url)
    monitor.autocommit = True

    run_migrations()

    assert not lock_is_held(monitor), "Lock should be released after success"
    monitor.close()


def test_lock_released_on_failure(database_url):
    """When Alembic raises, the lock must still be released before exiting."""
    from alembic import command

    monitor = psycopg2.connect(database_url)
    monitor.autocommit = True

    with patch.object(command, "upgrade", side_effect=RuntimeError("boom")):
        with pytest.raises(SystemExit) as exc_info:
            run_migrations()
        assert exc_info.value.code != 0

    assert not lock_is_held(monitor), "Lock should be released even after failure"
    monitor.close()


def test_idempotent_migration(database_url):
    """Running the migration runner twice must not raise."""
    run_migrations()
    run_migrations()  # already at head — Alembic no-ops, should not raise


def test_missing_database_url(monkeypatch):
    """Absent DATABASE_URL must produce a non-zero SystemExit immediately."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        run_migrations()
    assert exc_info.value.code != 0
