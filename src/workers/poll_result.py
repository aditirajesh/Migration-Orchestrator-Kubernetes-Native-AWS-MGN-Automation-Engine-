from dataclasses import dataclass
from enum import Enum


class PollStatus(Enum):
    COMPLETE    = "complete"     # operation finished; advance to new_state
    IN_PROGRESS = "in_progress"  # operation still running, check again later
    FAILED      = "failed"       # operation failed, trigger rollback
    DISPATCHED  = "dispatched"   # handler did its own re-dispatch; just ack


@dataclass
class PollResult:
    status: PollStatus
    new_state: str | None = None   # state to advance the server to on COMPLETE
    error: str | None = None       # failure reason, populated on FAILED

    @classmethod
    def complete(cls, new_state: str) -> "PollResult":
        return cls(status=PollStatus.COMPLETE, new_state=new_state)

    @classmethod
    def in_progress(cls) -> "PollResult":
        return cls(status=PollStatus.IN_PROGRESS)

    @classmethod
    def failed(cls, error: str) -> "PollResult":
        return cls(status=PollStatus.FAILED, error=error)

    @classmethod
    def dispatched(cls) -> "PollResult":
        """
        Returned when a handler advances state and dispatches a follow-up poll
        job itself, without needing the message loop to do either. The loop
        will simply ack the current message and move on.
        """
        return cls(status=PollStatus.DISPATCHED)
