# Server States and Failure Modes

## 1. Complete State Reference

Every state a source server can occupy during its migration lifecycle.
States fall into four categories: **ACTIVE** (automated work running), **IN_PROGRESS** (paused at a human gate), **COMPLETED** (migration finished), **ERROR** (requires human attention).

| # | State | Category | Stage | Triggered By | What Happens Next |
|---|-------|----------|-------|--------------|-------------------|
| 1 | `PENDING` | ACTIVE | 1 — Onboarding | Server registered via `POST /servers` | Engineer calls `/start` → dispatches `ADD_SERVER` job |
| 2 | `AGENT_INSTALLED` | ACTIVE | 1 — Onboarding | `ADD_SERVER` job confirms server visible in MGN | Engineer calls `/configure-replication` → dispatches `CONFIGURE_REPLICATION_SETTINGS` |
| 3 | `REPLICATION_CONFIGURED` | ACTIVE | 2 — Replication | `CONFIGURE_REPLICATION_SETTINGS` job calls `mgn.update_replication_configuration()` | Worker immediately advances to `AWAITING_REPLICATION_APPROVAL` |
| 4 | `AWAITING_REPLICATION_APPROVAL` | IN_PROGRESS | 2 — Replication | Worker advances from `REPLICATION_CONFIGURED` | Engineer approves → `REPLICATION_STARTED` + `START_REPLICATION` dispatched; or rejects → `FAILED` |
| 5 | `REPLICATION_STARTED` | ACTIVE | 2 — Replication | Engineer approves replication; `START_REPLICATION` calls `mgn.start_replication()` | Poller dispatches `POLL_REPLICATION_STATUS` until lag = 0, then advances to `READY_FOR_TESTING` |
| 6 | `READY_FOR_TESTING` | ACTIVE | 2 — Replication | Poller confirms replication lag reached zero | Engineer calls `/configure-test-launch` → dispatches `CONFIGURE_TEST_LAUNCH_TEMPLATE` |
| 7 | `TEST_LAUNCH_TEMPLATE_CONFIGURED` | ACTIVE | 3 — Test Launch | `CONFIGURE_TEST_LAUNCH_TEMPLATE` calls `mgn.update_launch_configuration(launchDisposition=TEST)` | Worker immediately advances to `AWAITING_TEST_LAUNCH_APPROVAL` |
| 8 | `AWAITING_TEST_LAUNCH_APPROVAL` | IN_PROGRESS | 3 — Test Launch | Worker advances from `TEST_LAUNCH_TEMPLATE_CONFIGURED` | Engineer approves → `TEST_INSTANCE_LAUNCHING` + `LAUNCH_TEST_INSTANCE` dispatched; or rejects → `FAILED` |
| 9 | `TEST_INSTANCE_LAUNCHING` | ACTIVE | 3 — Test Launch | `LAUNCH_TEST_INSTANCE` calls `mgn.start_test()` | Poller dispatches `POLL_TEST_INSTANCE_STATUS` until instance exists, then advances to `TEST_INSTANCE_RUNNING` |
| 10 | `TEST_INSTANCE_RUNNING` | ACTIVE | 3 — Test Launch | Poller confirms test instance launched | Poller continues checking EC2 status checks; advances to `AWAITING_TEST_VALIDATION` once healthy |
| 11 | `AWAITING_TEST_VALIDATION` | IN_PROGRESS | 3 — Test Launch | Poller confirms test instance is healthy | Engineer approves (validated test instance) → `TEST_FINALIZED` + `FINALIZE_TEST` dispatched; or rejects → `FAILED` |
| 12 | `TEST_FINALIZED` | ACTIVE | 3 — Test Launch | `FINALIZE_TEST` calls `ec2.terminate_instances()` on the test EC2 | Worker advances to `CUTOVER_LAUNCH_TEMPLATE_CONFIGURED` |
| 13 | `CUTOVER_LAUNCH_TEMPLATE_CONFIGURED` | ACTIVE | 4 — Cutover | Worker advances from `TEST_FINALIZED` | Engineer calls `/configure-cutover-launch` → dispatches `CONFIGURE_CUTOVER_LAUNCH_TEMPLATE` |
| 14 | `AWAITING_CUTOVER_LAUNCH_APPROVAL` | IN_PROGRESS | 4 — Cutover | `CONFIGURE_CUTOVER_LAUNCH_TEMPLATE` calls `mgn.update_launch_configuration(launchDisposition=CUTOVER)` then advances | Engineer approves → `CUTOVER_STARTED` + `START_CUTOVER` dispatched; or rejects → `FAILED` |
| 15 | `CUTOVER_STARTED` | ACTIVE | 4 — Cutover | `START_CUTOVER` calls `mgn.start_cutover()` | Poller dispatches `POLL_CUTOVER_SYNC_STATUS`; advances to `CUTOVER_SYNC_IN_PROGRESS` |
| 16 | `CUTOVER_SYNC_IN_PROGRESS` | ACTIVE | 4 — Cutover | Poller confirms cutover sync began | Poller continues; advances to `READY_FOR_CUTOVER_LAUNCH` once final sync complete |
| 17 | `READY_FOR_CUTOVER_LAUNCH` | ACTIVE | 4 — Cutover | Poller confirms final sync done | `LAUNCH_CUTOVER_INSTANCE` worker confirms cutover instance ID via `mgn.describe_source_servers()`; dispatches `POLL_CUTOVER_INSTANCE_STATUS` |
| 18 | `CUTOVER_INSTANCE_LAUNCHING` | ACTIVE | 4 — Cutover | Worker confirms cutover instance ID available | Poller checks until instance exists; advances to `CUTOVER_INSTANCE_RUNNING` |
| 19 | `CUTOVER_INSTANCE_RUNNING` | ACTIVE | 4 — Cutover | Poller confirms cutover instance launched | Poller continues checking EC2 status checks; advances to `AWAITING_CUTOVER_VALIDATION` once healthy |
| 20 | `AWAITING_CUTOVER_VALIDATION` | IN_PROGRESS | 4 — Cutover | Poller confirms production instance is healthy | Engineer approves (production validated) → `CUTOVER_FINALIZED` + `FINALIZE_CUTOVER` dispatched; or rejects → `FAILED` |
| 21 | `CUTOVER_FINALIZED` | ACTIVE | 4 — Cutover | `FINALIZE_CUTOVER` calls `mgn.finalize_cutover()` | Worker advances to `DISCONNECTING`; dispatches `DISCONNECT_SOURCE_SERVER` |
| 22 | `DISCONNECTING` | ACTIVE | 5 — Cleanup | `DISCONNECT_SOURCE_SERVER` calls `mgn.disconnect_from_service()` | Poller dispatches `POLL_DISCONNECT_STATUS`; advances to `DISCONNECTED` |
| 23 | `DISCONNECTED` | ACTIVE | 5 — Cleanup | Poller confirms source server disconnected from MGN | Worker advances to `AWAITING_ARCHIVE_APPROVAL` |
| 24 | `AWAITING_ARCHIVE_APPROVAL` | IN_PROGRESS | 5 — Cleanup | Worker advances from `DISCONNECTED` | Engineer approves → `ARCHIVED` + `ARCHIVE_SOURCE_SERVER` dispatched; or rejects → `FAILED` |
| 25 | `ARCHIVED` | ACTIVE | 5 — Cleanup | `ARCHIVE_SOURCE_SERVER` calls `mgn.mark_as_archived()` | Worker advances to `AWAITING_CLEANUP_APPROVAL` |
| 26 | `AWAITING_CLEANUP_APPROVAL` | IN_PROGRESS | 5 — Cleanup | Worker advances from `ARCHIVED` | Engineer approves → `CLEANUP_COMPLETE` + `DELETE_REPLICATION_COMPONENTS` dispatched; or rejects → `FAILED` |
| 27 | `CLEANUP_COMPLETE` | COMPLETED | 5 — Cleanup | `DELETE_REPLICATION_COMPONENTS` calls `mgn.delete_source_server()` | Terminal. Migration fully complete. No further transitions. |
| 28 | `FAILED` | ERROR | — | Rollback worker (automated failure) or engineer rejection at any gate | Terminal. Engineer investigates. Can reset to `PENDING` via `POST /servers/{id}/reset` if AWS state is clean. |
| 29 | `FROZEN` | ERROR | — | Rollback itself failed, or failure in the cutover-committed zone | Terminal. Human AWS inspection required before the system will touch this server again. Cannot be reset via the API. |

---

## 2. Failure Modes by State

A server enters `FAILED` or `FROZEN` via two paths:
- **Automated failure**: a worker job fails → message goes to DLX → rollback worker executes undo actions and marks the server `FAILED` or `FROZEN`.
- **Manual rejection**: an engineer calls a `reject-*` endpoint at any `AWAITING_*` gate → API advances directly to `FAILED` (no rollback worker involved — the engineer is aware of the context).

| State at Failure | How Failure Occurs | Failure Path | Undo Action | Final State |
|------------------|--------------------|--------------|-------------|-------------|
| `PENDING` | `ADD_SERVER` job: server not found in MGN, or found in DISCONNECTED state | Automated | None (no AWS resources exist) | `FAILED` |
| `AGENT_INSTALLED` | `CONFIGURE_REPLICATION_SETTINGS` job: `mgn.update_replication_configuration()` rejected (invalid subnet ID, bad security group IDs, unsupported instance type) | Automated | None | `FAILED` |
| `REPLICATION_CONFIGURED` | Worker fails to advance state or dispatch job (DB/queue error) | Automated | None | `FAILED` |
| `AWAITING_REPLICATION_APPROVAL` | Engineer rejects the replication configuration | Manual rejection | None | `FAILED` |
| `REPLICATION_STARTED` | Poller timeout: replication never became healthy within the configured deadline | Automated | `mgn.stop_replication()` | `FAILED` |
| `READY_FOR_TESTING` | `CONFIGURE_TEST_LAUNCH_TEMPLATE` job: `mgn.update_launch_configuration()` failed (invalid launch template parameters) | Automated | `mgn.stop_replication()` | `FAILED` |
| `TEST_LAUNCH_TEMPLATE_CONFIGURED` | Worker fails to advance state or dispatch job (DB/queue error) | Automated | `mgn.stop_replication()` | `FAILED` |
| `AWAITING_TEST_LAUNCH_APPROVAL` | Engineer rejects the test launch template | Manual rejection | None (API-direct) | `FAILED` |
| `TEST_INSTANCE_LAUNCHING` | Poller timeout: test instance never appeared in EC2 within deadline | Automated | `mgn.terminate_target_instances()` | `FAILED` |
| `TEST_INSTANCE_RUNNING` | Poller timeout: test instance launched but never passed EC2 2/2 status checks | Automated | `mgn.terminate_target_instances()` | `FAILED` |
| `AWAITING_TEST_VALIDATION` | Engineer rejects the test instance (broken OS, incorrect data, app not working) | Manual rejection | None (API-direct) | `FAILED` |
| `TEST_FINALIZED` | `FINALIZE_TEST` job: `ec2.terminate_instances()` failed, or worker fails to advance state | Automated | None (test instance already terminated or never running) | `FAILED` |
| `CUTOVER_LAUNCH_TEMPLATE_CONFIGURED` | `CONFIGURE_CUTOVER_LAUNCH_TEMPLATE` job: `mgn.update_launch_configuration()` failed | Automated | None | `FAILED` |
| `AWAITING_CUTOVER_LAUNCH_APPROVAL` | Engineer rejects the cutover launch template | Manual rejection | None (API-direct) | `FAILED` |
| `CUTOVER_STARTED` | `START_CUTOVER` job: `mgn.start_cutover()` returned error or worker crash | Automated | Attempt `mgn.terminate_target_instances()` | `FROZEN` |
| `CUTOVER_SYNC_IN_PROGRESS` | Poller timeout: final cutover sync never completed within deadline | Automated | Attempt `mgn.terminate_target_instances()` | `FROZEN` |
| `READY_FOR_CUTOVER_LAUNCH` | `LAUNCH_CUTOVER_INSTANCE` job: `mgn.describe_source_servers()` could not confirm cutover instance ID | Automated | Attempt `mgn.terminate_target_instances()` | `FROZEN` |
| `CUTOVER_INSTANCE_LAUNCHING` | Poller timeout: cutover instance never appeared in EC2 within deadline | Automated | Attempt `mgn.terminate_target_instances()` | `FROZEN` |
| `CUTOVER_INSTANCE_RUNNING` | Poller timeout: cutover instance launched but never passed EC2 2/2 status checks | Automated | None (production instance may be live) | `FROZEN` |
| `AWAITING_CUTOVER_VALIDATION` | Engineer rejects the production instance (broken, data incorrect, app down) | Manual rejection | None (API-direct) | `FAILED` |
| `CUTOVER_FINALIZED` | `FINALIZE_CUTOVER` job: `mgn.finalize_cutover()` failed | Automated | None | `FROZEN` |
| `DISCONNECTING` | `DISCONNECT_SOURCE_SERVER` or poller: `mgn.disconnect_from_service()` failed / poller timeout | Automated | None | `FROZEN` |
| `DISCONNECTED` | Worker fails to advance state or dispatch job (DB/queue error) | Automated | None | `FROZEN` |
| `AWAITING_ARCHIVE_APPROVAL` | Engineer rejects archiving | Manual rejection | None (API-direct) | `FAILED` |
| `ARCHIVED` | `ARCHIVE_SOURCE_SERVER` job: `mgn.mark_as_archived()` failed | Automated | None | `FROZEN` |
| `AWAITING_CLEANUP_APPROVAL` | Engineer rejects final cleanup | Manual rejection | None (API-direct) | `FAILED` |

---

## 3. Rollback Groups

The rollback worker assigns each state to a group that determines how much can be safely undone.

| Rollback Group | States Covered | Undo Action | Final State After Rollback |
|----------------|---------------|-------------|---------------------------|
| **Group 1** — No AWS resources created | `PENDING`, `AGENT_INSTALLED`, `REPLICATION_CONFIGURED`, `AWAITING_REPLICATION_APPROVAL` | None | `FAILED` |
| **Group 2** — Replication running, test not started | `REPLICATION_STARTED`, `READY_FOR_TESTING`, `TEST_LAUNCH_TEMPLATE_CONFIGURED`, `AWAITING_TEST_LAUNCH_APPROVAL` | `mgn.stop_replication()` | `FAILED` |
| **Group 3** — Test instance active | `TEST_INSTANCE_LAUNCHING`, `TEST_INSTANCE_RUNNING`, `AWAITING_TEST_VALIDATION` | `mgn.terminate_target_instances()` (replication left running) | `FAILED` |
| **Group 4** — Post-test, pre-cutover | `TEST_FINALIZED`, `CUTOVER_LAUNCH_TEMPLATE_CONFIGURED`, `AWAITING_CUTOVER_LAUNCH_APPROVAL` | None (test instance already gone, only config work done) | `FAILED` |
| **Group 5** — Cutover in progress | `CUTOVER_STARTED`, `CUTOVER_SYNC_IN_PROGRESS`, `READY_FOR_CUTOVER_LAUNCH`, `CUTOVER_INSTANCE_LAUNCHING` | Attempt `mgn.terminate_target_instances()` | `FROZEN` (always, regardless of undo result) |
| **Group 6** — Cutover committed | `CUTOVER_INSTANCE_RUNNING`, `AWAITING_CUTOVER_VALIDATION`, `CUTOVER_FINALIZED`, `DISCONNECTING`, `DISCONNECTED`, `AWAITING_ARCHIVE_APPROVAL`, `ARCHIVED`, `AWAITING_CLEANUP_APPROVAL` | None (production instance live or cleanup in flight) | `FROZEN` (always) |

> **FAILED vs FROZEN distinction:**
> - `FAILED` — the rollback worker executed its undo actions successfully (or there was nothing to undo). The server is in a known clean AWS state. An engineer can call `POST /servers/{id}/reset` to return it to `PENDING` and retry.
> - `FROZEN` — either the rollback itself failed and the AWS environment is ambiguous, or the failure occurred in the cutover-committed zone where no safe automated undo exists. Manual AWS inspection is required before the system will touch this server again. The API will not allow a reset.

---

## 4. Human Gate Summary

All seven human approval gates, what state they sit in, and what each decision does.

| Gate | State | Approve → | Reject → | Dispatched Job on Approve |
|------|-------|-----------|----------|--------------------------|
| Replication approval | `AWAITING_REPLICATION_APPROVAL` | `REPLICATION_STARTED` | `FAILED` | `START_REPLICATION` |
| Test launch approval | `AWAITING_TEST_LAUNCH_APPROVAL` | `TEST_INSTANCE_LAUNCHING` | `FAILED` | `LAUNCH_TEST_INSTANCE` |
| Test instance validation | `AWAITING_TEST_VALIDATION` | `TEST_FINALIZED` | `FAILED` | `FINALIZE_TEST` |
| Cutover launch approval | `AWAITING_CUTOVER_LAUNCH_APPROVAL` | `CUTOVER_STARTED` | `FAILED` | `START_CUTOVER` |
| Cutover instance validation | `AWAITING_CUTOVER_VALIDATION` | `CUTOVER_FINALIZED` | `FAILED` | `FINALIZE_CUTOVER` |
| Archive approval | `AWAITING_ARCHIVE_APPROVAL` | `ARCHIVED` | `FAILED` | `ARCHIVE_SOURCE_SERVER` |
| Cleanup approval | `AWAITING_CLEANUP_APPROVAL` | `CLEANUP_COMPLETE` | `FAILED` | `DELETE_REPLICATION_COMPONENTS` |
