from enum import Enum


class ServerState(str, Enum):
    """
    Every state a source server can be in during its migration lifecycle.

    States fall into four categories:
      - ACTIVE:      the system is performing automated work
      - IN_PROGRESS: automated work is paused, waiting for human approval at a gate
      - COMPLETED:   the migration finished successfully — nothing further will happen
      - ERROR:       something went wrong and the server needs human attention
    """

    # ------------------------------------------------------------------ #
    #  Stage 1 — Onboarding                                    [ACTIVE]  #
    # ------------------------------------------------------------------ #
    PENDING = "PENDING"                     # server registered, agent not yet installed
    AGENT_INSTALLED = "AGENT_INSTALLED"     # MGN agent installed, replication not configured

    # ------------------------------------------------------------------ #
    #  Stage 2 — Replication Setup                                        #
    # ------------------------------------------------------------------ #
    REPLICATION_CONFIGURED = "REPLICATION_CONFIGURED"                    # [ACTIVE]

    AWAITING_REPLICATION_APPROVAL = "AWAITING_REPLICATION_APPROVAL"      # [IN_PROGRESS]

    REPLICATION_STARTED = "REPLICATION_STARTED"                          # [ACTIVE]
    READY_FOR_TESTING = "READY_FOR_TESTING"                              # [ACTIVE]

    # ------------------------------------------------------------------ #
    #  Stage 3 — Test Launch                                              #
    # ------------------------------------------------------------------ #
    TEST_LAUNCH_TEMPLATE_CONFIGURED = "TEST_LAUNCH_TEMPLATE_CONFIGURED"  # [ACTIVE]

    AWAITING_TEST_LAUNCH_APPROVAL = "AWAITING_TEST_LAUNCH_APPROVAL"      # [IN_PROGRESS]

    TEST_INSTANCE_LAUNCHING = "TEST_INSTANCE_LAUNCHING"                  # [ACTIVE]
    TEST_INSTANCE_RUNNING = "TEST_INSTANCE_RUNNING"                      # [ACTIVE]

    AWAITING_TEST_VALIDATION = "AWAITING_TEST_VALIDATION"                # [IN_PROGRESS]

    TEST_FINALIZED = "TEST_FINALIZED"                                    # [ACTIVE]

    # ------------------------------------------------------------------ #
    #  Stage 4 — Cutover                                                  #
    # ------------------------------------------------------------------ #
    CUTOVER_LAUNCH_TEMPLATE_CONFIGURED = "CUTOVER_LAUNCH_TEMPLATE_CONFIGURED"  # [ACTIVE]

    AWAITING_CUTOVER_LAUNCH_APPROVAL = "AWAITING_CUTOVER_LAUNCH_APPROVAL"      # [IN_PROGRESS]

    CUTOVER_STARTED = "CUTOVER_STARTED"                                  # [ACTIVE]
    CUTOVER_SYNC_IN_PROGRESS = "CUTOVER_SYNC_IN_PROGRESS"                # [ACTIVE]
    READY_FOR_CUTOVER_LAUNCH = "READY_FOR_CUTOVER_LAUNCH"                # [ACTIVE]
    CUTOVER_INSTANCE_LAUNCHING = "CUTOVER_INSTANCE_LAUNCHING"            # [ACTIVE]
    CUTOVER_INSTANCE_RUNNING = "CUTOVER_INSTANCE_RUNNING"                # [ACTIVE]

    AWAITING_CUTOVER_VALIDATION = "AWAITING_CUTOVER_VALIDATION"          # [IN_PROGRESS]

    CUTOVER_FINALIZED = "CUTOVER_FINALIZED"                              # [ACTIVE]

    # ------------------------------------------------------------------ #
    #  Stage 5 — Post-Migration Cleanup                                   #
    # ------------------------------------------------------------------ #
    DISCONNECTING = "DISCONNECTING"                                      # [ACTIVE]
    DISCONNECTED = "DISCONNECTED"                                        # [ACTIVE]

    AWAITING_ARCHIVE_APPROVAL = "AWAITING_ARCHIVE_APPROVAL"              # [IN_PROGRESS]

    ARCHIVED = "ARCHIVED"                                                # [ACTIVE]

    AWAITING_CLEANUP_APPROVAL = "AWAITING_CLEANUP_APPROVAL"              # [IN_PROGRESS]

    CLEANUP_COMPLETE = "CLEANUP_COMPLETE"                                # [COMPLETED]

    # ------------------------------------------------------------------ #
    #  Error States                                             [ERROR]   #
    # ------------------------------------------------------------------ #

    # FAILED: a job failed and the rollback completed successfully.
    # The server is in a known clean state but the migration did not complete.
    FAILED = "FAILED"

    # FROZEN: a rollback itself failed. The server is in an unknown state.
    # Human intervention is required before the system will touch this server again.
    FROZEN = "FROZEN"


# ------------------------------------------------------------------ #
#  Named sets — used by the TransitionValidator and the API          #
# ------------------------------------------------------------------ #

# IN_PROGRESS_STATES: automated work is paused at a human approval gate.
# The system will not advance these servers until a human acts.
IN_PROGRESS_STATES: frozenset[ServerState] = frozenset({
    ServerState.AWAITING_REPLICATION_APPROVAL,
    ServerState.AWAITING_TEST_LAUNCH_APPROVAL,
    ServerState.AWAITING_TEST_VALIDATION,
    ServerState.AWAITING_CUTOVER_LAUNCH_APPROVAL,
    ServerState.AWAITING_CUTOVER_VALIDATION,
    ServerState.AWAITING_ARCHIVE_APPROVAL,
    ServerState.AWAITING_CLEANUP_APPROVAL,
})

# COMPLETED_STATES: the migration finished successfully.
COMPLETED_STATES: frozenset[ServerState] = frozenset({
    ServerState.CLEANUP_COMPLETE,
})

# ERROR_STATES: something went wrong and the automated system has stopped.
# FAILED = rollback succeeded, server in known clean state.
# FROZEN = rollback failed, server in unknown state.
ERROR_STATES: frozenset[ServerState] = frozenset({
    ServerState.FAILED,
    ServerState.FROZEN,
})
