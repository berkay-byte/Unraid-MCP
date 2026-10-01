"""Progress notifications (#114): stats sampling + batch docker updates.

Protocol tests drive the server through the SDK in-memory client with a
``progress_callback``; the logic-level tests cover the swallow-failures rule.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from unraid_mcp import subscriptions
from unraid_mcp.server import build_server
from unraid_mcp.tools import docker
from unraid_mcp.tools._base import with_heartbeat

from .test_tools_stats import _ack, _fake_connect, _FakeTransport, _next, _settings

URL = "https://tower.local/graphql"


def _recorder():
    events: list[tuple[float, float | None, str | None]] = []

    async def cb(progress, total, message):
        events.append((progress, total, message))

    return events, cb


def _update_route(delay: float = 0.0, *, key: str = "updateContainers", n: int = 2):
    async def _side_effect(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if key in query:
            await asyncio.sleep(delay)
            rows = [
                {"id": f"1:{i}", "names": [f"/c{i}"], "state": "RUNNING", "status": "Up"}
                for i in range(n)
            ]
            return httpx.Response(200, json={"data": {"docker": {key: rows}}})
        return httpx.Response(500)  # startup probes: failure is tolerated

    return _side_effect


# ── Stats sampling ────────────────────────────────────────────────────────────


async def test_stats_progress_per_container_through_protocol(settings_factory, monkeypatch):
    transport = _FakeTransport([_ack(), _next("docker:a"), _next("docker:b"), _next("docker:a")])
    monkeypatch.setattr(subscriptions, "open_ws", _fake_connect(transport))
    events, cb = _recorder()
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(500))
        mcp = build_server(settings_factory())
        async with Client(mcp, raise_exceptions=True) as session:
            result = await session.call_tool("get_docker_container_stats", {}, progress_callback=cb)
    assert result.is_error is False
    assert result.structured_content["sampled"] == 2
    assert [e[0] for e in events] == [1, 2]
    assert "Sampled 2" in events[-1][2]


async def test_stats_without_progress_token_unchanged(settings_factory, monkeypatch):
    transport = _FakeTransport([_ack(), _next("docker:a"), _next("docker:b"), _next("docker:a")])
    monkeypatch.setattr(subscriptions, "open_ws", _fake_connect(transport))
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(500))
        mcp = build_server(settings_factory())
        async with Client(mcp, raise_exceptions=True) as session:
            result = await session.call_tool("get_docker_container_stats", {})
    assert result.is_error is False
    assert result.structured_content["sampled"] == 2


async def test_stats_progress_failure_is_swallowed():
    async def boom(*_a):
        raise RuntimeError("client went away")

    transport = _FakeTransport([_ack(), _next("docker:a"), _next("docker:b"), _next("docker:a")])
    result = await docker.fetch_container_stats(
        None, settings=_settings(), connect=_fake_connect(transport), progress=boom
    )
    assert result["sampled"] == 2


# ── Batch updates ─────────────────────────────────────────────────────────────


async def test_batch_update_progress_through_protocol(settings_factory, monkeypatch):
    monkeypatch.setattr(docker, "UPDATE_HEARTBEAT_S", 0.05)
    events, cb = _recorder()
    with respx.mock:
        respx.post(URL).mock(side_effect=_update_route(delay=0.25))
        mcp = build_server(settings_factory(allow_mutations=True))
        async with Client(mcp, raise_exceptions=True) as session:
            result = await session.call_tool(
                "update_docker_containers",
                {"container_ids": ["1:0", "1:1"], "confirm": True},
                progress_callback=cb,
            )
    assert result.is_error is False
    assert len(events) >= 2
    assert events[0][:2] == (0, 2) and "Updating 2" in events[0][2]
    assert events[-1][:2] == (2, 2) and "Updated 2" in events[-1][2]
    assert any("elapsed" in (e[2] or "") for e in events[1:-1])  # heartbeat


async def test_update_all_progress_through_protocol(settings_factory, monkeypatch):
    monkeypatch.setattr(docker, "UPDATE_HEARTBEAT_S", 0.05)
    events, cb = _recorder()
    with respx.mock:
        respx.post(URL).mock(side_effect=_update_route(delay=0.2, key="updateAllContainers"))
        mcp = build_server(settings_factory(allow_mutations=True, allow_dangerous=True))
        async with Client(mcp, raise_exceptions=True) as session:
            result = await session.call_tool(
                "update_all_docker_containers", {"confirm": True}, progress_callback=cb
            )
    assert result.is_error is False
    assert len(events) >= 2
    assert events[-1][:2] == (2, 2)


async def test_batch_update_without_progress_token_unchanged(settings_factory):
    with respx.mock:
        respx.post(URL).mock(side_effect=_update_route())
        mcp = build_server(settings_factory(allow_mutations=True))
        async with Client(mcp, raise_exceptions=True) as session:
            result = await session.call_tool(
                "update_docker_containers", {"container_ids": ["1:0", "1:1"], "confirm": True}
            )
    assert result.is_error is False
    assert len(result.structured_content["result"]) == 2


async def test_update_refused_without_confirm_reports_no_progress(mocked_client):
    events, cb = _recorder()
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await docker.do_update_containers(client, ["1:a"], confirm=False, progress=cb)
        assert route.call_count == 0
    assert events == []


# ── Helpers ───────────────────────────────────────────────────────────────────


async def test_with_heartbeat_swallows_callback_errors_and_cancels_task():
    async def boom(*_a):
        raise RuntimeError("nope")

    async def work():
        await asyncio.sleep(0.12)
        return "ok"

    assert await with_heartbeat(work(), boom, interval_s=0.03) == "ok"


async def test_with_heartbeat_without_callback_just_awaits():
    async def work():
        return 7

    assert await with_heartbeat(work(), None, interval_s=0.01) == 7
