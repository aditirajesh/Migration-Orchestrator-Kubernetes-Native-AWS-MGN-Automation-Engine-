"""
Global state transition history endpoint with composable filters.
"""

from fastapi import APIRouter, Depends, Query

from state_manager.state_manager import StateManager
from api.models import TransitionResponse
from api.dependencies import get_state_manager

router = APIRouter()


@router.get("", response_model=list[TransitionResponse])
async def list_transitions(
    server_id:    str | None = Query(default=None, description="Filter by server_id"),
    batch_id:     str | None = Query(default=None, description="Filter by batch_id"),
    from_state:   str | None = Query(default=None, description="Filter by from_state"),
    to_state:     str | None = Query(default=None, description="Filter by to_state"),
    triggered_by: str | None = Query(default=None, description="Filter by who triggered the transition"),
    job_type:     str | None = Query(default=None, description="Filter by job_type"),
    since:        str | None = Query(default=None, description="ISO 8601 timestamp lower bound (inclusive)"),
    until:        str | None = Query(default=None, description="ISO 8601 timestamp upper bound (inclusive)"),
    sm: StateManager = Depends(get_state_manager),
):
    """
    List state transitions across all servers with composable filters.
    Returns newest-first. All filters are optional and combinable.
    """
    transitions = await sm.list_transitions(
        server_id=server_id,
        batch_id=batch_id,
        from_state=from_state,
        to_state=to_state,
        triggered_by=triggered_by,
        job_type=job_type,
        since=since,
        until=until,
    )
    return [TransitionResponse.model_validate(t) for t in transitions]
