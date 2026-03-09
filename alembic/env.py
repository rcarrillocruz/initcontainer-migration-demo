import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False preserves loggers created before Alembic
    # runs (e.g. the 'migrate' logger in app/migrate.py).  Without this,
    # fileConfig() disables them and post-upgrade log lines are silenced.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Allow DATABASE_URL env var to override alembic.ini so the same env.py
# works both in containers and in local test runs.
database_url = os.environ.get("DATABASE_URL")
if database_url:
    pg_url = database_url.replace("+psycopg2", "").replace("+asyncpg", "")
    config.set_main_option("sqlalchemy.url", pg_url)

from app.models import Base  # noqa: E402

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
