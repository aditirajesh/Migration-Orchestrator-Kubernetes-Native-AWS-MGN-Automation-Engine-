from enum import Enum


class JobType(str, Enum):
    # ------------------------------------------------------------------
    # MGN JOBS
    # Direct, discrete interactions with AWS MGN/EC2 APIs.
    # Each job is one atomic operation — if it fails, only that step
    # rolls back, not the entire migration.
    # ------------------------------------------------------------------

    # Stage 1: Server Onboarding
    ADD_SERVER = "add_server"                   # Register server with MGN, install agent

    # Stage 2: Replication Setup
    # Note: CONFIGURE_REPLICATION_SETTINGS triggers an approval gate before
    # START_REPLICATION is dispatched — security group rules need human sign-off.
    CONFIGURE_REPLICATION_SETTINGS = "configure_replication_settings"
    START_REPLICATION = "start_replication"

    # Stage 3: Test Launch
    # CONFIGURE_TEST_LAUNCH_TEMPLATE triggers an approval gate before
    # LAUNCH_TEST_INSTANCE is dispatched — human must review the template.
    CONFIGURE_TEST_LAUNCH_TEMPLATE = "configure_test_launch_template"
    LAUNCH_TEST_INSTANCE = "launch_test_instance"
    # FINALIZE_TEST is dispatched after the post-launch approval gate passes.
    # It marks the test complete in MGN and terminates the test instance.
    FINALIZE_TEST = "finalize_test"

    # Stage 4: Cutover
    # Cutover launch template is configured separately from test — production
    # network config, IAM roles, and instance sizing typically differ.
    # Triggers its own approval gate before LAUNCH_CUTOVER_INSTANCE.
    CONFIGURE_CUTOVER_LAUNCH_TEMPLATE = "configure_cutover_launch_template"
    # START_CUTOVER tells MGN to do a final replication sync and pause the source.
    START_CUTOVER = "start_cutover"
    LAUNCH_CUTOVER_INSTANCE = "launch_cutover_instance"
    # FINALIZE_CUTOVER is dispatched after the post-cutover approval gate passes.
    # It marks the migration complete in MGN.
    FINALIZE_CUTOVER = "finalize_cutover"

    # Stage 5: Post-Migration Cleanup
    # These run after FINALIZE_CUTOVER is confirmed.
    # DISCONNECT_SOURCE_SERVER stops the replication agent on the source machine.
    DISCONNECT_SOURCE_SERVER = "disconnect_source_server"
    # ARCHIVE_SOURCE_SERVER removes the server from active MGN migrations.
    ARCHIVE_SOURCE_SERVER = "archive_source_server"
    # DELETE_REPLICATION_COMPONENTS terminates the replication server and
    # deletes the staging area — cleans up all MGN infrastructure for this server.
    DELETE_REPLICATION_COMPONENTS = "delete_replication_components"

    # ------------------------------------------------------------------
    # POLL JOBS
    # Check the status of long-running AWS operations.
    # Each poll job runs on a schedule until the operation completes,
    # then reports back to the Orchestrator API to advance the state.
    # ------------------------------------------------------------------

    POLL_REPLICATION_STATUS = "poll_replication_status"       # until "Ready for Testing"
    POLL_TEST_INSTANCE_STATUS = "poll_test_instance_status"   # until test instance running
    POLL_CUTOVER_SYNC_STATUS = "poll_cutover_sync_status"     # until final sync complete
    POLL_CUTOVER_INSTANCE_STATUS = "poll_cutover_instance_status"  # until prod instance running
    POLL_DISCONNECT_STATUS = "poll_disconnect_status"         # until source disconnected

    # ------------------------------------------------------------------
    # ROLLBACK JOBS
    # Compensating transactions — undo a completed step.
    # Triggered when a job fails (via dead-letter) or an approval gate
    # is rejected by the human reviewer.
    # ------------------------------------------------------------------

    ROLLBACK_REPLICATION_SETTINGS = "rollback_replication_settings"
    ROLLBACK_TEST_LAUNCH_TEMPLATE = "rollback_test_launch_template"
    ROLLBACK_TEST_INSTANCE = "rollback_test_instance"          # terminate test instance
    ROLLBACK_CUTOVER_LAUNCH_TEMPLATE = "rollback_cutover_launch_template"
    ROLLBACK_CUTOVER_INSTANCE = "rollback_cutover_instance"    # terminate cutover, revert to source
    ROLLBACK_REPLICATION_COMPONENTS = "rollback_replication_components"


# Single source of truth: which job type belongs to which queue.
# The dispatcher reads this — nothing else needs to know queue names directly.
QUEUE_ROUTING: dict[JobType, str] = {
    # MGN jobs
    JobType.ADD_SERVER:                         "mgn_jobs",
    JobType.CONFIGURE_REPLICATION_SETTINGS:     "mgn_jobs",
    JobType.START_REPLICATION:                  "mgn_jobs",
    JobType.CONFIGURE_TEST_LAUNCH_TEMPLATE:     "mgn_jobs",
    JobType.LAUNCH_TEST_INSTANCE:               "mgn_jobs",
    JobType.FINALIZE_TEST:                      "mgn_jobs",
    JobType.CONFIGURE_CUTOVER_LAUNCH_TEMPLATE:  "mgn_jobs",
    JobType.START_CUTOVER:                      "mgn_jobs",
    JobType.LAUNCH_CUTOVER_INSTANCE:            "mgn_jobs",
    JobType.FINALIZE_CUTOVER:                   "mgn_jobs",
    JobType.DISCONNECT_SOURCE_SERVER:           "mgn_jobs",
    JobType.ARCHIVE_SOURCE_SERVER:              "mgn_jobs",
    JobType.DELETE_REPLICATION_COMPONENTS:      "mgn_jobs",

    # Poll jobs
    JobType.POLL_REPLICATION_STATUS:            "poll_jobs",
    JobType.POLL_TEST_INSTANCE_STATUS:          "poll_jobs",
    JobType.POLL_CUTOVER_SYNC_STATUS:           "poll_jobs",
    JobType.POLL_CUTOVER_INSTANCE_STATUS:       "poll_jobs",
    JobType.POLL_DISCONNECT_STATUS:             "poll_jobs",

    # Rollback jobs
    JobType.ROLLBACK_REPLICATION_SETTINGS:      "rollback_jobs",
    JobType.ROLLBACK_TEST_LAUNCH_TEMPLATE:      "rollback_jobs",
    JobType.ROLLBACK_TEST_INSTANCE:             "rollback_jobs",
    JobType.ROLLBACK_CUTOVER_LAUNCH_TEMPLATE:   "rollback_jobs",
    JobType.ROLLBACK_CUTOVER_INSTANCE:          "rollback_jobs",
    JobType.ROLLBACK_REPLICATION_COMPONENTS:    "rollback_jobs",
}
