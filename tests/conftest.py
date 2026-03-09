"""
Pytest fixtures for migration tests.

A single Postgres 15 container is started once per test session via
testcontainers.  Each test gets a fresh DATABASE_URL in its environment and
a clean schema (all tables dropped) so tests are fully isolated.
"""
import psycopg2
import pytest
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def postgres_container():
    with PostgresContainer("postgres:15-alpine") as pg:
        yield pg


@pytest.fixture(autouse=True)
def database_url(postgres_container, monkeypatch):
    """Set DATABASE_URL and wipe the schema before every test."""
    # testcontainers returns a SQLAlchemy URL; strip the driver prefix so
    # psycopg2 and our migration runner can both parse it as a plain URI.
    url = postgres_container.get_connection_url().replace("+psycopg2", "")
    monkeypatch.setenv("DATABASE_URL", url)

    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "DROP TABLE IF EXISTS items, job_runs, alembic_version CASCADE"
    )
    cur.close()
    conn.close()

    yield url
