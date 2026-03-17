import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from state_manager.state_manager import StateManager
from dispatcher.dispatcher import JobDispatcher
from api.routes.servers import router
from api.routes.batches import router as batches_router
from api.routes.history import router as history_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

AMQP_URL = os.getenv("AMQP_URL", "amqp://admin:changeme@localhost:5672/")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://admin:changeme@localhost:5432/migration_orchestrator",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: open DB pool and RabbitMQ connection.
    app.state.state_manager = await StateManager.create(DATABASE_URL)
    app.state.dispatcher = JobDispatcher(AMQP_URL)
    await app.state.dispatcher.connect()

    yield

    # Shutdown: close both cleanly.
    await app.state.dispatcher.close()
    await app.state.state_manager.close()


app = FastAPI(
    title="Migration Orchestrator API",
    description="Orchestrates end-to-end server migrations via AWS MGN.",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router,         prefix="/servers", tags=["servers"])
app.include_router(batches_router, prefix="/batches", tags=["batches"])
app.include_router(history_router, prefix="/history", tags=["history"])
