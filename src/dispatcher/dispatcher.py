import json
import uuid

import aio_pika
from aio_pika import DeliveryMode, Message

from .job_types import JobType, QUEUE_ROUTING


class JobDispatcher:
    """
    Publishes jobs to the correct RabbitMQ queue.

    One instance is created at application startup and reused for the
    lifetime of the process. Call connect() before dispatching any jobs,
    and close() on shutdown.
    """

    def __init__(self, amqp_url: str):
        self._amqp_url = amqp_url
        self._connection = None
        self._channel = None

    async def connect(self) -> None:
        # connect_robust automatically reconnects if the broker goes down.
        # A regular connect() would raise an exception on disconnect and
        # leave the dispatcher permanently broken.
        self._connection = await aio_pika.connect_robust(self._amqp_url)

        # publisher_confirms=True: the broker sends an ack once the message
        # is written to disk. The await on publish() blocks until that ack
        # arrives — so if this line completes, the message is guaranteed safe.
        self._channel = await self._connection.channel(
            publisher_confirms=True
        )

    async def dispatch(self, job_type: JobType, payload: dict) -> None:
        if self._channel is None:
            raise RuntimeError("Dispatcher not connected. Call connect() first.")

        # Look up which queue this job type belongs to.
        queue_name = QUEUE_ROUTING[job_type]

        # Construct the message body as JSON bytes.
        # job_type.value gives the raw string ("start_replication") rather
        # than the Enum object, which is what you want in a serialized message.
        #
        # job_id is a UUID stamped at dispatch time. Workers use this to check
        # whether they have already processed this exact job — if yes, they ack
        # and skip without executing. This makes every job idempotent: safe to
        # receive more than once without causing duplicate side effects.
        job_id = str(uuid.uuid4())
        body = json.dumps({
            "job_id": job_id,
            "job_type": job_type.value,
            "payload": payload,
        }).encode()

        message = Message(
            body=body,
            message_id=job_id,               # also set as AMQP message property
            delivery_mode=DeliveryMode.PERSISTENT,
            content_type="application/json",
        )

        # Publish to the default exchange with routing_key = queue name.
        #
        # From your notes: the default (nameless) exchange is a built-in
        # RabbitMQ exchange that compares the routing key against queue names
        # directly. So routing_key="mgn_jobs" routes straight to the mgn_jobs
        # queue with no extra binding configuration needed.
        #
        # This await only completes once the broker ack is received
        # (because publisher_confirms=True on the channel).
        await self._channel.default_exchange.publish(
            message,
            routing_key=queue_name,
        )

        print(f"[dispatcher] {job_type.value} → {queue_name} (job_id={job_id})")

    async def close(self) -> None:
        if self._connection and not self._connection.is_closed:
            await self._connection.close()
