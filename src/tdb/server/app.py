"""FastAPI wiring for the JSON-RPC debug server.

Behavior lives in `tdb.server.handlers.RpcHandlers`. This module's job
is just to map HTTP requests onto handler methods and stream DAP events
over Server-Sent Events.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from .handlers import ControllerRef, RpcHandlers
from .rpc_types import RpcRequest, RpcResponse

# Re-export ControllerRef so existing callers (`from tdb.server.app import
# ControllerRef`) keep working after the move into handlers.py.
__all__ = ["ControllerRef", "create_app"]

log = logging.getLogger(__name__)


def create_app(handlers: RpcHandlers) -> FastAPI:
    """Build the FastAPI app wired to the given RpcHandlers instance.

    `handlers` carries the controller ref + event handler; create_app is
    purely about HTTP transport.
    """
    app = FastAPI(title="tdb debug server")
    actions = handlers.dispatch_table()

    @app.post("/rpc", response_model=RpcResponse)
    async def rpc_endpoint(request: RpcRequest) -> RpcResponse:
        action_fn = actions.get(request.action)
        if action_fn is None:
            return RpcResponse.error(f"Unknown action: {request.action}")
        try:
            async with handlers.session_lock:
                return await action_fn(request.params)
        except Exception as e:
            log.exception("RPC action '%s' failed", request.action)
            return RpcResponse.error(str(e))

    @app.get("/events")
    async def events_endpoint() -> StreamingResponse:
        """SSE endpoint streaming DAP events in real-time."""
        q = handlers.event_handler.subscribe_sse()

        async def event_stream():
            try:
                while True:
                    msg = await q.get()
                    data = json.dumps(msg, default=str)
                    yield f"event: {msg['event']}\ndata: {data}\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                handlers.event_handler.unsubscribe_sse(q)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app
