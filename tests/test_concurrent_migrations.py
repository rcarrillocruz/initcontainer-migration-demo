"""
Integration tests simulating multiple replicas starting their initContainers
simultaneously — the primary concern raised against the initContainer pattern.

Each test spawns N threads that all call run_migrations() at the same time,
mirroring what happens during a Kubernetes rolling update when several pods
start concurrently.

Expected behaviour:
- pg_advisory_lock serialises the runners: exactly one runs, the rest wait
- Waiting runners acquire the lock in turn, see the schema at head, Alembic
  no-ops, and exit cleanly
- No deadlocks, no errors — all threads must finish within the timeout
- The final schema must be fully applied regardless of which thread "won"
"""
import threading
from dataclasses import dataclass, field
from unittest.mock import patch

import psycopg2
import pytest

from app.migrate import run_migrations


@dataclass
class MigrationResult:
    success: bool = False
    exception: Exception | None = None


def _run_in_thread(result: MigrationResult) -> None:
    try:
        run_migrations()
        result.success = True
    except SystemExit as exc:
        result.exception = exc


def run_concurrent(n: int, timeout: float = 60.0) -> list[MigrationResult]:
    """Spawn n threads calling run_migrations() simultaneously."""
    results = [MigrationResult() for _ in range(n)]
    threads = [
        threading.Thread(target=_run_in_thread, args=(r,), daemon=True)
        for r in results
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)
        assert not t.is_alive(), "Thread did not finish — possible deadlock"
    return results


# ---------------------------------------------------------------------------
# Happy-path concurrency tests
# ---------------------------------------------------------------------------


def test_two_concurrent_replicas(database_url):
    """Two replicas starting at the same time must both succeed."""
    results = run_concurrent(2)
    for i, r in enumerate(results):
        assert r.success, f"Replica {i} failed: {r.exception}"


def test_three_concurrent_replicas(database_url):
    """Three replicas — a typical small rolling-update scenario."""
    results = run_concurrent(3)
    for i, r in enumerate(results):
        assert r.success, f"Replica {i} failed: {r.exception}"


def test_schema_correct_after_concurrent_migrations(database_url):
    """After concurrent startup, both migration versions must be applied."""
    results = run_concurrent(3)
    assert all(r.success for r in results)

    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name IN ('items', 'job_runs')
        ORDER BY table_name
        """
    )
    tables = [row[0] for row in cur.fetchall()]
    assert tables == ["items", "job_runs"], f"Unexpected tables: {tables}"

    cur.execute("SELECT version_num FROM alembic_version")
    version = cur.fetchone()[0]
    assert version == "002", f"Expected version 002, got {version}"

    cur.close()
    conn.close()


# ---------------------------------------------------------------------------
# Failure + recovery tests
# ---------------------------------------------------------------------------


def test_no_deadlock_after_failed_first_runner(database_url):
    """
    Scenario: the first replica's migration raises mid-run.

    The lock must be released in the finally block so the second replica can
    acquire it and complete the migration successfully.  This is the critical
    safety property: a crashed initContainer must never starve all other pods.
    """
    from alembic import command

    call_count = {"n": 0}
    original_upgrade = command.upgrade

    def flaky_upgrade(cfg, rev):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("Simulated transient migration failure")
        original_upgrade(cfg, rev)

    with patch.object(command, "upgrade", side_effect=flaky_upgrade):
        with pytest.raises(SystemExit):
            run_migrations()  # first call — fails

    # Lock must be free now.  Second call should succeed.
    run_migrations()

    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT version_num FROM alembic_version")
    version = cur.fetchone()[0]
    assert version == "002"
    cur.close()
    conn.close()


def test_concurrent_with_one_failing(database_url):
    """
    One of three concurrent replicas encounters a migration error.

    The other two must still complete without deadlock.  The failing replica's
    initContainer exits non-zero (pod stays in Init:Error), but the two healthy
    replicas should finish and their main containers can start.
    """
    from alembic import command

    call_count = {"n": 0}
    lock = threading.Lock()
    original_upgrade = command.upgrade

    def one_bad_upgrade(cfg, rev):
        with lock:
            call_count["n"] += 1
            is_first = call_count["n"] == 1
        if is_first:
            raise RuntimeError("Simulated single-replica failure")
        original_upgrade(cfg, rev)

    def run_with_patch(result: MigrationResult) -> None:
        try:
            with patch.object(command, "upgrade", side_effect=one_bad_upgrade):
                run_migrations()
            result.success = True
        except SystemExit as exc:
            result.exception = exc

    results = [MigrationResult() for _ in range(3)]
    threads = [
        threading.Thread(target=run_with_patch, args=(r,), daemon=True)
        for r in results
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive(), "Possible deadlock"

    failures = [r for r in results if not r.success]
    successes = [r for r in results if r.success]

    # Exactly one failure (the patched one), two successes
    assert len(failures) == 1, f"Expected 1 failure, got {len(failures)}"
    assert len(successes) == 2, f"Expected 2 successes, got {len(successes)}"
