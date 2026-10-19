"""A delegate_task child must never wake a duplicate of itself through a background completion.

Incident 2026-10-19: an iPad-polish child (delegate_task, background) ran
``terminal(background=True, notify_on_complete=True)``. The child inherited the
parent's Discord routing, so the completion woke a NEW gateway turn in the
parent's thread; the async-delegation resolver then pinned that turn to the
child's session id, and a second copy of the still-running child worked the same
goal in the same git worktree for an hour.

Two layers, both covered here:
1. ``async_delivery_supported()`` is False inside a delegated child, so the
   terminal tool refuses notify_on_complete there (the child polls instead).
2. The gateway resolver drops a completion pinned to a LIVE child's session id.
"""

import asyncio
import types

import pytest


def test_async_delivery_refused_inside_delegated_child():
    from agent.delegation_context import delegated_child_context
    from gateway.session_context import async_delivery_supported

    assert async_delivery_supported() is True
    with delegated_child_context("child_session_x"):
        assert async_delivery_supported() is False
    # restored for the parent
    assert async_delivery_supported() is True


def test_live_subagent_session_lookup():
    import tools.delegate_tool as dt

    agent = types.SimpleNamespace(session_id="child_live_1")
    dt._register_subagent({"subagent_id": "sa-test-live", "agent": agent})
    try:
        assert dt.is_live_subagent_session("child_live_1") is True
        assert dt.is_live_subagent_session("someone_else") is False
        assert dt.is_live_subagent_session("") is False
    finally:
        dt._unregister_subagent("sa-test-live")
    assert dt.is_live_subagent_session("child_live_1") is False


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows

    async def get_session(self, sid):
        return self.rows.get(sid)

    async def get_compression_tip(self, sid):
        return sid


def _runner_with(rows):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._session_db = _FakeDB(rows)
    switched = []

    class _Store:
        async def switch_session(self, key, sid):
            switched.append((key, sid))
            return types.SimpleNamespace(session_key=key, session_id=sid)

        async def advance_compression_session(self, key, prior, sid):
            switched.append((key, sid))
            return types.SimpleNamespace(session_key=key, session_id=sid)

    # async_session_store is a property over session_store; seed its cache.
    store = _Store()
    runner.session_store = store
    store._store = store
    runner._async_session_store = store
    return runner, switched


def test_resolver_drops_completion_pinned_to_live_child():
    import tools.delegate_tool as dt

    child_sid = "20261019_child"
    rows = {child_sid: {"id": child_sid, "ended_at": None, "end_reason": None}}
    runner, switched = _runner_with(rows)
    entry = types.SimpleNamespace(session_key="agent:main:discord:thread:1:1", session_id="20261010_parent")

    agent = types.SimpleNamespace(session_id=child_sid)
    dt._register_subagent({"subagent_id": "sa-test-resolver", "agent": agent})
    try:
        result = asyncio.run(runner._resolve_async_delegation_session(entry, child_sid))
    finally:
        dt._unregister_subagent("sa-test-resolver")
    assert result is None, "a live child's session must never be woken as a gateway turn"
    assert switched == [], "the chat route must not be switched to the child"


def test_resolver_still_switches_to_a_normal_owning_session():
    owner = "20261019_owner"
    rows = {owner: {"id": owner, "ended_at": None, "end_reason": None}}
    runner, switched = _runner_with(rows)
    entry = types.SimpleNamespace(session_key="agent:main:discord:thread:1:1", session_id="20261010_parent")
    result = asyncio.run(runner._resolve_async_delegation_session(entry, owner))
    assert result is not None and result.session_id == owner
    assert switched == [("agent:main:discord:thread:1:1", owner)]
