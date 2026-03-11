from dataclasses import dataclass
from enum import Enum


class MgnStatus(Enum):
    SUCCESS = "success"
    FAILED  = "failed"


@dataclass
class MgnResult:
    """
    Returned by every MGN worker handler.

    SUCCESS — the AWS API call completed. next_state is the state to advance
              the server to, or None if the state was already set by an
              approval gate and only a poll job needs to be dispatched.

    FAILED  — the AWS API call failed. The worker will nack the message to
              the DLX so the Rollback Worker can undo the step.
    """
    status:     MgnStatus
    next_state: str | None  = None   # None = no state advance, dispatch poll job instead
    metadata:   dict | None = None   # AWS resource IDs, timestamps, etc.
    error:      str | None  = None   # populated on FAILED

    @classmethod
    def success(cls, next_state: str | None = None, metadata: dict | None = None) -> "MgnResult":
        return cls(status=MgnStatus.SUCCESS, next_state=next_state, metadata=metadata)

    @classmethod
    def failed(cls, error: str) -> "MgnResult":
        return cls(status=MgnStatus.FAILED, error=error)
