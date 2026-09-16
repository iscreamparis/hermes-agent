"""A kanban heartbeat must mean the AGENT progressed, not that a thread is alive.

Regression: a worker wedged inside a single tool call kept heartbeating every
60s, because the tool-activity keepalive thread stamps ``_touch_activity`` while
the tool is in flight and the auto-heartbeat bridge trusted that alone. Observed
live: 28 minutes with zero model round-trips while the board looked healthy, so
the dispatcher's stale check (which reads ``last_heartbeat_at``) could never
reclaim it.

The bridge now additionally requires the progress counter to have CHANGED since
the previous heartbeat. ``note_agent_progress()`` is called on completed model
round-trips and completed tool calls only.
"""
from __future__ import annotations

import os

import pytest

from tools import kanban_tools


@pytest.fixture
def worker_env(monkeypatch):
    """Pretend to be a dispatcher-spawned worker, with the DB write stubbed out."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    calls: list[int] = []

    class _FakeKb:
        """Minimal stand-in: every bridge op is a no-op that we simply count."""

        @staticmethod
        def heartbeat_claim(conn, tid, **kw):
            return True

    class _FakeBoard:
        def __enter__(self):
            calls.append(1)
            return (_FakeKb(), object())

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(kanban_tools, "_board", lambda *a, **k: _FakeBoard())
    # The bridge also reaches for the dispatcher module; neutralise its write.
    import hermes_cli.kanban_db_dispatch as _kbd
    monkeypatch.setattr(_kbd, "heartbeat_worker", lambda *a, **k: True)
    monkeypatch.setattr(kanban_tools, "_worker_run_id", lambda tid: None)
    # Fresh module state per test.
    monkeypatch.setattr(kanban_tools, "_auto_heartbeat_last_attempt", 0.0)
    monkeypatch.setattr(kanban_tools, "_auto_heartbeat_last_progress", -1)
    monkeypatch.setattr(kanban_tools, "_agent_progress_counter", 0)
    monkeypatch.setattr(kanban_tools, "_AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS", 0.0)
    return calls


def test_heartbeat_requires_progress_since_last_beat(worker_env):
    """The wedged-worker case: no progress -> no heartbeat, forever."""
    kanban_tools.note_agent_progress()
    assert kanban_tools.heartbeat_current_worker_from_env() is True
    assert len(worker_env) == 1

    # Keepalive thread ticks, but nothing real happened: must NOT heartbeat.
    for _ in range(10):
        assert kanban_tools.heartbeat_current_worker_from_env() is False
    assert len(worker_env) == 1, "a wedged worker must stop refreshing the board"


def test_progress_resumes_heartbeat(worker_env):
    """A slow-but-working worker keeps beating across real progress."""
    for _ in range(3):
        kanban_tools.note_agent_progress()
        assert kanban_tools.heartbeat_current_worker_from_env() is True
    assert len(worker_env) == 3


def test_no_heartbeat_outside_worker_context(monkeypatch, worker_env):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kanban_tools.note_agent_progress()
    assert kanban_tools.heartbeat_current_worker_from_env() is False


def test_rate_limit_still_applies(monkeypatch, worker_env):
    """Progress alone must not defeat the once-per-interval write limit."""
    monkeypatch.setattr(kanban_tools, "_AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS", 3600.0)
    kanban_tools.note_agent_progress()
    assert kanban_tools.heartbeat_current_worker_from_env() is True
    kanban_tools.note_agent_progress()
    assert kanban_tools.heartbeat_current_worker_from_env() is False


def test_note_agent_progress_is_monotonic():
    before = kanban_tools._agent_progress_counter
    kanban_tools.note_agent_progress()
    kanban_tools.note_agent_progress()
    assert kanban_tools._agent_progress_counter == before + 2
