import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

# Alembic Config object — gives access to values in alembic.ini
config = context.config

# Set up Python logging from the alembic.ini logging section
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Read the database URL from environment variable.
# This means credentials never live in alembic.ini or any committed file.
# For local development, set DATABASE_URL in your shell or .env file.
# For Kubernetes, inject it via a Secret as an environment variable.
database_url = os.environ.get(
    "DATABASE_URL",
    "postgresql://admin:changeme@localhost:5432/migration_orchestrator"
)
config.set_main_option("sqlalchemy.url", database_url)

# target_metadata is used by autogenerate to compare models against the DB.
# We are writing migrations by hand so this stays None.
target_metadata = None


def run_migrations_offline() -> None:
    """
    Offline mode: generate SQL statements without connecting to the database.
    Useful for reviewing exactly what SQL will be executed before applying it,
    or for environments where you cannot connect to the database directly.
    Run with: alembic upgrade head --sql
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Online mode: connect directly to the database and apply migrations.
    This is the mode used for all normal operations:
    alembic upgrade head    — apply all pending migrations
    alembic downgrade -1    — roll back the last migration
    alembic current         — show which migration is currently applied
    alembic history         — show the full migration chain
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # NullPool: do not maintain a connection pool — each migration command
        # opens one connection, uses it, and closes it. Appropriate for a
        # short-lived CLI command rather than a long-running application.
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
