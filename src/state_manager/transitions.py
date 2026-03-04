from state_manager.states import ServerState, COMPLETED_STATES, ERROR_STATES


class InvalidTransitionError(Exception):
    """
    Raised when a requested state transition is not permitted.
    Contains the server_id, from_state, and to_state so the caller
    can log a precise, actionable error message.
    """
    def __init__(self, server_id: str, from_state: str, to_state: str):
        self.server_id = server_id
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Invalid transition for server '{server_id}': "
            f"{from_state} → {to_state}"
        )


# The complete map of legal state transitions.
#
# Key:   the state a server is currently in
# Value: the set of states it is allowed to move to from there
#
# Design rules encoded here:
#   1. Every active state can transition to FAILED (job failed, rollback succeeded)
#   2. Every state except COMPLETED and ERROR states can transition to FROZEN
#      (rollback failed, human must intervene)
#   3. No state can transition to itself
#   4. COMPLETED and ERROR states have no outgoing transitions — once there,
#      a server cannot move unless manually overridden through the API
#
# WAITING states (AWAITING_*) have exactly two outgoing transitions:
#   - APPROVED → the next automated state
#   - REJECTED → FAILED (human rejected, rollback will be triggered)

VALID_TRANSITIONS: dict[ServerState, frozenset[ServerState]] = {

    # ------------------------------------------------------------------ #
    #  Stage 1 — Onboarding                                               #
    # ------------------------------------------------------------------ #
    ServerState.PENDING: frozenset({
        ServerState.AGENT_INSTALLED,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),
    ServerState.AGENT_INSTALLED: frozenset({
        ServerState.REPLICATION_CONFIGURED,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),

    # ------------------------------------------------------------------ #
    #  Stage 2 — Replication Setup                                        #
    # ------------------------------------------------------------------ #
    ServerState.REPLICATION_CONFIGURED: frozenset({
        ServerState.AWAITING_REPLICATION_APPROVAL,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),
    ServerState.AWAITING_REPLICATION_APPROVAL: frozenset({
        ServerState.REPLICATION_STARTED,    # approved
        ServerState.FAILED,                 # rejected
        ServerState.FROZEN,
    }),
    ServerState.REPLICATION_STARTED: frozenset({
        ServerState.READY_FOR_TESTING,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),
    ServerState.READY_FOR_TESTING: frozenset({
        ServerState.TEST_LAUNCH_TEMPLATE_CONFIGURED,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),

    # ------------------------------------------------------------------ #
    #  Stage 3 — Test Launch                                              #
    # ------------------------------------------------------------------ #
    ServerState.TEST_LAUNCH_TEMPLATE_CONFIGURED: frozenset({
        ServerState.AWAITING_TEST_LAUNCH_APPROVAL,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),
    ServerState.AWAITING_TEST_LAUNCH_APPROVAL: frozenset({
        ServerState.TEST_INSTANCE_LAUNCHING,    # approved
        ServerState.FAILED,                     # rejected
        ServerState.FROZEN,
    }),
    ServerState.TEST_INSTANCE_LAUNCHING: frozenset({
        ServerState.TEST_INSTANCE_RUNNING,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),
    ServerState.TEST_INSTANCE_RUNNING: frozenset({
        ServerState.AWAITING_TEST_VALIDATION,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),
    ServerState.AWAITING_TEST_VALIDATION: frozenset({
        ServerState.TEST_FINALIZED,     # approved — engineer confirmed test works
        ServerState.FAILED,             # rejected — test instance is broken
        ServerState.FROZEN,
    }),
    ServerState.TEST_FINALIZED: frozenset({
        ServerState.CUTOVER_LAUNCH_TEMPLATE_CONFIGURED,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),

    # ------------------------------------------------------------------ #
    #  Stage 4 — Cutover                                                  #
    # ------------------------------------------------------------------ #
    ServerState.CUTOVER_LAUNCH_TEMPLATE_CONFIGURED: frozenset({
        ServerState.AWAITING_CUTOVER_LAUNCH_APPROVAL,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),
    ServerState.AWAITING_CUTOVER_LAUNCH_APPROVAL: frozenset({
        ServerState.CUTOVER_STARTED,    # approved
        ServerState.FAILED,             # rejected
        ServerState.FROZEN,
    }),
    ServerState.CUTOVER_STARTED: frozenset({
        ServerState.CUTOVER_SYNC_IN_PROGRESS,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),
    ServerState.CUTOVER_SYNC_IN_PROGRESS: frozenset({
        ServerState.READY_FOR_CUTOVER_LAUNCH,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),
    ServerState.READY_FOR_CUTOVER_LAUNCH: frozenset({
        ServerState.CUTOVER_INSTANCE_LAUNCHING,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),
    ServerState.CUTOVER_INSTANCE_LAUNCHING: frozenset({
        ServerState.CUTOVER_INSTANCE_RUNNING,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),
    ServerState.CUTOVER_INSTANCE_RUNNING: frozenset({
        ServerState.AWAITING_CUTOVER_VALIDATION,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),
    ServerState.AWAITING_CUTOVER_VALIDATION: frozenset({
        ServerState.CUTOVER_FINALIZED,  # approved — engineer confirmed production works
        ServerState.FAILED,             # rejected — production instance is broken
        ServerState.FROZEN,
    }),
    ServerState.CUTOVER_FINALIZED: frozenset({
        ServerState.DISCONNECTING,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),

    # ------------------------------------------------------------------ #
    #  Stage 5 — Post-Migration Cleanup                                   #
    # ------------------------------------------------------------------ #
    ServerState.DISCONNECTING: frozenset({
        ServerState.DISCONNECTED,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),
    ServerState.DISCONNECTED: frozenset({
        ServerState.AWAITING_ARCHIVE_APPROVAL,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),
    ServerState.AWAITING_ARCHIVE_APPROVAL: frozenset({
        ServerState.ARCHIVED,       # approved — safe to remove from MGN
        ServerState.FAILED,         # rejected
        ServerState.FROZEN,
    }),
    ServerState.ARCHIVED: frozenset({
        ServerState.AWAITING_CLEANUP_APPROVAL,
        ServerState.FAILED,
        ServerState.FROZEN,
    }),
    ServerState.AWAITING_CLEANUP_APPROVAL: frozenset({
        ServerState.CLEANUP_COMPLETE,   # approved — safe to delete replication infrastructure
        ServerState.FAILED,             # rejected
        ServerState.FROZEN,
    }),

    # ------------------------------------------------------------------ #
    #  Completed and error states — no outgoing transitions               #
    # ------------------------------------------------------------------ #
    ServerState.CLEANUP_COMPLETE: frozenset(),
    ServerState.FAILED: frozenset(),
    ServerState.FROZEN: frozenset(),
}


class TransitionValidator:
    """
    The single enforcement point for state transition rules.

    Every state change in the system — whether triggered by a worker,
    an approval gate, or a manual API call — must pass through here
    before any database write happens.
    """

    def validate(
        self,
        server_id: str,
        from_state: str,
        to_state: str,
    ) -> None:
        """
        Validates that moving server_id from from_state to to_state is legal.

        Raises InvalidTransitionError if the transition is not permitted.
        Returns None silently if it is.

        This method is intentionally not async — validation is pure logic,
        no I/O. The State Manager calls this before acquiring the DB lock,
        so invalid transitions are rejected before touching the database.
        """
        try:
            from_enum = ServerState(from_state)
        except ValueError:
            raise InvalidTransitionError(server_id, from_state, to_state)

        try:
            to_enum = ServerState(to_state)
        except ValueError:
            raise InvalidTransitionError(server_id, from_state, to_state)

        # Completed and error states have no outgoing transitions.
        # A server that has finished (successfully or not) cannot be moved
        # further by the automated system.
        if from_enum in COMPLETED_STATES or from_enum in ERROR_STATES:
            raise InvalidTransitionError(server_id, from_state, to_state)

        allowed = VALID_TRANSITIONS.get(from_enum, frozenset())
        if to_enum not in allowed:
            raise InvalidTransitionError(server_id, from_state, to_state)

    def get_valid_next_states(self, current_state: str) -> frozenset[ServerState]:
        """
        Returns the set of states a server can legally move to from current_state.
        Used by the API to show engineers what actions are available.
        """
        try:
            state_enum = ServerState(current_state)
        except ValueError:
            return frozenset()
        return VALID_TRANSITIONS.get(state_enum, frozenset())

    def is_completed(self, state: str) -> bool:
        """Returns True if the server finished the migration successfully."""
        try:
            return ServerState(state) in COMPLETED_STATES
        except ValueError:
            return False

    def is_error(self, state: str) -> bool:
        """Returns True if the server is in a FAILED or FROZEN error state."""
        try:
            return ServerState(state) in ERROR_STATES
        except ValueError:
            return False

