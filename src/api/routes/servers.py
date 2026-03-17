"""
All Orchestrator API endpoints for server management.

Organised by pipeline stage. Every action endpoint reads the server's
current_state from the DB before doing any work and returns 409 if the
server is not in the expected state — the engineer gets immediate feedback
without waiting for a worker to fail.

Pipeline linearity is enforced at three layers:
  1. Here  — API state check → 409 if wrong state
  2. Worker — VALID_STATES_FOR_JOB / VALID_STATES_FOR_POLL stale checks
  3. DB     — TransitionValidator + SELECT FOR UPDATE inside advance_state
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from state_manager.state_manager import StateManager
from dispatcher.dispatcher import JobDispatcher
from dispatcher.job_types import JobType
from api.models import (
    RegisterServerRequest,
    StartServerRequest,
    ConfigureReplicationRequest,
    ConfigureTestLaunchRequest,
    ConfigureCutoverLaunchRequest,
    ApproveRequest,
    RejectRequest,
    ResetRequest,
    ServerResponse,
    TransitionResponse,
    ActionResponse,
    BulkRegisterRequest,
    BulkStartRequest,
    BulkItemResult,
    BulkActionResponse,
)
from api.dependencies import get_state_manager, get_dispatcher

logger = logging.getLogger(__name__)

router = APIRouter()


# -----------------------------------------------------------------------
# Private helpers
# -----------------------------------------------------------------------

async def _get_server_or_404(sm: StateManager, server_id: str) -> dict:
    """Fetches the server record, raises 404 if not found."""
    try:
        return await sm.get_server(server_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Server '{server_id}' not found")


async def _require_state(sm: StateManager, server_id: str, expected: str) -> dict:
    """
    Fetches the server record and raises 409 if not in the expected state.
    Returns the full server dict on success so callers can read aws_source_server_id
    without a second DB round-trip.
    """
    server = await _get_server_or_404(sm, server_id)
    if server["current_state"] != expected:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Server '{server_id}' is in state '{server['current_state']}'. "
                f"This action requires state '{expected}'."
            ),
        )
    return server


async def _approve_gate(
    server_id: str,
    body: ApproveRequest,
    expected_state: str,
    next_state: str,
    job_type: JobType,
    sm: StateManager,
    dispatcher: JobDispatcher,
) -> ActionResponse:
    """
    Shared logic for all approval gate endpoints.

    Steps:
      1. Validate current state == expected_state (409 if not)
      2. Advance state to next_state (triggered_by = engineer_id)
      3. Dispatch the follow-up job
    """
    server = await _require_state(sm, server_id, expected_state)

    await sm.advance_state(
        server_id=server_id,
        to_state=next_state,
        triggered_by=body.engineer_id,
        job_type=f"approve:{expected_state.lower()}",
        metadata={"notes": body.notes} if body.notes else None,
    )

    await dispatcher.dispatch(
        job_type,
        {
            "server_id":            server_id,
            "aws_source_server_id": server["aws_source_server_id"],
        },
    )

    logger.info(
        "[api] %s approved gate %s → %s (dispatched %s)",
        body.engineer_id, expected_state, next_state, job_type.value,
    )
    return ActionResponse(
        message=f"Approved. Server advanced to {next_state}. {job_type.value} dispatched.",
        server_id=server_id,
    )


async def _reject_gate(
    server_id: str,
    body: RejectRequest,
    expected_state: str,
    sm: StateManager,
) -> ActionResponse:
    """
    Shared logic for all rejection gate endpoints.

    Steps:
      1. Validate current state == expected_state (409 if not)
      2. Advance state to FAILED (triggered_by = engineer_id)

    No job is dispatched — the rollback worker handles automated failures
    via the DLX. A human rejection is a deliberate decision; the engineer
    is aware of the context and can manage AWS cleanup manually if needed.
    """
    await _require_state(sm, server_id, expected_state)

    await sm.advance_state(
        server_id=server_id,
        to_state="FAILED",
        triggered_by=body.engineer_id,
        job_type=f"reject:{expected_state.lower()}",
        metadata={"reason": body.reason},
    )

    logger.info(
        "[api] %s rejected gate %s → FAILED (reason: %s)",
        body.engineer_id, expected_state, body.reason,
    )
    return ActionResponse(
        message=f"Rejected. Server marked FAILED from {expected_state}.",
        server_id=server_id,
    )


# -----------------------------------------------------------------------
# Query endpoints
# -----------------------------------------------------------------------

#get list of servers
@router.get("", response_model=list[ServerResponse])
async def list_servers(
    state:             str | None = Query(default=None, description="Filter by current_state"),
    batch_id:          str | None = Query(default=None, description="Filter by batch_id. Use 'unassigned' for servers with no batch."),
    assigned_engineer: str | None = Query(default=None, description="Filter by assigned_engineer"),
    hostname:          str | None = Query(default=None, description="Partial hostname match (case-insensitive)"),
    sm: StateManager = Depends(get_state_manager),
):
    """
    List servers with composable optional filters.
    All filters are combinable. Use batch_id='unassigned' to find servers not in any batch.
    """
    servers = await sm.list_servers(
        state_filter=state,
        batch_id=batch_id,
        assigned_engineer=assigned_engineer,
        hostname=hostname,
    )
    return [ServerResponse.model_validate(s) for s in servers]


#get servers according to id 
@router.get("/{server_id}", response_model=ServerResponse)
async def get_server(
    server_id: str,
    sm: StateManager = Depends(get_state_manager),
):
    """Get a single server by ID."""
    server = await _get_server_or_404(sm, server_id)
    return ServerResponse.model_validate(server)


#get server history according to id
@router.get("/{server_id}/history", response_model=list[TransitionResponse])
async def get_server_history(
    server_id: str,
    sm: StateManager = Depends(get_state_manager),
):
    """Get the full state transition audit trail for a server."""
    try:
        history = await sm.get_server_history(server_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Server '{server_id}' not found")
    return [TransitionResponse.model_validate(t) for t in history]


# -----------------------------------------------------------------------
# Stage 1 — Registration and onboarding
# -----------------------------------------------------------------------

#register new server
@router.post("", status_code=201, response_model=ServerResponse)
async def register_server(
    body: RegisterServerRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """
    Register a new server. Creates a PENDING record.
    aws_account_id and aws_region identify the target AWS environment.
    Credentials stay in the k8s secret — not accepted here.
    """
    try:
        await sm.register_server(
            server_id=body.server_id,
            hostname=body.hostname,
            ip_address=body.ip_address,
            aws_account_id=body.aws_account_id,
            aws_region=body.aws_region,
            assigned_engineer=body.assigned_engineer,
            batch_id=body.batch_id,
        )
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(
                status_code=409,
                detail=f"Server '{body.server_id}' is already registered.",
            )
        raise HTTPException(status_code=500, detail=f"Registration failed: {e}")

    server = await sm.get_server(body.server_id)
    logger.info("[api] Registered server %s", body.server_id)
    return ServerResponse.model_validate(server)


#after mgn installation
@router.post("/{server_id}/start", status_code=202, response_model=ActionResponse)
async def start_server(
    server_id: str,
    body: StartServerRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Dispatch ADD_SERVER. Call this once the MGN agent is externally installed
    on the source machine and visible in the MGN console.
    Server must be in PENDING state.
    """
    await _require_state(sm, server_id, "PENDING")

    await dispatcher.dispatch(
        JobType.ADD_SERVER,
        {
            "server_id":            server_id,
            "aws_source_server_id": body.aws_source_server_id,
        },
    )

    logger.info("[api] Dispatched ADD_SERVER for %s", server_id)
    return ActionResponse(message="ADD_SERVER job dispatched.", server_id=server_id)


# -----------------------------------------------------------------------
# Stage 2 — Replication
# -----------------------------------------------------------------------

#configure replication of server
@router.post("/{server_id}/configure-replication", status_code=202, response_model=ActionResponse)
async def configure_replication(
    server_id: str,
    body: ConfigureReplicationRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Dispatch CONFIGURE_REPLICATION_SETTINGS.
    Server must be in AGENT_INSTALLED state.
    On success the worker advances to AWAITING_REPLICATION_APPROVAL.
    """
    server = await _require_state(sm, server_id, "AGENT_INSTALLED")

    await dispatcher.dispatch(
        JobType.CONFIGURE_REPLICATION_SETTINGS,
        {
            "server_id":                       server_id,
            "aws_source_server_id":            server["aws_source_server_id"],
            "staging_subnet_id":               body.staging_subnet_id,
            "replication_security_group_ids":  body.replication_security_group_ids,
            "replication_instance_type":       body.replication_instance_type,
            "data_plane_routing":              body.data_plane_routing,
            "staging_disk_type":               body.staging_disk_type,
        },
    )

    logger.info("[api] Dispatched CONFIGURE_REPLICATION_SETTINGS for %s", server_id)
    return ActionResponse(
        message="CONFIGURE_REPLICATION_SETTINGS job dispatched.",
        server_id=server_id,
    )

#approve replication of server
@router.post("/{server_id}/approve-replication", response_model=ActionResponse)
async def approve_replication(
    server_id: str,
    body: ApproveRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Approve the replication configuration.
    Engineer has reviewed the staging subnet and security group settings.
    Advances AWAITING_REPLICATION_APPROVAL → REPLICATION_STARTED.
    Dispatches START_REPLICATION.
    """
    return await _approve_gate(
        server_id, body,
        expected_state="AWAITING_REPLICATION_APPROVAL",
        next_state="REPLICATION_STARTED",
        job_type=JobType.START_REPLICATION,
        sm=sm, dispatcher=dispatcher,
    )

#reject replication of server
@router.post("/{server_id}/reject-replication", response_model=ActionResponse)
async def reject_replication(
    server_id: str,
    body: RejectRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """
    Reject the replication configuration.
    Marks the server FAILED. No AWS resources exist yet at this gate.
    """
    return await _reject_gate(server_id, body, "AWAITING_REPLICATION_APPROVAL", sm)


# -----------------------------------------------------------------------
# Stage 3 — Test launch
# -----------------------------------------------------------------------

#configure test launch for a server
@router.post("/{server_id}/configure-test-launch", status_code=202, response_model=ActionResponse)
async def configure_test_launch(
    server_id: str,
    body: ConfigureTestLaunchRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Dispatch CONFIGURE_TEST_LAUNCH_TEMPLATE.
    Server must be in READY_FOR_TESTING (replication lag reached zero).
    On success the worker advances to AWAITING_TEST_LAUNCH_APPROVAL.
    """
    server = await _require_state(sm, server_id, "READY_FOR_TESTING")

    await dispatcher.dispatch(
        JobType.CONFIGURE_TEST_LAUNCH_TEMPLATE,
        {
            "server_id":            server_id,
            "aws_source_server_id": server["aws_source_server_id"],
            "right_sizing":         body.right_sizing,
            "byol":                 body.byol,
        },
    )

    logger.info("[api] Dispatched CONFIGURE_TEST_LAUNCH_TEMPLATE for %s", server_id)
    return ActionResponse(
        message="CONFIGURE_TEST_LAUNCH_TEMPLATE job dispatched.",
        server_id=server_id,
    )


#approve test launch of a server
@router.post("/{server_id}/approve-test-launch", response_model=ActionResponse)
async def approve_test_launch(
    server_id: str,
    body: ApproveRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Approve the test launch template.
    Engineer has reviewed the test instance configuration.
    Advances AWAITING_TEST_LAUNCH_APPROVAL → TEST_INSTANCE_LAUNCHING.
    Dispatches LAUNCH_TEST_INSTANCE.
    """
    return await _approve_gate(
        server_id, body,
        expected_state="AWAITING_TEST_LAUNCH_APPROVAL",
        next_state="TEST_INSTANCE_LAUNCHING",
        job_type=JobType.LAUNCH_TEST_INSTANCE,
        sm=sm, dispatcher=dispatcher,
    )

#reject test launch 
@router.post("/{server_id}/reject-test-launch", response_model=ActionResponse)
async def reject_test_launch(
    server_id: str,
    body: RejectRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """
    Reject the test launch template.
    Marks the server FAILED. Replication remains running — only the
    template config is being rejected, not the replication itself.
    """
    return await _reject_gate(server_id, body, "AWAITING_TEST_LAUNCH_APPROVAL", sm)

#approve test instance after manual validation 
@router.post("/{server_id}/approve-test-validation", response_model=ActionResponse)
async def approve_test_validation(
    server_id: str,
    body: ApproveRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Approve the test instance after manual validation.
    Engineer has logged into the test instance and confirmed it is working.
    Advances AWAITING_TEST_VALIDATION → TEST_FINALIZED.
    Dispatches FINALIZE_TEST (terminates test instance, begins cutover setup).
    """
    return await _approve_gate(
        server_id, body,
        expected_state="AWAITING_TEST_VALIDATION",
        next_state="TEST_FINALIZED",
        job_type=JobType.FINALIZE_TEST,
        sm=sm, dispatcher=dispatcher,
    )


@router.post("/{server_id}/reject-test-validation", response_model=ActionResponse)
async def reject_test_validation(
    server_id: str,
    body: RejectRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """
    Reject the test instance — it is broken or data is incorrect.
    Marks the server FAILED. The rollback worker will terminate the test
    instance (Group 3 rollback plan).
    """
    return await _reject_gate(server_id, body, "AWAITING_TEST_VALIDATION", sm)


# -----------------------------------------------------------------------
# Stage 4 — Cutover
# -----------------------------------------------------------------------

@router.post("/{server_id}/configure-cutover-launch", status_code=202, response_model=ActionResponse)
async def configure_cutover_launch(
    server_id: str,
    body: ConfigureCutoverLaunchRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Dispatch CONFIGURE_CUTOVER_LAUNCH_TEMPLATE.
    Server must be in CUTOVER_LAUNCH_TEMPLATE_CONFIGURED (set by FINALIZE_TEST).
    Production config typically differs from test — instance type, IAM role,
    network placement, and security groups.
    On success the worker advances to AWAITING_CUTOVER_LAUNCH_APPROVAL.
    """
    server = await _require_state(sm, server_id, "CUTOVER_LAUNCH_TEMPLATE_CONFIGURED")

    await dispatcher.dispatch(
        JobType.CONFIGURE_CUTOVER_LAUNCH_TEMPLATE,
        {
            "server_id":            server_id,
            "aws_source_server_id": server["aws_source_server_id"],
            "right_sizing":         body.right_sizing,
            "byol":                 body.byol,
        },
    )

    logger.info("[api] Dispatched CONFIGURE_CUTOVER_LAUNCH_TEMPLATE for %s", server_id)
    return ActionResponse(
        message="CONFIGURE_CUTOVER_LAUNCH_TEMPLATE job dispatched.",
        server_id=server_id,
    )


@router.post("/{server_id}/approve-cutover-launch", response_model=ActionResponse)
async def approve_cutover_launch(
    server_id: str,
    body: ApproveRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Approve the cutover launch template.
    Engineer has reviewed the production instance configuration.
    Advances AWAITING_CUTOVER_LAUNCH_APPROVAL → CUTOVER_STARTED.
    Dispatches START_CUTOVER.

    This is the point of no safe automated return — once cutover starts,
    the server enters the committed zone and failures lead to FROZEN.
    """
    return await _approve_gate(
        server_id, body,
        expected_state="AWAITING_CUTOVER_LAUNCH_APPROVAL",
        next_state="CUTOVER_STARTED",
        job_type=JobType.START_CUTOVER,
        sm=sm, dispatcher=dispatcher,
    )


@router.post("/{server_id}/reject-cutover-launch", response_model=ActionResponse)
async def reject_cutover_launch(
    server_id: str,
    body: RejectRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """
    Reject the cutover launch template.
    Safe to reject here — no production instance has been launched yet.
    Marks the server FAILED.
    """
    return await _reject_gate(server_id, body, "AWAITING_CUTOVER_LAUNCH_APPROVAL", sm)


@router.post("/{server_id}/approve-cutover-validation", response_model=ActionResponse)
async def approve_cutover_validation(
    server_id: str,
    body: ApproveRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Approve the cutover — production instance is live and healthy.
    Engineer has validated the production instance is working correctly.
    Advances AWAITING_CUTOVER_VALIDATION → CUTOVER_FINALIZED.
    Dispatches FINALIZE_CUTOVER, which automatically chains into the
    full cleanup sequence (disconnect → archive → cleanup).
    """
    return await _approve_gate(
        server_id, body,
        expected_state="AWAITING_CUTOVER_VALIDATION",
        next_state="CUTOVER_FINALIZED",
        job_type=JobType.FINALIZE_CUTOVER,
        sm=sm, dispatcher=dispatcher,
    )


@router.post("/{server_id}/reject-cutover-validation", response_model=ActionResponse)
async def reject_cutover_validation(
    server_id: str,
    body: RejectRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """
    Reject the cutover — production instance is broken.
    Marks the server FAILED. The rollback worker will mark it FROZEN
    (cutover-committed zone — no automated undo is possible).
    Human intervention required to assess the production instance.
    """
    return await _reject_gate(server_id, body, "AWAITING_CUTOVER_VALIDATION", sm)


# -----------------------------------------------------------------------
# Stage 5 — Cleanup
# -----------------------------------------------------------------------

@router.post("/{server_id}/approve-archive", response_model=ActionResponse)
async def approve_archive(
    server_id: str,
    body: ApproveRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Approve archiving the source server from active MGN migrations.
    Engineer has confirmed the production instance is stable and the
    source server is no longer needed in the MGN console.
    Advances AWAITING_ARCHIVE_APPROVAL → ARCHIVED.
    Dispatches ARCHIVE_SOURCE_SERVER.
    """
    return await _approve_gate(
        server_id, body,
        expected_state="AWAITING_ARCHIVE_APPROVAL",
        next_state="ARCHIVED",
        job_type=JobType.ARCHIVE_SOURCE_SERVER,
        sm=sm, dispatcher=dispatcher,
    )


@router.post("/{server_id}/reject-archive", response_model=ActionResponse)
async def reject_archive(
    server_id: str,
    body: RejectRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """Reject archiving. Marks the server FAILED."""
    return await _reject_gate(server_id, body, "AWAITING_ARCHIVE_APPROVAL", sm)


@router.post("/{server_id}/approve-cleanup", response_model=ActionResponse)
async def approve_cleanup(
    server_id: str,
    body: ApproveRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Approve final cleanup — permanently delete the replication server and
    staging area for this migration.
    Engineer has confirmed it is safe to destroy all MGN infrastructure.
    Advances AWAITING_CLEANUP_APPROVAL → CLEANUP_COMPLETE.
    Dispatches DELETE_REPLICATION_COMPONENTS.
    """
    return await _approve_gate(
        server_id, body,
        expected_state="AWAITING_CLEANUP_APPROVAL",
        next_state="CLEANUP_COMPLETE",
        job_type=JobType.DELETE_REPLICATION_COMPONENTS,
        sm=sm, dispatcher=dispatcher,
    )


@router.post("/{server_id}/reject-cleanup", response_model=ActionResponse)
async def reject_cleanup(
    server_id: str,
    body: RejectRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """Reject cleanup. Marks the server FAILED."""
    return await _reject_gate(server_id, body, "AWAITING_CLEANUP_APPROVAL", sm)


# -----------------------------------------------------------------------
# Error recovery
# -----------------------------------------------------------------------

@router.post("/{server_id}/reset", response_model=ActionResponse)
async def reset_server(
    server_id: str,
    body: ResetRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """
    Reset a FAILED server back to PENDING so the migration can be retried.

    Only valid for servers in FAILED state (rollback succeeded, known clean
    AWS state). FROZEN servers cannot be reset here — they require manual
    AWS inspection before the system will touch them again.
    """
    try:
        await sm.reset_server(
            server_id=server_id,
            engineer_id=body.engineer_id,
            reason=body.reason,
        )
    except ValueError as e:
        status = 404 if "not found" in str(e).lower() else 409
        raise HTTPException(status_code=status, detail=str(e))

    logger.info("[api] %s reset server %s → PENDING", body.engineer_id, server_id)
    return ActionResponse(message="Server reset to PENDING.", server_id=server_id)


# -----------------------------------------------------------------------
# Bulk operations
# -----------------------------------------------------------------------

@router.post("/bulk", status_code=207, response_model=BulkActionResponse)
async def bulk_register(
    body: BulkRegisterRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """
    Register multiple servers in a single request.
    Returns 207 Multi-Status with per-server succeeded/failed results.
    A single server failure does not abort the others.
    """
    succeeded: list[BulkItemResult] = []
    failed: list[BulkItemResult] = []

    for item in body.servers:
        try:
            await sm.register_server(
                server_id=item.server_id,
                hostname=item.hostname,
                ip_address=item.ip_address,
                aws_account_id=item.aws_account_id,
                aws_region=item.aws_region,
                assigned_engineer=item.assigned_engineer,
                batch_id=item.batch_id,
            )
            succeeded.append(BulkItemResult(server_id=item.server_id, status="succeeded"))
        except Exception as e:
            detail = (
                f"Already registered."
                if "unique" in str(e).lower() or "duplicate" in str(e).lower()
                else str(e)
            )
            failed.append(BulkItemResult(server_id=item.server_id, status="failed", detail=detail))

    logger.info("[api] Bulk register: %d ok, %d failed", len(succeeded), len(failed))
    return BulkActionResponse(succeeded=succeeded, failed=failed)


@router.post("/bulk-start", status_code=207, response_model=BulkActionResponse)
async def bulk_start(
    body: BulkStartRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Dispatch ADD_SERVER for multiple servers in a single request.
    Each server must be in PENDING state. Returns 207 Multi-Status.
    """
    succeeded: list[BulkItemResult] = []
    failed: list[BulkItemResult] = []

    for item in body.servers:
        try:
            state = await sm.get_server_state(item.server_id)
            if state != "PENDING":
                failed.append(BulkItemResult(
                    server_id=item.server_id,
                    status="failed",
                    detail=f"Server is in state '{state}', expected PENDING.",
                ))
                continue

            await dispatcher.dispatch(
                JobType.ADD_SERVER,
                {
                    "server_id":            item.server_id,
                    "aws_source_server_id": item.aws_source_server_id,
                },
            )
            succeeded.append(BulkItemResult(server_id=item.server_id, status="succeeded"))
        except ValueError:
            failed.append(BulkItemResult(
                server_id=item.server_id,
                status="failed",
                detail="Server not found.",
            ))
        except Exception as e:
            failed.append(BulkItemResult(
                server_id=item.server_id,
                status="failed",
                detail=str(e),
            ))

    logger.info("[api] Bulk start: %d ok, %d failed", len(succeeded), len(failed))
    return BulkActionResponse(succeeded=succeeded, failed=failed)
