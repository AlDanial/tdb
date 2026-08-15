import asyncio

import pytest

from tdb.dap.messages import Response
from tdb.dap.reverse import ReverseRequester, ReverseRequestError


def make_requester(sent: list[dict]) -> ReverseRequester:
    seq = iter(range(100, 200))
    return ReverseRequester(sent.append, lambda: next(seq))


async def test_request_resolves_on_matching_response():
    sent: list[dict] = []
    requester = make_requester(sent)
    task = asyncio.ensure_future(requester.request("runInTerminal", {"args": ["true"]}))
    await asyncio.sleep(0)
    assert sent[0]["command"] == "runInTerminal"
    assert sent[0]["type"] == "request"
    response = Response(
        seq=1,
        request_seq=sent[0]["seq"],
        command="runInTerminal",
        success=True,
        body={},
    )
    assert requester.route(response) is True
    assert (await task).success is True


async def test_failure_response_raises():
    sent: list[dict] = []
    requester = make_requester(sent)
    task = asyncio.ensure_future(requester.request("runInTerminal", {}))
    await asyncio.sleep(0)
    requester.route(
        Response(
            seq=1,
            request_seq=sent[0]["seq"],
            command="runInTerminal",
            success=False,
            message="no emulator",
        )
    )
    with pytest.raises(ReverseRequestError, match="no emulator"):
        await task


async def test_route_ignores_unrelated_messages():
    requester = make_requester([])
    assert requester.route(object()) is False
    assert (
        requester.route(Response(seq=1, request_seq=999, command="x", success=True))
        is False
    )
