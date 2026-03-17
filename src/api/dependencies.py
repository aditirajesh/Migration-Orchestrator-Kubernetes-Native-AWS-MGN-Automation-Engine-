from fastapi import Request

from state_manager.state_manager import StateManager
from dispatcher.dispatcher import JobDispatcher


def get_state_manager(request: Request) -> StateManager:
    return request.app.state.state_manager


def get_dispatcher(request: Request) -> JobDispatcher:
    return request.app.state.dispatcher
