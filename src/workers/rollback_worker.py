import json
import logging

import aio_pika
from aio_pika import IncomingMessage

from workers.rollback_result import RollbackResult, RollbackStatus
from workers.state_manager_client import StateManagerClient
from workers.aws_clients import get_mgn_client

logger = logging.getLogger(__name__)

# The rollback worker consumes from both dead-letter queues directly.
# Failed MGN jobs land in mgn_jobs.failed; failed poll jobs land in
# poll_jobs.failed. Both are bound to the dlx.migration exchange per
# the RabbitMQ setup. Consuming from the isolated .failed queues
# preserves visibility into which worker produced each failure.
_FAILURE_QUEUES = ("mgn_jobs.failed", "poll_jobs.failed")

# States that are already terminal — rollback already ran, or the migration
# completed successfully. If a message arrives for a server in one of these
# states, it is stale and should be acked without any work.
_TERMINAL_STATES = frozenset({
    "FAILED",
    "FROZEN",
    "CLEANUP_COMPLETE",
})

# States in the cutover-committed zone where the server is always marked
# FROZEN regardless of whether an undo action succeeds. The production
# instance is running or disconnection/cleanup is in flight — there is no
# automated path back to a clean pre-cutover state.
_ALWAYS_FROZEN_STATES = frozenset({
    "CUTOVER_INSTANCE_RUNNING",
    "AWAITING_CUTOVER_VALIDATION",
    "CUTOVER_FINALIZED",
    "DISCONNECTING",
    "DISCONNECTED",
    "AWAITING_ARCHIVE_APPROVAL",
    "ARCHIVED",
    "AWAITING_CLEANUP_APPROVAL",
})


class RollbackWorker:
    """
    Consumes failed jobs from mgn_jobs.failed and poll_jobs.failed and
    executes the minimum AWS undo actions needed to return the server
    to a known state, then marks it FAILED or FROZEN.

    FAILED  — undo succeeded. Server is in a clean state.
              An engineer can investigate and reset via the API.
    FROZEN  — undo failed, or failure occurred too late to undo safely.
              Human intervention required before the system touches
              this server again.
    """

    def __init__(self, amqp_url: str, state_manager: StateManagerClient):
        self._amqp_url      = amqp_url
        self._state_manager = state_manager
        self._connection    = None
        self._channel       = None
        self._mgn           = get_mgn_client()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(self._amqp_url)
        self._channel    = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=1)

    async def start(self) -> None:
        # Open one consumer per failure queue. Both route to the same
        # _on_message handler — the job_type field in the message body
        # identifies which worker produced the failure.
        for queue_name in _FAILURE_QUEUES:
            queue = await self._channel.declare_queue(queue_name, passive=True)
            await queue.consume(self._on_message)
            logger.info("[rollback] Listening on %s", queue_name)

    async def close(self) -> None:
        if self._connection and not self._connection.is_closed:
            await self._connection.close()

    # ------------------------------------------------------------------
    # Message loop
    # ------------------------------------------------------------------

    async def _on_message(self, message: IncomingMessage) -> None:
        """
        Called by aio-pika for every message from mgn_jobs.failed and
        poll_jobs.failed.

        Unlike other workers, unexpected exceptions here always result in
        an ack rather than a nack. The .failed queues have no DLX configured
        so nacking would silently drop the message anyway. Acking with an
        error log is explicit — the message is gone but the failure is visible.
        """
        try:
            body      = json.loads(message.body)
            job_type  = body.get("job_type", "unknown")
            payload   = body.get("payload", {})
            server_id = payload.get("server_id")

            if not server_id:
                logger.error("[rollback] Message has no server_id — discarding: %s", body)
                await message.ack()
                return

            logger.info(
                "[rollback] Received failed job '%s' for server %s",
                job_type, server_id,
            )

            # Fetch authoritative state from the DB.
            # The payload state is what the job *expected* — the DB reflects
            # what the server actually reached before the failure.
            server        = await self._state_manager.get_server(server_id)
            current_state = server["current_state"]
            aws_id        = server.get("aws_source_server_id")

            # Stale check: if already terminal, a previous rollback completed
            # or the migration finished. Nothing to do.
            if current_state in _TERMINAL_STATES:
                logger.info(
                    "[rollback] Server %s already in terminal state '%s' — skipping",
                    server_id, current_state,
                )
                await message.ack()
                return

            # Build the rollback plan and execute it.
            plan, intended_final_state = self._get_rollback_plan(current_state, aws_id)
            result = await self._execute_plan(plan)

            # If any undo step failed, override to FROZEN regardless of group.
            final_state = "FROZEN" if result.status == RollbackStatus.FROZEN else intended_final_state

            await self._finalise(
                server_id=server_id,
                final_state=final_state,
                job_type=job_type,
                failed_state=current_state,
                result=result,
            )

            await message.ack()

        except Exception as e:
            # Ack rather than nack — .failed queues have no DLX so nacking
            # drops the message silently. Acking with a logged error is
            # identical in outcome but makes the decision explicit.
            logger.error("[rollback] Unexpected error processing message: %s", e, exc_info=True)
            await message.ack()

    # ------------------------------------------------------------------
    # Rollback plan builder
    # ------------------------------------------------------------------

    def _get_rollback_plan(
        self,
        current_state: str,
        aws_source_server_id: str | None,
    ) -> tuple[list[tuple[str, callable]], str]:
        """
        Returns (plan, intended_final_state).

        plan                  — ordered list of (description, callable) undo steps
        intended_final_state  — "FAILED" or "FROZEN" assuming plan succeeds

        If aws_source_server_id is None (failure before ADD_SERVER stored it),
        all plans are empty — nothing exists in MGN to clean up.

        Design principle: only undo the action that created the resource
        associated with the current state. Do not touch resources that were
        confirmed working before the failure occurred.
        """
        no_aws_id = aws_source_server_id is None

        # Capture in local scope for use in lambdas.
        mgn   = self._mgn
        aws_id = aws_source_server_id

        def stop_replication():
            mgn.stop_replication(sourceServerID=aws_id)

        def terminate_instances():
            mgn.terminate_target_instances(sourceServerIDs=[aws_id])

        # ── Group 1: No AWS resources created yet ────────────────────────
        # Nothing was provisioned in MGN — registration or configuration
        # failed before any replication or instance resource was created.
        if current_state in {
            "PENDING",
            "AGENT_INSTALLED",
            "REPLICATION_CONFIGURED",
            "AWAITING_REPLICATION_APPROVAL",
        }:
            return [], "FAILED"

        # ── Group 2: Replication running, test not started ───────────────
        # Replication was started but the test phase has not begun.
        # Replication has not been validated — safe to stop.
        if current_state in {
            "REPLICATION_STARTED",
            "READY_FOR_TESTING",
            "TEST_LAUNCH_TEMPLATE_CONFIGURED",
            "AWAITING_TEST_LAUNCH_APPROVAL",
        }:
            if no_aws_id:
                return [], "FAILED"
            return [("Stop replication", stop_replication)], "FAILED"

        # ── Group 3: Test instance active ────────────────────────────────
        # A test instance was launched. Terminate it.
        # Replication is intentionally left running — a test failure does
        # not invalidate a working replication stream.
        if current_state in {
            "TEST_INSTANCE_LAUNCHING",
            "TEST_INSTANCE_RUNNING",
            "AWAITING_TEST_VALIDATION",
        }:
            if no_aws_id:
                return [], "FAILED"
            return [("Terminate test instance", terminate_instances)], "FAILED"

        # ── Group 4: Post-test, pre-cutover ──────────────────────────────
        # Test instance is already gone. Only configuration was done since
        # then — nothing to undo on the AWS side.
        if current_state in {
            "TEST_FINALIZED",
            "CUTOVER_LAUNCH_TEMPLATE_CONFIGURED",
            "AWAITING_CUTOVER_LAUNCH_APPROVAL",
        }:
            return [], "FAILED"

        # ── Group 5: Cutover in progress ─────────────────────────────────
        # The cutover has been initiated. Attempt to terminate any launched
        # instance, but always mark FROZEN — the system is in a partially
        # committed state and human review is required regardless of whether
        # the termination succeeds.
        if current_state in {
            "CUTOVER_STARTED",
            "CUTOVER_SYNC_IN_PROGRESS",
            "READY_FOR_CUTOVER_LAUNCH",
            "CUTOVER_INSTANCE_LAUNCHING",
        }:
            if no_aws_id:
                return [], "FROZEN"
            return [("Terminate cutover instance", terminate_instances)], "FROZEN"

        # ── Group 6: Cutover committed ───────────────────────────────────
        # Production instance is running or cleanup is in flight.
        # No automated undo is possible — always FROZEN.
        if current_state in _ALWAYS_FROZEN_STATES:
            return [], "FROZEN"

        # Unknown state — should never happen, but default to FROZEN safely.
        logger.warning(
            "[rollback] Unrecognised state '%s' — defaulting to FROZEN", current_state
        )
        return [], "FROZEN"

    # ------------------------------------------------------------------
    # Plan executor
    # ------------------------------------------------------------------

    async def _execute_plan(
        self,
        plan: list[tuple[str, callable]],
    ) -> RollbackResult:
        """
        Executes each undo step in order.

        Stops immediately on the first failure and returns FROZEN — partial
        cleanup is more dangerous than no cleanup, because it leaves the
        AWS environment in an ambiguous state that is harder to reason about.

        Returns ROLLED_BACK if all steps succeed or the plan is empty.
        """
        if not plan:
            return RollbackResult.rolled_back()

        for description, action in plan:
            try:
                logger.info("[rollback] Executing undo step: %s", description)
                action()
                logger.info("[rollback] Undo step completed: %s", description)
            except Exception as e:
                logger.error(
                    "[rollback] Undo step '%s' failed: %s — marking FROZEN",
                    description, e,
                )
                return RollbackResult.frozen(
                    error=f"Undo step '{description}' failed: {e}"
                )

        return RollbackResult.rolled_back()

    # ------------------------------------------------------------------
    # State finaliser
    # ------------------------------------------------------------------

    async def _finalise(
        self,
        server_id: str,
        final_state: str,
        job_type: str,
        failed_state: str,
        result: RollbackResult,
    ) -> None:
        """
        Advances the server to FAILED or FROZEN and records the full failure
        context in the state transition history metadata.

        This is the only state write the rollback worker makes. The server
        stays at its failed state throughout plan execution — no intermediate
        state advances are recorded during rollback.
        """
        metadata = {
            "failed_job_type": job_type,
            "failed_at_state": failed_state,
            "rollback_status": result.status.value,
        }
        if result.error:
            metadata["rollback_error"] = result.error

        logger.error(
            "[rollback] Server %s → %s  (failed job: %s, failed at state: %s)",
            server_id, final_state, job_type, failed_state,
        )

        await self._state_manager.advance_state(
            server_id=server_id,
            new_state=final_state,
            job_type=f"rollback:{job_type}",
            metadata=metadata,
        )
