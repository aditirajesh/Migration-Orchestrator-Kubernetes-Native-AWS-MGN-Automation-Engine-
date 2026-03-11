import json
import logging

import aio_pika
from aio_pika import IncomingMessage, Message, DeliveryMode

from dispatcher.job_types import JobType, QUEUE_ROUTING
from workers.poll_result import PollResult, PollStatus
from workers.state_manager_client import StateManagerClient
from workers.aws_clients import get_mgn_client

logger = logging.getLogger(__name__)

# How many times a poll job can be re-enqueued as IN_PROGRESS before
# being treated as a failure. Prevents a stuck operation from cycling forever.
MAX_POLL_ATTEMPTS = 30

POLL_QUEUE = "poll_jobs"


class PollerWorker:
    """
    Consumes jobs from the poll_jobs queue and checks the status of
    long-running AWS operations. Reports outcomes back to the State Manager.
    """

    def __init__(self, amqp_url: str, state_manager: StateManagerClient):
        self._amqp_url = amqp_url
        self._state_manager = state_manager
        self._connection = None
        self._channel = None
        self._mgn = get_mgn_client()

        # Dispatch table: maps each poll job type to its handler function.
        # Adding a new poll type only requires adding an entry here and
        # implementing the handler — nothing else in the worker changes.
        self._handlers: dict[JobType, callable] = {
            JobType.POLL_REPLICATION_STATUS:      self._poll_replication_status,
            JobType.POLL_TEST_INSTANCE_STATUS:    self._poll_test_instance_status,
            JobType.POLL_CUTOVER_SYNC_STATUS:     self._poll_cutover_sync_status,
            JobType.POLL_CUTOVER_INSTANCE_STATUS: self._poll_cutover_instance_status,
            JobType.POLL_DISCONNECT_STATUS:       self._poll_disconnect_status,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(self._amqp_url)
        self._channel = await self._connection.channel()

        # prefetch_count=1: the broker only sends this worker one message at a
        # time. The next message is not delivered until the current one is acked
        # or nacked. Without this, RabbitMQ would flood the worker with all
        # queued messages at once, and a single slow poll could stall all others.
        await self._channel.set_qos(prefetch_count=1)

    async def start(self) -> None:
        """Begin consuming from the poll_jobs queue."""

        # passive=True: assert the queue exists but do not create it.
        # The queue was already created in the management UI with durable=True
        # and DLX configured. Declaring it without passive=True risks
        # conflicting with those settings and raising a channel error.
        queue = await self._channel.declare_queue(POLL_QUEUE, passive=True)
        await queue.consume(self._on_message)
        logger.info("[poller] Listening on %s", POLL_QUEUE)

    async def close(self) -> None:
        if self._connection and not self._connection.is_closed:
            await self._connection.close()

    # ------------------------------------------------------------------
    # Message loop
    # ------------------------------------------------------------------

    async def _on_message(self, message: IncomingMessage) -> None:
        """
        Called by aio-pika for every message delivered from the queue.

        ack/nack are called manually inside a try/except so that exceptions
        are fully handled within this function and never propagate to aio-pika's
        task wrapper. If exceptions escape, aio-pika's channel becomes unstable,
        connect_robust reconnects, and RabbitMQ redelivers the unacked message
        indefinitely — creating the infinite loop this pattern prevents.
        """
        try:
            body = json.loads(message.body)
            job_id    = body.get("job_id") or message.message_id or "unknown"
            job_type  = JobType(body["job_type"])
            payload   = body["payload"]
            attempt   = body.get("attempt", 1)
            server_id = payload["server_id"]

            logger.info(
                "[poller] Received %s for server %s (job_id=%s, attempt=%d)",
                job_type.value, server_id, job_id, attempt,
            )

            # ---- State Manager check ----
            # If the server's state has already advanced past what this poll
            # expects, the message is stale (duplicate or out-of-order).
            # Ack and skip — no work needed.
            is_valid = await self._state_manager.is_poll_valid(server_id, job_type)
            if not is_valid:
                logger.info(
                    "[poller] Skipping %s for %s — state already advanced (duplicate or stale)",
                    job_type.value, server_id,
                )
                await message.ack()
                return

            # ---- Handler dispatch ----
            handler = self._handlers.get(job_type)
            if handler is None:
                logger.error("[poller] No handler for job type: %s — sending to DLX", job_type)
                await message.nack(requeue=False)
                return

            result: PollResult = await handler(payload)

            # ---- Act on result ----
            if result.status == PollStatus.COMPLETE:
                logger.info(
                    "[poller] %s complete for %s → advancing to %s",
                    job_type.value, server_id, result.new_state,
                )
                await self._state_manager.advance_state(
                    server_id,
                    result.new_state,
                    job_id=job_id,
                    job_type=job_type.value,
                )
                await message.ack()

            elif result.status == PollStatus.IN_PROGRESS:
                if attempt >= MAX_POLL_ATTEMPTS:
                    logger.error(
                        "[poller] %s for %s exceeded %d attempts — sending to DLX",
                        job_type.value, server_id, MAX_POLL_ATTEMPTS,
                    )
                    await message.nack(requeue=False)
                    return

                logger.info(
                    "[poller] %s still in progress for %s (attempt %d/%d) — re-enqueuing",
                    job_type.value, server_id, attempt, MAX_POLL_ATTEMPTS,
                )
                # Re-publish to the BACK of the queue, then ack the current
                # message. nack requeue=True would put it at the FRONT, causing
                # a tight loop that starves other jobs.
                await self._reenqueue(body, attempt)
                await message.ack()

            elif result.status == PollStatus.FAILED:
                logger.error(
                    "[poller] %s failed for %s: %s — sending to DLX",
                    job_type.value, server_id, result.error,
                )
                await message.nack(requeue=False)

        except Exception as e:
            # Catch-all: log the error and nack cleanly so the message goes to
            # DLX rather than looping. The channel stays healthy because the
            # exception does not propagate out of this function.
            logger.error("[poller] Unexpected error processing message: %s", e, exc_info=True)
            await message.nack(requeue=False)

    async def _reenqueue(self, body: dict, current_attempt: int) -> None:
        """Re-publish the job to the back of poll_jobs with an incremented attempt counter."""
        body["attempt"] = current_attempt + 1
        await self._channel.default_exchange.publish(
            Message(
                body=json.dumps(body).encode(),
                delivery_mode=DeliveryMode.PERSISTENT,
                content_type="application/json",
            ),
            routing_key=POLL_QUEUE,
        )

    # ------------------------------------------------------------------
    # Handlers — one per poll job type
    #
    # Each handler receives the job payload and returns a PollResult.
    # AWS API calls go here. Handlers know nothing about RabbitMQ or
    # state transitions — that logic lives in _on_message above.
    # ------------------------------------------------------------------

    async def _poll_replication_status(self, payload: dict) -> PollResult:
        """
        Polls MGN until the source server is ready for test launch.
        MGN reports READY_FOR_TEST once replication lag reaches zero and
        the initial sync is complete.

        AWS call: mgn.describe_source_servers
        Complete when: lifeCycle.state == "READY_FOR_TEST" → READY_FOR_TESTING
        Failed when:   lifeCycle.state == "DISCONNECTED"
        """
        server_id            = payload["server_id"]
        aws_source_server_id = payload["aws_source_server_id"]

        try:
            response = self._mgn.describe_source_servers(
                filters={"sourceServerIDs": [aws_source_server_id]}
            )
            items = response.get("items", [])

            if not items:
                return PollResult.failed(f"Server {aws_source_server_id} not found in MGN")

            lifecycle_state = items[0].get("lifeCycle", {}).get("state", "")
            logger.debug("[poller] Replication lifecycle for %s: %s", server_id, lifecycle_state)

            if lifecycle_state == "READY_FOR_TEST":
                return PollResult.complete(new_state="READY_FOR_TESTING")
            elif lifecycle_state == "DISCONNECTED":
                return PollResult.failed(
                    f"Server {aws_source_server_id} disconnected during replication"
                )
            else:
                return PollResult.in_progress()

        except Exception as e:
            return PollResult.failed(f"MGN describe_source_servers error: {e}")

    async def _poll_test_instance_status(self, payload: dict) -> PollResult:
        """
        Polls the MGN job created by start_test until the test instance is running.
        When complete, advances through TEST_INSTANCE_RUNNING and immediately sets
        the AWAITING_TEST_VALIDATION gate so an engineer can validate the instance.

        AWS call: mgn.describe_jobs
        Complete when: job.status == "COMPLETED"
        Failed when:   job.status in ("FAILED", "PARTIALLY_COMPLETED")
        """
        server_id  = payload["server_id"]
        mgn_job_id = payload.get("mgn_job_id")

        if not mgn_job_id:
            return PollResult.failed("No mgn_job_id in payload for test instance poll")

        try:
            response = self._mgn.describe_jobs(filters={"jobIDs": [mgn_job_id]})
            items    = response.get("items", [])

            if not items:
                return PollResult.failed(f"MGN job {mgn_job_id} not found")

            status = items[0].get("status", "")
            logger.debug("[poller] Test launch job %s status: %s", mgn_job_id, status)

            if status == "COMPLETED":
                # Record that the instance is running, then immediately move to the
                # human validation gate. TEST_INSTANCE_RUNNING has no automated work
                # to do — it exists to mark the moment in the audit trail.
                await self._state_manager.advance_state(
                    server_id, "TEST_INSTANCE_RUNNING", job_type="poll_test_instance_status"
                )
                return PollResult.complete(new_state="AWAITING_TEST_VALIDATION")

            elif status in ("FAILED", "PARTIALLY_COMPLETED"):
                return PollResult.failed(
                    f"MGN test launch job {mgn_job_id} ended with status: {status}"
                )
            else:
                return PollResult.in_progress()

        except Exception as e:
            return PollResult.failed(f"MGN describe_jobs error: {e}")

    async def _poll_cutover_sync_status(self, payload: dict) -> PollResult:
        """
        Polls the MGN job created by start_cutover until the final sync and
        cutover instance launch are complete. MGN performs a last replication
        delta before launching the cutover instance — this job covers both.

        AWS call: mgn.describe_jobs
        Complete when: job.status == "COMPLETED" → READY_FOR_CUTOVER_LAUNCH
        Failed when:   job.status in ("FAILED", "PARTIALLY_COMPLETED")
        """
        server_id  = payload["server_id"]
        mgn_job_id = payload.get("mgn_job_id")

        if not mgn_job_id:
            return PollResult.failed("No mgn_job_id in payload for cutover sync poll")

        try:
            response = self._mgn.describe_jobs(filters={"jobIDs": [mgn_job_id]})
            items    = response.get("items", [])

            if not items:
                return PollResult.failed(f"MGN job {mgn_job_id} not found")

            status = items[0].get("status", "")
            logger.debug("[poller] Cutover sync job %s status: %s", mgn_job_id, status)

            if status == "COMPLETED":
                return PollResult.complete(new_state="READY_FOR_CUTOVER_LAUNCH")
            elif status in ("FAILED", "PARTIALLY_COMPLETED"):
                return PollResult.failed(
                    f"MGN cutover job {mgn_job_id} ended with status: {status}"
                )
            else:
                return PollResult.in_progress()

        except Exception as e:
            return PollResult.failed(f"MGN describe_jobs error: {e}")

    async def _poll_cutover_instance_status(self, payload: dict) -> PollResult:
        """
        Polls MGN until the cutover instance reaches the CUTOVER lifecycle state,
        confirming it is fully running. Then immediately sets the
        AWAITING_CUTOVER_VALIDATION gate for engineer sign-off.

        AWS call: mgn.describe_source_servers
        Complete when: lifeCycle.state == "CUTOVER" → CUTOVER_INSTANCE_RUNNING → AWAITING_CUTOVER_VALIDATION
        Failed when:   lifeCycle.state == "DISCONNECTED"
        """
        server_id            = payload["server_id"]
        aws_source_server_id = payload["aws_source_server_id"]

        try:
            response = self._mgn.describe_source_servers(
                filters={"sourceServerIDs": [aws_source_server_id]}
            )
            items = response.get("items", [])

            if not items:
                return PollResult.failed(f"Server {aws_source_server_id} not found in MGN")

            lifecycle_state = items[0].get("lifeCycle", {}).get("state", "")
            logger.debug("[poller] Cutover instance lifecycle for %s: %s", server_id, lifecycle_state)

            if lifecycle_state == "CUTOVER":
                await self._state_manager.advance_state(
                    server_id, "CUTOVER_INSTANCE_RUNNING", job_type="poll_cutover_instance_status"
                )
                return PollResult.complete(new_state="AWAITING_CUTOVER_VALIDATION")
            elif lifecycle_state == "DISCONNECTED":
                return PollResult.failed(
                    f"Server {aws_source_server_id} unexpectedly disconnected during cutover"
                )
            else:
                return PollResult.in_progress()

        except Exception as e:
            return PollResult.failed(f"MGN describe_source_servers error: {e}")

    async def _poll_disconnect_status(self, payload: dict) -> PollResult:
        """
        Polls MGN until the source server is fully disconnected from replication.
        Once confirmed, advances through DISCONNECTED and immediately moves to the
        AWAITING_ARCHIVE_APPROVAL gate so an engineer can approve archiving.

        AWS call: mgn.describe_source_servers
        Complete when: lifeCycle.state == "DISCONNECTED" (or server absent from MGN)
        """
        server_id            = payload["server_id"]
        aws_source_server_id = payload["aws_source_server_id"]

        try:
            response = self._mgn.describe_source_servers(
                filters={"sourceServerIDs": [aws_source_server_id]}
            )
            items = response.get("items", [])

            if items:
                lifecycle_state = items[0].get("lifeCycle", {}).get("state", "")
                logger.debug("[poller] Disconnect lifecycle for %s: %s", server_id, lifecycle_state)

                if lifecycle_state != "DISCONNECTED":
                    return PollResult.in_progress()

            # Either MGN confirms DISCONNECTED or the server is absent (already removed).
            await self._state_manager.advance_state(
                server_id, "DISCONNECTED", job_type="poll_disconnect_status"
            )
            return PollResult.complete(new_state="AWAITING_ARCHIVE_APPROVAL")

        except Exception as e:
            return PollResult.failed(f"MGN describe_source_servers error: {e}")
