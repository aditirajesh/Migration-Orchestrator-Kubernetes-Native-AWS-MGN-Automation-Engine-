from dataclasses import dataclass
from enum import Enum


class RollbackStatus(Enum):
    ROLLED_BACK = "rolled_back"  # all undo steps succeeded — server can be marked FAILED
    FROZEN      = "frozen"       # an undo step itself failed — server state is unknown


@dataclass
class RollbackResult:
    """
    Returned by _execute_plan in the RollbackWorker.

    ROLLED_BACK — every undo AWS call succeeded. The server will be marked
                  FAILED — a known clean state an engineer can recover from.

    FROZEN      — an undo call failed, or no safe undo exists (cutover committed).
                  The server will be marked FROZEN — human intervention required
                  before the automated system will touch this server again.
    """
    status:   RollbackStatus
    error:    str | None = None   # populated when an undo step itself raised
    metadata: dict | None = None

    @classmethod
    def rolled_back(cls, metadata: dict | None = None) -> "RollbackResult":
        return cls(status=RollbackStatus.ROLLED_BACK, metadata=metadata)

    @classmethod
    def frozen(cls, error: str) -> "RollbackResult":
        return cls(status=RollbackStatus.FROZEN, error=error)
