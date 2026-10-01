"""Regression: the mid-turn "adopt external roll" branch must not hot-loop.

2026-10-01: the fulcrum and amplifi Slack bots each wedged for ~1 hour logging
"adopted external roll mid-turn: a0281a -> 328459" ~80x/sec. Causes:
  * the branch ran for non-credential errors (status_code=None,
    ModuleNotFoundError after a Homebrew Python upgrade);
  * it compared against pool.current(), which _swap_credential() never moved,
    so the same pair was adopted on every retry with no budget.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.agent_runtime_helpers import recover_with_credential_pool
from agent.error_classifier import FailoverReason


def _setup(current_id="a0281a", disk_id="328459"):
    agent = MagicMock()
    agent.provider = "anthropic"
    agent.api_key = "tok-" + current_id
    agent._credential_pool_entry_id = current_id
    # MagicMock would auto-create this attr; start it absent-ish.
    agent._external_roll_adopt_counts = None

    cur = SimpleNamespace(id=current_id, runtime_api_key="tok-" + current_id,
                          last_status="ok")
    disk_entry = SimpleNamespace(id=disk_id, runtime_api_key="tok-" + disk_id,
                                 last_status="ok")

    pool = MagicMock()
    pool.provider = "anthropic"
    pool.current.return_value = cur
    pool._entries = [cur, disk_entry]
    import threading
    pool._lock = threading.Lock()
    pool._current_id = current_id
    agent._credential_pool = pool

    disk_pool = MagicMock()
    disk_pool.select.return_value = disk_entry
    return agent, pool, disk_pool


def test_non_credential_error_never_adopts():
    agent, pool, disk_pool = _setup()
    with patch("agent.credential_pool.load_pool", return_value=disk_pool) as lp:
        recovered, _ = recover_with_credential_pool(
            agent, status_code=None, has_retried_429=False, classified_reason=None,
        )
    assert recovered is False
    lp.assert_not_called()
    agent._swap_credential.assert_not_called()


def test_rate_limit_adopts_once_then_stops_repeating_same_pair():
    agent, pool, disk_pool = _setup()
    with patch("agent.credential_pool.load_pool", return_value=disk_pool):
        first, _ = recover_with_credential_pool(
            agent, status_code=429, has_retried_429=False,
            classified_reason=FailoverReason.rate_limit,
        )
        assert first is True
        assert agent._swap_credential.call_count == 1
        # pool cursor follows the swap
        assert pool._current_id == "328459"
        # Simulate the agent now being on the adopted entry.
        agent._credential_pool_entry_id = "328459"
        results = [
            recover_with_credential_pool(
                agent, status_code=429, has_retried_429=True,
                classified_reason=FailoverReason.rate_limit,
            )
            for _ in range(50)
        ]
    # Never re-adopts the entry it is already on.
    assert agent._swap_credential.call_count <= 1 + sum(1 for r in results if r[0])
    adopt_calls = [c for c in agent._swap_credential.call_args_list
                   if getattr(c.args[0], "id", None) == "328459"]
    assert len(adopt_calls) == 1


def test_adopt_budget_caps_even_if_ids_never_settle():
    """Even if attribution is stale (in-use id never updates), adoption of the
    same target is capped so the retry loop can fall through to the normal
    budgeted recovery path."""
    agent, pool, disk_pool = _setup()
    with patch("agent.credential_pool.load_pool", return_value=disk_pool):
        for _ in range(50):
            agent._credential_pool_entry_id = "a0281a"  # stale, never moves
            recover_with_credential_pool(
                agent, status_code=429, has_retried_429=True,
                classified_reason=FailoverReason.rate_limit,
            )
    adopt_calls = [c for c in agent._swap_credential.call_args_list
                   if getattr(c.args[0], "id", None) == "328459"
                   and c is not None]
    # 2 adoptions max for this target; anything else came from normal rotation.
    assert len([1 for _ in adopt_calls]) <= 2 + pool.mark_exhausted_and_rotate.call_count
