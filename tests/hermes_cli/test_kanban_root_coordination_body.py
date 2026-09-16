"""A decompose root must wake as a COORDINATION task, never as an open bug report.

Regression: ``decompose_triage_task`` set the root's status and assignee but never
its body. A card filed as a one-line title therefore woke on the orchestrator with
no statement that its children had already shipped the fix — so the model
re-implemented the work. Observed live on a real board.

The root now gets an explicit verify-don't-rebuild brief, and only when its body is
empty: a user-written brief is never overwritten. LLM-free by design.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_graph import _ROOT_COORDINATION_BODY, decompose_triage_task


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


CHILDREN = [
    {"title": "investigate", "body": "look", "assignee": "a-sol-med", "parents": []},
    {"title": "implement", "body": "build", "assignee": "r1-luna-max", "parents": [0]},
]


def _decompose(conn, body=None):
    tid = kb.create_task(
        conn, title="Projects are empty and the tab shows nothing", body=body, triage=True,
    )
    child_ids = decompose_triage_task(
        conn, tid, root_assignee="r2-opus-med", children=[dict(c) for c in CHILDREN],
    )
    assert child_ids and len(child_ids) == 2
    return tid


def test_empty_root_body_is_filled_with_the_coordination_brief(kanban_home):
    with kbc.connect() as conn:
        tid = _decompose(conn, body=None)
        body = kb.get_task(conn, tid).body or ""
    assert body, "root woke with an empty body — the model will re-implement the children's work"
    assert "coordination task" in body.lower()
    assert "do not re-implement" in body.lower()
    assert "2 child tasks" in body


def test_blank_whitespace_body_counts_as_empty(kanban_home):
    with kbc.connect() as conn:
        tid = _decompose(conn, body="   \n  ")
        body = kb.get_task(conn, tid).body or ""
    assert "coordination task" in body.lower()


def test_user_written_root_body_is_never_overwritten(kanban_home):
    brief = "Keep the existing API shape. Ship behind a flag."
    with kbc.connect() as conn:
        tid = _decompose(conn, body=brief)
        assert kb.get_task(conn, tid).body == brief


def test_root_still_moves_to_todo_with_the_orchestrator(kanban_home):
    with kbc.connect() as conn:
        tid = _decompose(conn)
        task = kb.get_task(conn, tid)
    assert task.status == "todo"
    assert task.assignee == "r2-opus-med"


def test_brief_template_states_both_halves_of_the_rule():
    text = _ROOT_COORDINATION_BODY.format(n=3)
    assert "verify" in text.lower()
    assert "not to build it again" in text.lower()
    assert "3 child tasks" in text
