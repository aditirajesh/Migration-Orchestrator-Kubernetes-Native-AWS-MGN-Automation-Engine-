import asyncio
import sys

sys.path.insert(0, ".")

from dispatcher import JobDispatcher, JobType

# Port-forward must be running:
# kubectl port-forward svc/rabbitmq 5672:5672 -n migration-orchestrator
AMQP_URL = "amqp://admin:changeme@localhost:5672/"


async def main():
    dispatcher = JobDispatcher(AMQP_URL)

    print("Connecting to RabbitMQ...")
    await dispatcher.connect()
    print("Connected.\n")

    # One test message per queue — watch them appear in the management UI
    await dispatcher.dispatch(
        JobType.START_REPLICATION,
        {"server_id": "server-001", "source_region": "us-east-1"},
    )

    await dispatcher.dispatch(
        JobType.POLL_REPLICATION_STATUS,
        {"server_id": "server-001", "replication_job_id": "job-abc123"},
    )

    await dispatcher.dispatch(
        JobType.ROLLBACK_TEST_INSTANCE,
        {"server_id": "server-001", "test_instance_id": "i-0abc123"},
    )

    await dispatcher.close()
    print("\nDone. Open http://localhost:15672 → Queues to verify.")


if __name__ == "__main__":
    asyncio.run(main())
