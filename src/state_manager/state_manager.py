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
        aws_account_id: str | None = None,
        aws_region: str | None = None,
        assigned_engineer: str | None = None,
        batch_id: str | None = None,
    ) -> None:
        """
        Registers a new source server in the system.
        Initial state is always PENDING — the first step in the pipeline.
        Raises asyncpg.UniqueViolationError if server_id already exists.

        aws_account_id and aws_region identify the target AWS environment.
        Credentials themselves stay in the k8s secret — these are metadata only.
        batch_id optionally assigns the server to a wave at registration time.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO servers
                    (server_id, hostname, ip_address, current_state,
                     aws_account_id, aws_region, assigned_engineer, batch_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                server_id,
                hostname,
                ip_address,
                ServerState.PENDING.value,
                aws_account_id,
                aws_region,
                assigned_engineer,
                batch_id,
            )

    # ------------------------------------------------------------------ #
    #  Bulk Reads                                                          #
    # ------------------------------------------------------------------ #

    async def list_servers(
        self,
        state_filter: str | None = None,
        batch_id: str | None = None,
        assigned_engineer: str | None = None,
        hostname: str | None = None,
    ) -> list[dict]:
        """
        Returns all server rows, with composable optional filters.
        Ordered by created_at descending so the newest registrations appear first.

        batch_id='unassigned' is a sentinel that queries WHERE batch_id IS NULL.
        """
        conditions: list[str] = []
        params: list = []

        if state_filter:
            params.append(state_filter)
            conditions.append(f"current_state = ${len(params)}")

        if batch_id is not None:
            if batch_id == "unassigned":
                conditions.append("batch_id IS NULL")
            else:
                params.append(batch_id)
                conditions.append(f"batch_id = ${len(params)}")

        if assigned_engineer:
            params.append(assigned_engineer)
            conditions.append(f"assigned_engineer = ${len(params)}")

        if hostname:
            params.append(f"%{hostname}%")
            conditions.append(f"hostname ILIKE ${len(params)}")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM servers {where} ORDER BY created_at DESC"

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]

    async def get_server_history(self, server_id: str) -> list[dict]:
        """
        Returns all state transitions for a server in ascending timestamp order.
        Raises ValueError if the server does not exist.
        """
        async with self._pool.acquire() as conn:
            exists = await conn.fetchrow(
                "SELECT server_id FROM servers WHERE server_id = $1", server_id
            )
            if exists is None:
                raise ValueError(f"Server '{server_id}' not found")
            rows = await conn.fetch(
                """
                SELECT * FROM state_transition_history
                WHERE server_id = $1
                ORDER BY timestamp ASC
                """,
                server_id,
            )
        return [dict(r) for r in rows]

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

    # ------------------------------------------------------------------ #
    #  Server Attribute Updates                                            #
    # ------------------------------------------------------------------ #

    async def set_aws_source_server_id(
        self, server_id: str, aws_source_server_id: str
    ) -> None:
        """
        Stores the AWS MGN source server ID against the server record.
        Called by the ADD_SERVER handler after confirming the server exists in MGN.
        Raises ValueError if the server does not exist.
        """
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE servers
                SET aws_source_server_id = $1,
                    updated_at           = now()
                WHERE server_id = $2
                """,
                aws_source_server_id,
                server_id,
            )
        if result == "UPDATE 0":
            raise ValueError(f"Server '{server_id}' not found")

    # ------------------------------------------------------------------ #
    #  Manual Overrides (API-only paths — bypass TransitionValidator)      #
    # ------------------------------------------------------------------ #

    async def reset_server(
        self,
        server_id: str,
        engineer_id: str,
        reason: str | None = None,
    ) -> None:
        """
        Resets a FAILED server back to PENDING so the migration can be retried.

        Intentionally bypasses TransitionValidator — FAILED has no automated
        outgoing transitions by design, preventing workers from accidentally
        restarting a failed migration. Only an engineer calling the API can
        trigger this path.

        FROZEN servers cannot be reset here. FROZEN means the AWS state is
        unknown and requires manual inspection before the system touches it.

        Raises ValueError if the server is not found or is not in FAILED state.
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT current_state FROM servers WHERE server_id = $1 FOR UPDATE",
                    server_id,
                )
                if row is None:
                    raise ValueError(f"Server '{server_id}' not found")

                current_state = row["current_state"]
                if current_state != "FAILED":
                    raise ValueError(
                        f"Server '{server_id}' is in state '{current_state}'. "
                        f"Only FAILED servers can be reset. "
                        f"FROZEN servers require manual AWS inspection before reset."
                    )

                await conn.execute(
                    """
                    UPDATE servers
                    SET current_state  = 'PENDING',
                        previous_state = 'FAILED',
                        updated_at     = now()
                    WHERE server_id = $1
                    """,
                    server_id,
                )

                await conn.execute(
                    """
                    INSERT INTO state_transition_history
                        (server_id, from_state, to_state, job_id, job_type, triggered_by, metadata)
                    VALUES ($1, 'FAILED', 'PENDING', NULL, 'reset', $2, $3)
                    """,
                    server_id,
                    engineer_id,
                    json.dumps({"reason": reason}) if reason else None,
                )

    # ------------------------------------------------------------------ #
    #  Batch Management                                                    #
    # ------------------------------------------------------------------ #

    async def create_batch(
        self,
        batch_id: str,
        name: str,
        created_by: str,
        description: str | None = None,
    ) -> None:
        """
        Creates a new batch record.
        Raises asyncpg.UniqueViolationError if batch_id already exists.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO batches (batch_id, name, description, created_by)
                VALUES ($1, $2, $3, $4)
                """,
                batch_id,
                name,
                description,
                created_by,
            )

    async def get_batch(self, batch_id: str) -> dict:
        """
        Returns the batch record with an additional server_count field.
        Raises ValueError if batch_id does not exist.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT b.*,
                       COUNT(s.server_id) AS server_count
                FROM batches b
                LEFT JOIN servers s ON s.batch_id = b.batch_id
                WHERE b.batch_id = $1
                GROUP BY b.batch_id
                """,
                batch_id,
            )
        if row is None:
            raise ValueError(f"Batch '{batch_id}' not found")
        return dict(row)

    async def list_batches(self) -> list[dict]:
        """
        Returns all batches with server counts, newest first.
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT b.*,
                       COUNT(s.server_id) AS server_count
                FROM batches b
                LEFT JOIN servers s ON s.batch_id = b.batch_id
                GROUP BY b.batch_id
                ORDER BY b.created_at DESC
                """
            )
        return [dict(r) for r in rows]

    async def add_servers_to_batch(
        self, batch_id: str, server_ids: list[str]
    ) -> dict:
        """
        Assigns the given server_ids to the batch.
        Servers not found in the DB land in 'not_found'.
        Servers already in this batch land in 'already_assigned'.
        All others are updated to point to this batch ('assigned').

        Does not raise if the batch does not exist — callers must validate first.
        """
        if not server_ids:
            return {"assigned": [], "already_assigned": [], "not_found": []}

        assigned: list[str] = []
        already_assigned: list[str] = []
        not_found: list[str] = []

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT server_id, batch_id FROM servers WHERE server_id = ANY($1::text[])",
                server_ids,
            )
            found = {r["server_id"]: r["batch_id"] for r in rows}

            for sid in server_ids:
                if sid not in found:
                    not_found.append(sid)
                elif found[sid] == batch_id:
                    already_assigned.append(sid)
                else:
                    assigned.append(sid)

            if assigned:
                await conn.execute(
                    """
                    UPDATE servers
                    SET batch_id   = $1,
                        updated_at = now()
                    WHERE server_id = ANY($2::text[])
                    """,
                    batch_id,
                    assigned,
                )

        return {
            "assigned": assigned,
            "already_assigned": already_assigned,
            "not_found": not_found,
        }

    async def get_batch_servers(self, batch_id: str) -> list[dict]:
        """
        Returns all server rows that belong to the given batch.
        Returns an empty list if no servers are assigned (batch may still exist).
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM servers WHERE batch_id = $1 ORDER BY created_at ASC",
                batch_id,
            )
        return [dict(r) for r in rows]

    async def get_batch_history(self, batch_id: str) -> list[dict]:
        """
        Returns all state transitions for every server in the batch,
        ordered chronologically. Raises ValueError if batch does not exist.
        """
        async with self._pool.acquire() as conn:
            exists = await conn.fetchrow(
                "SELECT batch_id FROM batches WHERE batch_id = $1", batch_id
            )
            if exists is None:
                raise ValueError(f"Batch '{batch_id}' not found")

            rows = await conn.fetch(
                """
                SELECT h.*
                FROM state_transition_history h
                JOIN servers s ON s.server_id = h.server_id
                WHERE s.batch_id = $1
                ORDER BY h.timestamp ASC
                """,
                batch_id,
            )
        return [dict(r) for r in rows]

    async def update_batch_config(
        self, batch_id: str, config_type: str, config: dict
    ) -> None:
        """
        Stores a config snapshot on the batch record.
        config_type must be one of: 'replication_config', 'test_launch_config',
        'cutover_launch_config'.
        Raises ValueError for unknown config_type or missing batch.
        """
        allowed = {"replication_config", "test_launch_config", "cutover_launch_config"}
        if config_type not in allowed:
            raise ValueError(f"Unknown config_type '{config_type}'")

        async with self._pool.acquire() as conn:
            result = await conn.execute(
                f"""
                UPDATE batches
                SET {config_type} = $1::jsonb
                WHERE batch_id = $2
                """,
                json.dumps(config),
                batch_id,
            )
        if result == "UPDATE 0":
            raise ValueError(f"Batch '{batch_id}' not found")

    # ------------------------------------------------------------------ #
    #  Global Transition History                                           #
    # ------------------------------------------------------------------ #

    async def list_transitions(
        self,
        server_id: str | None = None,
        batch_id: str | None = None,
        from_state: str | None = None,
        to_state: str | None = None,
        triggered_by: str | None = None,
        job_type: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict]:
        """
        Returns state transitions with composable optional filters.
        When batch_id is provided, joins to servers to filter by batch.
        Ordered chronologically (newest first).
        """
        conditions: list[str] = []
        params: list = []

        join = ""
        if batch_id is not None:
            join = "JOIN servers s ON s.server_id = h.server_id"
            params.append(batch_id)
            conditions.append(f"s.batch_id = ${len(params)}")

        if server_id:
            params.append(server_id)
            conditions.append(f"h.server_id = ${len(params)}")

        if from_state:
            params.append(from_state)
            conditions.append(f"h.from_state = ${len(params)}")

        if to_state:
            params.append(to_state)
            conditions.append(f"h.to_state = ${len(params)}")

        if triggered_by:
            params.append(triggered_by)
            conditions.append(f"h.triggered_by = ${len(params)}")

        if job_type:
            params.append(job_type)
            conditions.append(f"h.job_type = ${len(params)}")

        if since:
            params.append(since)
            conditions.append(f"h.timestamp >= ${len(params)}::timestamptz")

        if until:
            params.append(until)
            conditions.append(f"h.timestamp <= ${len(params)}::timestamptz")

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = (
            f"SELECT h.* FROM state_transition_history h "
            f"{join} {where} ORDER BY h.timestamp DESC"
        )

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]
