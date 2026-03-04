import json
import logging

import aio_pika
from aio_pika import IncomingMessage, Message, DeliveryMode

from dispatcher.job_types import JobType, QUEUE_ROUTING
from workers.poll_result import PollResult, PollStatus
from workers.state_manager_client import StateManagerClient

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
        Checks MGN replication state for the source server.
        Advances to READY_FOR_TESTING when replication lag reaches zero
        and MGN reports the server as ready.

        AWS call: mgn_client.describe_source_servers(filters={"sourceServerIDs": [server_id]})
        Complete when: lifecycleState == "READY_FOR_TEST"
        Failed when:   lifecycleState == "DISCONNECTED" or "CUTOVER_COMPLETE"
        """
        # TODO: replace with real MGN API call
        server_id = payload["server_id"]
        logger.debug("[poller] Polling replication status for %s", server_id)
        return PollResult.in_progress()

    async def _poll_test_instance_status(self, payload: dict) -> PollResult:
        """
        Checks EC2 instance state for the launched test instance.
        Advances to TEST_INSTANCE_RUNNING when the instance reaches 'running'.

        AWS call: ec2_client.describe_instances(InstanceIds=[instance_id])
        Complete when: State.Name == "running"
        Failed when:   State.Name in ("terminated", "shutting-down")
        """
        # TODO: replace with real EC2 API call
        server_id   = payload["server_id"]
        instance_id = payload.get("test_instance_id")
        logger.debug("[poller] Polling test instance %s for server %s", instance_id, server_id)
        return PollResult.in_progress()

    async def _poll_cutover_sync_status(self, payload: dict) -> PollResult:
        """
        Checks MGN final replication sync status before the cutover instance
        is launched. MGN must complete the final delta sync before launch.
        Advances to READY_FOR_CUTOVER_LAUNCH when sync is confirmed complete.

        AWS call: mgn_client.describe_source_servers(...)
        Complete when: lifecycleState == "CUTOVER" and finalLaunch is ready
        """
        # TODO: replace with real MGN API call
        server_id = payload["server_id"]
        logger.debug("[poller] Polling cutover sync status for %s", server_id)
        return PollResult.in_progress()

    async def _poll_cutover_instance_status(self, payload: dict) -> PollResult:
        """
        Checks EC2 instance state for the launched cutover (production) instance.
        Advances to CUTOVER_INSTANCE_RUNNING when the instance reaches 'running'.

        AWS call: ec2_client.describe_instances(InstanceIds=[instance_id])
        Complete when: State.Name == "running"
        Failed when:   State.Name in ("terminated", "shutting-down")
        """
        # TODO: replace with real EC2 API call
        server_id   = payload["server_id"]
        instance_id = payload.get("cutover_instance_id")
        logger.debug("[poller] Polling cutover instance %s for server %s", instance_id, server_id)
        return PollResult.in_progress()

    async def _poll_disconnect_status(self, payload: dict) -> PollResult:
        """
        Checks whether the source server has fully disconnected from MGN
        after finalize_cutover. Advances to DISCONNECTED once confirmed.

        AWS call: mgn_client.describe_source_servers(...)
        Complete when: lifecycleState == "DISCONNECTED"
        """
        # TODO: replace with real MGN API call
        server_id = payload["server_id"]
        logger.debug("[poller] Polling disconnect status for %s", server_id)
        return PollResult.in_progress()
