"""
Batch management and batch pipeline action endpoints.

A batch is a named wave of servers that move through the pipeline together.
Batch pipeline actions apply the same operation to all eligible servers in the
batch simultaneously, returning a three-way result: succeeded / skipped / failed.

A server is SKIPPED if it is not in the required state for the action — it is
not an error, just a gate that is not yet ready for that particular server.
A server FAILS only if the state advance or job dispatch raises an exception.
"""

import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException

from state_manager.state_manager import StateManager
from dispatcher.dispatcher import JobDispatcher
from dispatcher.job_types import JobType
from api.models import (
    AddServersToBatchRequest,
    AddServersToBatchResponse,
    BatchActionResponse,
    BatchApproveRequest,
    BatchConfigureCutoverLaunchRequest,
    BatchConfigureReplicationRequest,
    BatchConfigureTestLaunchRequest,
    BatchRejectRequest,
    BatchResponse,
    CreateBatchRequest,
    TransitionResponse,
    ServerResponse,
)
from api.dependencies import get_state_manager, get_dispatcher

logger = logging.getLogger(__name__)

router = APIRouter()


# -----------------------------------------------------------------------
# Private helpers
# -----------------------------------------------------------------------

async def _get_batch_or_404(sm: StateManager, batch_id: str) -> dict:
    try:
        return await sm.get_batch(batch_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")


async def _batch_action_loop(
    batch_id: str,
    expected_state: str,
    sm: StateManager,
    action_fn: Callable[[dict], Awaitable[None]],
) -> BatchActionResponse:
    """
    Iterates all servers in the batch.
    - SKIP if current_state != expected_state.
    - Call action_fn(server) for eligible servers.
    - FAIL if action_fn raises; SUCCEED otherwise.
    """
    await _get_batch_or_404(sm, batch_id)
    servers = await sm.get_batch_servers(batch_id)

    succeeded: list[str] = []
    skipped:   list[str] = []
    failed:    list[str] = []

    for server in servers:
        sid = server["server_id"]
        if server["current_state"] != expected_state:
            skipped.append(sid)
            continue
        try:
            await action_fn(server)
            succeeded.append(sid)
        except Exception as exc:
            logger.error("[batch:%s] Server %s failed: %s", batch_id, sid, exc)
            failed.append(sid)

    return BatchActionResponse(
        batch_id=batch_id,
        succeeded=succeeded,
        skipped=skipped,
        failed=failed,
    )


async def _batch_approve_gate(
    batch_id: str,
    body: BatchApproveRequest,
    expected_state: str,
    next_state: str,
    job_type: JobType,
    sm: StateManager,
    dispatcher: JobDispatcher,
) -> BatchActionResponse:
    async def action(server: dict) -> None:
        sid = server["server_id"]
        await sm.advance_state(
            server_id=sid,
            to_state=next_state,
            triggered_by=body.engineer_id,
            job_type=f"approve:{expected_state.lower()}",
            metadata={"notes": body.notes} if body.notes else None,
        )
        await dispatcher.dispatch(
            job_type,
            {
                "server_id":            sid,
                "aws_source_server_id": server["aws_source_server_id"],
            },
        )

    result = await _batch_action_loop(batch_id, expected_state, sm, action)
    logger.info(
        "[batch:%s] %s batch-approved %s → %s | ok=%d skip=%d fail=%d",
        batch_id, body.engineer_id, expected_state, next_state,
        len(result.succeeded), len(result.skipped), len(result.failed),
    )
    return result


async def _batch_reject_gate(
    batch_id: str,
    body: BatchRejectRequest,
    expected_state: str,
    sm: StateManager,
) -> BatchActionResponse:
    async def action(server: dict) -> None:
        await sm.advance_state(
            server_id=server["server_id"],
            to_state="FAILED",
            triggered_by=body.engineer_id,
            job_type=f"reject:{expected_state.lower()}",
            metadata={"reason": body.reason},
        )

    result = await _batch_action_loop(batch_id, expected_state, sm, action)
    logger.info(
        "[batch:%s] %s batch-rejected %s → FAILED | ok=%d skip=%d fail=%d",
        batch_id, body.engineer_id, expected_state,
        len(result.succeeded), len(result.skipped), len(result.failed),
    )
    return result


# -----------------------------------------------------------------------
# Batch management endpoints
# -----------------------------------------------------------------------

@router.post("", status_code=201, response_model=BatchResponse)
async def create_batch(
    body: CreateBatchRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """Create a new migration wave (batch)."""
    try:
        await sm.create_batch(
            batch_id=body.batch_id,
            name=body.name,
            description=body.description,
            created_by=body.created_by,
        )
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(
                status_code=409,
                detail=f"Batch '{body.batch_id}' already exists.",
            )
        raise HTTPException(status_code=500, detail=f"Batch creation failed: {e}")

    batch = await sm.get_batch(body.batch_id)
    logger.info("[api] Created batch %s", body.batch_id)
    return BatchResponse.model_validate(batch)


@router.get("", response_model=list[BatchResponse])
async def list_batches(
    sm: StateManager = Depends(get_state_manager),
):
    """List all batches with server counts."""
    batches = await sm.list_batches()
    return [BatchResponse.model_validate(b) for b in batches]


@router.get("/{batch_id}", response_model=BatchResponse)
async def get_batch(
    batch_id: str,
    sm: StateManager = Depends(get_state_manager),
):
    """Get a single batch by ID."""
    batch = await _get_batch_or_404(sm, batch_id)
    return BatchResponse.model_validate(batch)


@router.post("/{batch_id}/servers", response_model=AddServersToBatchResponse)
async def add_servers_to_batch(
    batch_id: str,
    body: AddServersToBatchRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """
    Assign servers to this batch.
    Servers already in this batch are reported as 'already_assigned'.
    Servers not found in the DB are reported as 'not_found'.
    """
    await _get_batch_or_404(sm, batch_id)
    result = await sm.add_servers_to_batch(batch_id, body.server_ids)
    logger.info(
        "[api] Batch %s: assigned=%d already=%d not_found=%d",
        batch_id,
        len(result["assigned"]),
        len(result["already_assigned"]),
        len(result["not_found"]),
    )
    return AddServersToBatchResponse(batch_id=batch_id, **result)


@router.get("/{batch_id}/servers", response_model=list[ServerResponse])
async def get_batch_servers(
    batch_id: str,
    sm: StateManager = Depends(get_state_manager),
):
    """List all servers assigned to a batch."""
    await _get_batch_or_404(sm, batch_id)
    servers = await sm.get_batch_servers(batch_id)
    return [ServerResponse.model_validate(s) for s in servers]


@router.get("/{batch_id}/history", response_model=list[TransitionResponse])
async def get_batch_history(
    batch_id: str,
    sm: StateManager = Depends(get_state_manager),
):
    """Get the full audit trail for all servers in a batch."""
    try:
        history = await sm.get_batch_history(batch_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
    return [TransitionResponse.model_validate(t) for t in history]


# -----------------------------------------------------------------------
# Stage 2 — Batch replication
# -----------------------------------------------------------------------

@router.post("/{batch_id}/configure-replication", response_model=BatchActionResponse)
async def batch_configure_replication(
    batch_id: str,
    body: BatchConfigureReplicationRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Dispatch CONFIGURE_REPLICATION_SETTINGS for all AGENT_INSTALLED servers
    in the batch. Stores the config on the batch record for auditing.
    """
    await _get_batch_or_404(sm, batch_id)

    config = body.model_dump()
    await sm.update_batch_config(batch_id, "replication_config", config)

    async def action(server: dict) -> None:
        await dispatcher.dispatch(
            JobType.CONFIGURE_REPLICATION_SETTINGS,
            {
                "server_id":                       server["server_id"],
                "aws_source_server_id":            server["aws_source_server_id"],
                "staging_subnet_id":               body.staging_subnet_id,
                "replication_security_group_ids":  body.replication_security_group_ids,
                "replication_instance_type":       body.replication_instance_type,
                "data_plane_routing":              body.data_plane_routing,
                "staging_disk_type":               body.staging_disk_type,
            },
        )

    return await _batch_action_loop(batch_id, "AGENT_INSTALLED", sm, action)


@router.post("/{batch_id}/approve-replication", response_model=BatchActionResponse)
async def batch_approve_replication(
    batch_id: str,
    body: BatchApproveRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Approve replication for all AWAITING_REPLICATION_APPROVAL servers.
    Advances each to REPLICATION_STARTED and dispatches START_REPLICATION.
    """
    return await _batch_approve_gate(
        batch_id, body,
        expected_state="AWAITING_REPLICATION_APPROVAL",
        next_state="REPLICATION_STARTED",
        job_type=JobType.START_REPLICATION,
        sm=sm, dispatcher=dispatcher,
    )


@router.post("/{batch_id}/reject-replication", response_model=BatchActionResponse)
async def batch_reject_replication(
    batch_id: str,
    body: BatchRejectRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """Reject replication for all AWAITING_REPLICATION_APPROVAL servers."""
    return await _batch_reject_gate(
        batch_id, body, "AWAITING_REPLICATION_APPROVAL", sm
    )


# -----------------------------------------------------------------------
# Stage 3 — Batch test launch
# -----------------------------------------------------------------------

@router.post("/{batch_id}/configure-test-launch", response_model=BatchActionResponse)
async def batch_configure_test_launch(
    batch_id: str,
    body: BatchConfigureTestLaunchRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Dispatch CONFIGURE_TEST_LAUNCH_TEMPLATE for all READY_FOR_TESTING servers.
    Stores the config on the batch record.
    """
    await _get_batch_or_404(sm, batch_id)

    config = body.model_dump()
    await sm.update_batch_config(batch_id, "test_launch_config", config)

    async def action(server: dict) -> None:
        await dispatcher.dispatch(
            JobType.CONFIGURE_TEST_LAUNCH_TEMPLATE,
            {
                "server_id":            server["server_id"],
                "aws_source_server_id": server["aws_source_server_id"],
                "right_sizing":         body.right_sizing,
                "byol":                 body.byol,
            },
        )

    return await _batch_action_loop(batch_id, "READY_FOR_TESTING", sm, action)


@router.post("/{batch_id}/approve-test-launch", response_model=BatchActionResponse)
async def batch_approve_test_launch(
    batch_id: str,
    body: BatchApproveRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Approve test launch for all AWAITING_TEST_LAUNCH_APPROVAL servers.
    Advances each to TEST_INSTANCE_LAUNCHING and dispatches LAUNCH_TEST_INSTANCE.
    """
    return await _batch_approve_gate(
        batch_id, body,
        expected_state="AWAITING_TEST_LAUNCH_APPROVAL",
        next_state="TEST_INSTANCE_LAUNCHING",
        job_type=JobType.LAUNCH_TEST_INSTANCE,
        sm=sm, dispatcher=dispatcher,
    )


@router.post("/{batch_id}/reject-test-launch", response_model=BatchActionResponse)
async def batch_reject_test_launch(
    batch_id: str,
    body: BatchRejectRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """Reject test launch for all AWAITING_TEST_LAUNCH_APPROVAL servers."""
    return await _batch_reject_gate(
        batch_id, body, "AWAITING_TEST_LAUNCH_APPROVAL", sm
    )


@router.post("/{batch_id}/approve-test-validation", response_model=BatchActionResponse)
async def batch_approve_test_validation(
    batch_id: str,
    body: BatchApproveRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Approve test instances for all AWAITING_TEST_VALIDATION servers.
    Advances each to TEST_FINALIZED and dispatches FINALIZE_TEST.
    """
    return await _batch_approve_gate(
        batch_id, body,
        expected_state="AWAITING_TEST_VALIDATION",
        next_state="TEST_FINALIZED",
        job_type=JobType.FINALIZE_TEST,
        sm=sm, dispatcher=dispatcher,
    )


@router.post("/{batch_id}/reject-test-validation", response_model=BatchActionResponse)
async def batch_reject_test_validation(
    batch_id: str,
    body: BatchRejectRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """Reject test instances for all AWAITING_TEST_VALIDATION servers."""
    return await _batch_reject_gate(
        batch_id, body, "AWAITING_TEST_VALIDATION", sm
    )


# -----------------------------------------------------------------------
# Stage 4 — Batch cutover
# -----------------------------------------------------------------------

@router.post("/{batch_id}/configure-cutover-launch", response_model=BatchActionResponse)
async def batch_configure_cutover_launch(
    batch_id: str,
    body: BatchConfigureCutoverLaunchRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Dispatch CONFIGURE_CUTOVER_LAUNCH_TEMPLATE for all
    CUTOVER_LAUNCH_TEMPLATE_CONFIGURED servers. Stores config on batch record.
    """
    await _get_batch_or_404(sm, batch_id)

    config = body.model_dump()
    await sm.update_batch_config(batch_id, "cutover_launch_config", config)

    async def action(server: dict) -> None:
        await dispatcher.dispatch(
            JobType.CONFIGURE_CUTOVER_LAUNCH_TEMPLATE,
            {
                "server_id":            server["server_id"],
                "aws_source_server_id": server["aws_source_server_id"],
                "right_sizing":         body.right_sizing,
                "byol":                 body.byol,
            },
        )

    return await _batch_action_loop(
        batch_id, "CUTOVER_LAUNCH_TEMPLATE_CONFIGURED", sm, action
    )


@router.post("/{batch_id}/approve-cutover-launch", response_model=BatchActionResponse)
async def batch_approve_cutover_launch(
    batch_id: str,
    body: BatchApproveRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Approve cutover launch for all AWAITING_CUTOVER_LAUNCH_APPROVAL servers.
    Advances each to CUTOVER_STARTED and dispatches START_CUTOVER.

    This is the point of no safe automated return for the batch.
    """
    return await _batch_approve_gate(
        batch_id, body,
        expected_state="AWAITING_CUTOVER_LAUNCH_APPROVAL",
        next_state="CUTOVER_STARTED",
        job_type=JobType.START_CUTOVER,
        sm=sm, dispatcher=dispatcher,
    )


@router.post("/{batch_id}/reject-cutover-launch", response_model=BatchActionResponse)
async def batch_reject_cutover_launch(
    batch_id: str,
    body: BatchRejectRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """Reject cutover launch for all AWAITING_CUTOVER_LAUNCH_APPROVAL servers."""
    return await _batch_reject_gate(
        batch_id, body, "AWAITING_CUTOVER_LAUNCH_APPROVAL", sm
    )


@router.post("/{batch_id}/approve-cutover-validation", response_model=BatchActionResponse)
async def batch_approve_cutover_validation(
    batch_id: str,
    body: BatchApproveRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """
    Approve production instances for all AWAITING_CUTOVER_VALIDATION servers.
    Advances each to CUTOVER_FINALIZED and dispatches FINALIZE_CUTOVER.
    """
    return await _batch_approve_gate(
        batch_id, body,
        expected_state="AWAITING_CUTOVER_VALIDATION",
        next_state="CUTOVER_FINALIZED",
        job_type=JobType.FINALIZE_CUTOVER,
        sm=sm, dispatcher=dispatcher,
    )


@router.post("/{batch_id}/reject-cutover-validation", response_model=BatchActionResponse)
async def batch_reject_cutover_validation(
    batch_id: str,
    body: BatchRejectRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """Reject production instances for all AWAITING_CUTOVER_VALIDATION servers."""
    return await _batch_reject_gate(
        batch_id, body, "AWAITING_CUTOVER_VALIDATION", sm
    )


# -----------------------------------------------------------------------
# Stage 5 — Batch cleanup
# -----------------------------------------------------------------------

@router.post("/{batch_id}/approve-archive", response_model=BatchActionResponse)
async def batch_approve_archive(
    batch_id: str,
    body: BatchApproveRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """Approve archiving for all AWAITING_ARCHIVE_APPROVAL servers."""
    return await _batch_approve_gate(
        batch_id, body,
        expected_state="AWAITING_ARCHIVE_APPROVAL",
        next_state="ARCHIVED",
        job_type=JobType.ARCHIVE_SOURCE_SERVER,
        sm=sm, dispatcher=dispatcher,
    )


@router.post("/{batch_id}/reject-archive", response_model=BatchActionResponse)
async def batch_reject_archive(
    batch_id: str,
    body: BatchRejectRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """Reject archiving for all AWAITING_ARCHIVE_APPROVAL servers."""
    return await _batch_reject_gate(
        batch_id, body, "AWAITING_ARCHIVE_APPROVAL", sm
    )


@router.post("/{batch_id}/approve-cleanup", response_model=BatchActionResponse)
async def batch_approve_cleanup(
    batch_id: str,
    body: BatchApproveRequest,
    sm: StateManager = Depends(get_state_manager),
    dispatcher: JobDispatcher = Depends(get_dispatcher),
):
    """Approve final cleanup for all AWAITING_CLEANUP_APPROVAL servers."""
    return await _batch_approve_gate(
        batch_id, body,
        expected_state="AWAITING_CLEANUP_APPROVAL",
        next_state="CLEANUP_COMPLETE",
        job_type=JobType.DELETE_REPLICATION_COMPONENTS,
        sm=sm, dispatcher=dispatcher,
    )


@router.post("/{batch_id}/reject-cleanup", response_model=BatchActionResponse)
async def batch_reject_cleanup(
    batch_id: str,
    body: BatchRejectRequest,
    sm: StateManager = Depends(get_state_manager),
):
    """Reject cleanup for all AWAITING_CLEANUP_APPROVAL servers."""
    return await _batch_reject_gate(
        batch_id, body, "AWAITING_CLEANUP_APPROVAL", sm
    )
