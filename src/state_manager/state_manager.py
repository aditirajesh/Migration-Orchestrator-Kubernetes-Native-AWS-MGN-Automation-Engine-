import json

import asyncpg

from state_manager.states import ServerState
from state_manager.transitions import TransitionValidator, InvalidTransitionError


class StateManager:
    """
    The single point of contact for all database state operations.

    Every state read and write in the system goes through here.
    Uses asyncpg for async PostgreSQL access and SELECT FOR UPDATE
    for row-level locking to prevent concurrent state corruption.
    """

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        self._validator = TransitionValidator()

    @classmethod
    async def create(cls, database_url: str) -> "StateManager":
        """
        Factory method. Creates the connection pool and returns a ready StateManager.
        Call this once at application startup, share the instance across all workers.
        """
        pool = await asyncpg.create_pool(dsn=database_url, min_size=2, max_size=10)
        return cls(pool)

    async def close(self) -> None:
        """Closes the connection pool. Call at application shutdown."""
        await self._pool.close()

    # ------------------------------------------------------------------ #
    #  Server Registration                                                 #
    # ------------------------------------------------------------------ #

    async def register_server(
        self,
        server_id: str,
        hostname: str,
        ip_address: str,
        assigned_engineer: str | None = None,
    ) -> None:
        """
        Registers a new source server in the system.
        Initial state is always PENDING — the first step in the pipeline.
        Raises asyncpg.UniqueViolationError if server_id already exists.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO servers (server_id, hostname, ip_address, current_state, assigned_engineer)
                VALUES ($1, $2, $3, $4, $5)
                """,
                server_id,
                hostname,
                ip_address,
                ServerState.PENDING.value,
                assigned_engineer,
            )

    # ------------------------------------------------------------------ #
    #  State Reads                                                         #
    # ------------------------------------------------------------------ #

    async def get_server_state(self, server_id: str) -> str:
        """
        Returns the current_state string for a server.
        Raises ValueError if the server does not exist.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT current_state FROM servers WHERE server_id = $1",
                server_id,
            )
        if row is None:
            raise ValueError(f"Server '{server_id}' not found")
        return row["current_state"]

    async def get_server(self, server_id: str) -> dict:
        """
        Returns all columns for a server as a plain dict.
        Raises ValueError if the server does not exist.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM servers WHERE server_id = $1",
                server_id,
            )
        if row is None:
            raise ValueError(f"Server '{server_id}' not found")
        return dict(row)

    # ------------------------------------------------------------------ #
    #  State Transitions                                                   #
    # ------------------------------------------------------------------ #

    async def advance_state(
        self,
        server_id: str,
        to_state: str,
        triggered_by: str,
        job_id: str | None = None,
        job_type: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """
        Moves server_id from its current state to to_state.

        This is the only method that writes state changes. All four steps
        happen inside a single transaction — if any step fails, nothing
        is written to either table.

          1. Lock the server row        (SELECT FOR UPDATE)
          2. Validate the transition    (TransitionValidator)
          3. Update the servers table   (current_state, previous_state, updated_at)
          4. Append to history table    (immutable audit record)

        Parameters:
          triggered_by — 'system' for automated jobs, engineer ID for manual/approval actions
          job_id       — the RabbitMQ message ID that caused this transition (None for manual)
          job_type     — the JobType enum value as a string (None for manual)
          metadata     — any structured context, e.g. {"instance_id": "i-0abc123", "lag_seconds": 0}

        Raises:
          ValueError             — server_id not found
          InvalidTransitionError — transition is not permitted by the state machine
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():

                # Step 1: Lock the row for the duration of this transaction.
                # Any other worker trying to advance the same server will block
                # here until we commit, guaranteeing serialised state changes.
                row = await conn.fetchrow(
                    "SELECT current_state FROM servers WHERE server_id = $1 FOR UPDATE",
                    server_id,
                )
                if row is None:
                    raise ValueError(f"Server '{server_id}' not found")

                from_state = row["current_state"]

                # Step 2: Validate the transition against the state machine rules.
                # This runs inside the lock so the from_state we validate against
                # is guaranteed to be current — no other worker can have changed
                # it between our SELECT and this check.
                self._validator.validate(server_id, from_state, to_state)

                # Step 3: Update the live snapshot in the servers table.
                await conn.execute(
                    """
                    UPDATE servers
                    SET current_state = $1,
                        previous_state = $2,
                        updated_at     = now()
                    WHERE server_id = $3
                    """,
                    to_state,
                    from_state,
                    server_id,
                )

                # Step 4: Append an immutable record to the audit trail.
                # json.dumps converts the Python dict to a JSON string that
                # PostgreSQL accepts for the JSONB column.
                await conn.execute(
                    """
                    INSERT INTO state_transition_history
                        (server_id, from_state, to_state, job_id, job_type, triggered_by, metadata)
                    VALUES
                        ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    server_id,
                    from_state,
                    to_state,
                    job_id,
                    job_type,
                    triggered_by,
                    json.dumps(metadata) if metadata is not None else None,
                )
