import logging

from dispatcher.job_types import JobType
from state_manager import StateManager

logger = logging.getLogger(__name__)


# Maps each poll job type to the server state it is valid for.
# If the server is not in this state when the poll message arrives,
# the job is stale (a duplicate) and should be skipped.
VALID_STATES_FOR_POLL: dict[JobType, str] = {
    JobType.POLL_REPLICATION_STATUS:      "REPLICATION_STARTED",
    JobType.POLL_TEST_INSTANCE_STATUS:    "TEST_INSTANCE_LAUNCHING",
    JobType.POLL_CUTOVER_SYNC_STATUS:     "CUTOVER_SYNC_IN_PROGRESS",
    JobType.POLL_CUTOVER_INSTANCE_STATUS: "CUTOVER_INSTANCE_LAUNCHING",
    JobType.POLL_DISCONNECT_STATUS:       "DISCONNECTING",
}


class StateManagerClient:
    """
    Worker-facing interface to the StateManager.

    Wraps the core StateManager with two responsibilities:
      1. is_poll_valid() — poller-specific stale-message check
      2. advance_state() — forwards to StateManager with triggered_by="system"
         so callers don't need to repeat worker-specific context

    All reads and writes ultimately go through the StateManager, which
    holds the connection pool and enforces the transition rules.
    """

    def __init__(self, state_manager: StateManager):
        self._sm = state_manager

    async def get_server_state(self, server_id: str) -> str:
        """Returns the current state of the server from the database."""
        return await self._sm.get_server_state(server_id)

    async def is_poll_valid(self, server_id: str, job_type: JobType) -> bool:
        """
        Returns True if the server is in the expected state for this poll job.
        Returns False if the state has already advanced — message is stale.
        """
        expected_state = VALID_STATES_FOR_POLL.get(job_type)
        if expected_state is None:
            return False

        current_state = await self.get_server_state(server_id)
        return current_state == expected_state

    async def advance_state(
        self,
        server_id: str,
        new_state: str,
        job_id: str | None = None,
        job_type: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """
        Advances the server to new_state.
        Always records triggered_by='system' — this client is only used by
        automated workers, never for human approval gate decisions.
        """
        await self._sm.advance_state(
            server_id=server_id,
            to_state=new_state,
            triggered_by="system",
            job_id=job_id,
            job_type=job_type,
            metadata=metadata,
        )
