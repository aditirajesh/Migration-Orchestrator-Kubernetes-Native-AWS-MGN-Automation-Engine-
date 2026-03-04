"""
End-to-end test script for the StateManager class.

Runs a series of scenarios against a live PostgreSQL instance and prints
a clear pass/fail for each one. Cleans up all inserted rows at the end
so the database is left in the same state it started in.

Usage:
    DATABASE_URL="postgresql://admin:changeme@localhost:5432/migration_orchestrator" \
    python -m test_state_manager
"""

import asyncio
import os

import asyncpg

from state_manager import StateManager
from state_manager.transitions import InvalidTransitionError

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://admin:changeme@localhost:5432/migration_orchestrator",
)

TEST_SERVER_ID = "srv-test-001"

# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

def ok(label: str) -> None:
    print(f"  PASS  {label}")

def fail(label: str, detail: str) -> None:
    print(f"  FAIL  {label}: {detail}")
    raise SystemExit(1)


async def fetch_history(pool: asyncpg.Pool, server_id: str) -> list[dict]:
    """Reads all history rows for a server in chronological order."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT from_state, to_state, job_id, job_type, triggered_by, metadata
            FROM state_transition_history
            WHERE server_id = $1
            ORDER BY timestamp ASC
            """,
            server_id,
        )
    return [dict(r) for r in rows]


async def cleanup(pool: asyncpg.Pool, server_id: str) -> None:
    """
    Removes test rows. History must be deleted before the server row
    because of the ON DELETE RESTRICT foreign key.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM state_transition_history WHERE server_id = $1", server_id
        )
        await conn.execute(
            "DELETE FROM servers WHERE server_id = $1", server_id
        )


# ------------------------------------------------------------------ #
#  Tests                                                               #
# ------------------------------------------------------------------ #

async def run(sm: StateManager, pool: asyncpg.Pool) -> None:

    # ------------------------------------------------------------------ #
    print("\n── Registration ──────────────────────────────────────────────")
    # ------------------------------------------------------------------ #

    # 1. Register a new server — should land in PENDING
    await sm.register_server(
        server_id=TEST_SERVER_ID,
        hostname="web-prod-01",
        ip_address="10.0.1.5",
        assigned_engineer="alice@company.com",
    )
    state = await sm.get_server_state(TEST_SERVER_ID)
    if state != "PENDING":
        fail("register → PENDING", f"got {state}")
    ok("register_server() sets initial state to PENDING")

    # 2. Registering the same server_id a second time must raise
    try:
        await sm.register_server(TEST_SERVER_ID, "web-prod-01", "10.0.1.5")
        fail("duplicate registration raises", "no exception raised")
    except asyncpg.UniqueViolationError:
        ok("duplicate server_id raises UniqueViolationError")

    # ------------------------------------------------------------------ #
    print("\n── Reads ─────────────────────────────────────────────────────")
    # ------------------------------------------------------------------ #

    # 3. get_server() returns all columns
    server = await sm.get_server(TEST_SERVER_ID)
    if server["current_state"] != "PENDING":
        fail("get_server current_state", f"got {server['current_state']}")
    if server["previous_state"] is not None:
        fail("get_server previous_state", f"expected None, got {server['previous_state']}")
    ok("get_server() returns correct initial snapshot")

    # 4. get_server_state() on a missing server must raise
    try:
        await sm.get_server_state("srv-does-not-exist")
        fail("missing server raises ValueError", "no exception raised")
    except ValueError:
        ok("get_server_state() raises ValueError for unknown server_id")

    # ------------------------------------------------------------------ #
    print("\n── Valid transitions ─────────────────────────────────────────")
    # ------------------------------------------------------------------ #

    # 5. PENDING → AGENT_INSTALLED (automated job)
    await sm.advance_state(
        server_id=TEST_SERVER_ID,
        to_state="AGENT_INSTALLED",
        triggered_by="system",
        job_id="job-111",
        job_type="INSTALL_AGENT",
        metadata={"agent_version": "1.2.3"},
    )
    state = await sm.get_server_state(TEST_SERVER_ID)
    if state != "AGENT_INSTALLED":
        fail("PENDING → AGENT_INSTALLED", f"got {state}")
    ok("advance_state()  PENDING → AGENT_INSTALLED")

    # 6. previous_state must be updated in the servers row
    server = await sm.get_server(TEST_SERVER_ID)
    if server["previous_state"] != "PENDING":
        fail("previous_state updated", f"got {server['previous_state']}")
    ok("servers.previous_state updated correctly after transition")

    # 7. History table must have exactly one record so far
    history = await fetch_history(pool, TEST_SERVER_ID)
    if len(history) != 1:
        fail("history row count", f"expected 1, got {len(history)}")
    h = history[0]
    if h["from_state"] != "PENDING" or h["to_state"] != "AGENT_INSTALLED":
        fail("history row content", str(h))
    if h["job_id"] != "job-111" or h["triggered_by"] != "system":
        fail("history row metadata", str(h))
    ok("state_transition_history has correct record after first transition")

    # 8. AGENT_INSTALLED → REPLICATION_CONFIGURED (manual, engineer-triggered)
    await sm.advance_state(
        server_id=TEST_SERVER_ID,
        to_state="REPLICATION_CONFIGURED",
        triggered_by="alice@company.com",
        metadata={"notes": "security groups confirmed"},
    )
    state = await sm.get_server_state(TEST_SERVER_ID)
    if state != "REPLICATION_CONFIGURED":
        fail("AGENT_INSTALLED → REPLICATION_CONFIGURED", f"got {state}")
    ok("advance_state()  AGENT_INSTALLED → REPLICATION_CONFIGURED (manual trigger)")

    # 9. History now has two records, second has no job_id (manual action)
    history = await fetch_history(pool, TEST_SERVER_ID)
    if len(history) != 2:
        fail("history row count after second transition", f"expected 2, got {len(history)}")
    if history[1]["job_id"] is not None:
        fail("manual transition job_id", f"expected None, got {history[1]['job_id']}")
    if history[1]["triggered_by"] != "alice@company.com":
        fail("manual transition triggered_by", history[1]["triggered_by"])
    ok("manual transition correctly records no job_id and engineer as triggered_by")

    # ------------------------------------------------------------------ #
    print("\n── Invalid transitions ───────────────────────────────────────")
    # ------------------------------------------------------------------ #

    # 10. Trying to skip steps must be blocked
    try:
        await sm.advance_state(
            server_id=TEST_SERVER_ID,
            to_state="REPLICATION_STARTED",   # skips AWAITING_REPLICATION_APPROVAL
            triggered_by="system",
        )
        fail("skipping a step raises InvalidTransitionError", "no exception raised")
    except InvalidTransitionError as e:
        ok(f"skipping steps raises InvalidTransitionError  ({e})")

    # 11. Server state must be unchanged after a blocked transition
    state = await sm.get_server_state(TEST_SERVER_ID)
    if state != "REPLICATION_CONFIGURED":
        fail("state unchanged after invalid transition", f"got {state}")
    ok("server state unchanged after blocked transition (transaction rolled back)")

    # 12. History must also be unchanged (no phantom record written)
    history = await fetch_history(pool, TEST_SERVER_ID)
    if len(history) != 2:
        fail("history unchanged after invalid transition", f"expected 2, got {len(history)}")
    ok("history table unchanged after blocked transition")

    # ------------------------------------------------------------------ #
    print("\n── Error state behaviour ─────────────────────────────────────")
    # ------------------------------------------------------------------ #

    # 13. Transition to FAILED (simulating a rollback completing)
    await sm.advance_state(
        server_id=TEST_SERVER_ID,
        to_state="FAILED",
        triggered_by="system",
        job_id="job-999",
        job_type="CONFIGURE_REPLICATION",
        metadata={"error": "AWS API call timed out after 30s"},
    )
    state = await sm.get_server_state(TEST_SERVER_ID)
    if state != "FAILED":
        fail("transition to FAILED", f"got {state}")
    ok("advance_state()  REPLICATION_CONFIGURED → FAILED")

    # 14. Automated system cannot move a server out of FAILED
    try:
        await sm.advance_state(
            server_id=TEST_SERVER_ID,
            to_state="REPLICATION_CONFIGURED",
            triggered_by="system",
        )
        fail("transition from FAILED raises InvalidTransitionError", "no exception raised")
    except InvalidTransitionError as e:
        ok(f"automated system cannot move server out of FAILED  ({e})")


# ------------------------------------------------------------------ #
#  Entry point                                                         #
# ------------------------------------------------------------------ #

async def main() -> None:
    print(f"Connecting to {DATABASE_URL} ...")
    sm = await StateManager.create(DATABASE_URL)

    # Hold a direct pool reference for the helper queries in the tests
    pool = sm._pool

    try:
        # Start clean in case a previous run crashed mid-test
        await cleanup(pool, TEST_SERVER_ID)

        await run(sm, pool)

        print("\n══════════════════════════════════════════════════════════")
        print("  All tests passed")
        print("══════════════════════════════════════════════════════════\n")

    finally:
        await cleanup(pool, TEST_SERVER_ID)
        await sm.close()


if __name__ == "__main__":
    asyncio.run(main())
