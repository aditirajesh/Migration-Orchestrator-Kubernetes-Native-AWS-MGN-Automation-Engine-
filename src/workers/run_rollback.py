import asyncio
import logging
import os
import sys

sys.path.insert(0, ".")

from workers.rollback_worker import RollbackWorker
from workers.state_manager_client import StateManagerClient
from state_manager import StateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

AMQP_URL = os.getenv("AMQP_URL", "amqp://admin:changeme@localhost:5672/")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://admin:changeme@postgres.migration-orchestrator.svc.cluster.local:5432/migration_orchestrator",
)


async def main():
    state_manager = await StateManager.create(DATABASE_URL)
    client        = StateManagerClient(state_manager)

    worker = RollbackWorker(amqp_url=AMQP_URL, state_manager=client)

    await worker.connect()
    await worker.start()

    print("[rollback] Running. Waiting for messages. Ctrl+C to stop.")
    try:
        await asyncio.Future()  # run forever until interrupted
    finally:
        await worker.close()
        await state_manager.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[rollback] Stopped.")
