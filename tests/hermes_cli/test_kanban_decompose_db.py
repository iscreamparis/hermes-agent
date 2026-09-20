"""Tests for kb.decompose_triage_task — the DB-layer atomic fan-out
from the triage column. LLM-free by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_triage(conn, title="rough idea", body=None, assignee=None, tenant=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        assignee=assignee,
        tenant=tenant,
        triage=True,
    )


def test_decompose_creates_children_and_promotes_root(kanban_home):
    with kbc.connect() as conn:
        tid = _create_triage(conn, title="ship a feature")
        assert kb.get_task(conn, tid).status == "triage"

    children = [
        {"title": "research", "body": "look at prior art", "assignee": "researcher", "parents": []},
        {"title": "build it", "body": "write code", "assignee": "engineer", "parents": [0]},
    ]
    with kbc.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=children,
            author="decomposer",
        )
    assert child_ids is not None
    assert len(child_ids) == 2

    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, child_ids[0])
        c1 = kb.get_task(conn, child_ids[1])

    # Root flipped to todo with orchestrator assignee, gated by children.
    assert root.status == "todo"
    assert root.assignee == "orchestrator"
    # First child has no internal parents → ready on recompute_ready.
    assert c0.status == "ready"
    assert c0.assignee == "researcher"
    # Second child has parents=[0] → stays in todo until c0 completes.
    assert c1.status == "todo"
    assert c1.assignee == "engineer"


def test_decompose_records_audit_comment_and_event(kanban_home):
    with kbc.connect() as conn:
        tid = _create_triage(conn)
        child_ids = kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orch",
            children=[{"title": "task A", "assignee": "researcher"}],
            author="alice",
        )
    assert child_ids is not None

    with kbc.connect() as conn:
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)

    assert any("Decomposed into" in (c.body or "") for c in comments)
    assert any(ev.kind == "decomposed" for ev in events)


def test_decompose_preserves_external_prerequisites_across_the_workgraph(kanban_home):
    with kbc.connect() as conn:
        prerequisite_a = kb.create_task(conn, title="planning approval A")
        prerequisite_b = kb.create_task(conn, title="planning approval B")
        root = _create_triage(conn, title="coarse roadmap card")
        kb.link_tasks(conn, prerequisite_a, root)
        kb.link_tasks(conn, prerequisite_b, root)
        downstream = kb.create_task(conn, title="release", parents=[root])

        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "parallel entry A", "assignee": "worker-a", "parents": []},
                {"title": "parallel entry B", "assignee": "worker-b", "parents": []},
                {"title": "dependent", "assignee": "worker-c", "parents": [0, 1]},
            ],
        )
        assert child_ids is not None
        entry_a, entry_b, dependent = child_ids

        assert set(kb.parent_ids(conn, root)) == {
            prerequisite_a,
            prerequisite_b,
            entry_a,
            entry_b,
            dependent,
        }
        assert set(kb.parent_ids(conn, entry_a)) == {prerequisite_a, prerequisite_b}
        assert set(kb.parent_ids(conn, entry_b)) == {prerequisite_a, prerequisite_b}
        assert set(kb.parent_ids(conn, dependent)) == {entry_a, entry_b}
        assert kb.parent_ids(conn, downstream) == [root]
        assert all(kb.get_task(conn, task_id).status == "todo" for task_id in child_ids)

        # Even a stale writer forcing an entry to ready cannot bypass the planning hold.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (entry_a,))
        assert kb.claim_task(conn, entry_a, claimer="test-worker") is None
        assert kb.get_task(conn, entry_a).status == "todo"

        assert kb.complete_task(conn, prerequisite_a)
        assert kb.get_task(conn, entry_a).status == "todo"
        assert kb.complete_task(conn, prerequisite_b)
        assert kb.get_task(conn, entry_a).status == "ready"
        assert kb.get_task(conn, entry_b).status == "ready"

        assert kb.claim_task(conn, entry_a, claimer="worker-a") is not None
        assert kb.complete_task(conn, entry_a)
        assert kb.claim_task(conn, entry_b, claimer="worker-b") is not None
        assert kb.complete_task(conn, entry_b)
        assert kb.get_task(conn, dependent).status == "ready"
        assert kb.claim_task(conn, dependent, claimer="worker-c") is not None
        assert kb.complete_task(conn, dependent)
        assert kb.get_task(conn, root).status == "ready"
        assert kb.complete_task(conn, root)
        assert kb.get_task(conn, downstream).status == "ready"

        # Reopening an external prerequisite retracts every result that depended on it.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'todo', completed_at = NULL WHERE id = ?",
                (prerequisite_a,),
            )
        invalidated = kb.invalidate_descendants_for_parent_reopen(
            conn, prerequisite_a, author="operator",
        )
        assert {entry["id"] for entry in invalidated["invalidated"]} == {
            entry_a,
            entry_b,
            dependent,
            root,
            downstream,
        }
        assert kb.get_task(conn, downstream).status == "todo"


def test_nested_decompose_preserves_transitive_external_prerequisites(kanban_home):
    with kbc.connect() as conn:
        prerequisite = kb.create_task(conn, title="programme approval")
        outer = _create_triage(conn, title="programme")
        kb.link_tasks(conn, prerequisite, outer)
        inner = _create_triage(conn, title="coarse workstream")
        kb.link_tasks(conn, outer, inner)

        nested = kb.decompose_triage_task(
            conn,
            inner,
            root_assignee="planner",
            children=[
                {"title": "nested entry", "assignee": "worker", "parents": []},
                {"title": "nested dependent", "assignee": "worker", "parents": [0]},
            ],
        )
        assert nested is not None
        nested_entry, nested_dependent = nested

        first_level = kb.decompose_triage_task(
            conn,
            outer,
            root_assignee="orchestrator",
            children=[{"title": "programme entry", "assignee": "planner", "parents": []}],
        )
        assert first_level is not None
        programme_entry = first_level[0]

        assert kb.parent_ids(conn, programme_entry) == [prerequisite]
        assert kb.parent_ids(conn, nested_entry) == [outer]
        assert kb.parent_ids(conn, nested_dependent) == [nested_entry]
        assert set(kb.parent_ids(conn, outer)) == {prerequisite, programme_entry}
        assert set(kb.parent_ids(conn, inner)) == {
            outer,
            nested_entry,
            nested_dependent,
        }
        assert kb.get_task(conn, programme_entry).status == "todo"
        assert kb.get_task(conn, nested_entry).status == "todo"

        assert kb.complete_task(conn, prerequisite)
        assert kb.get_task(conn, programme_entry).status == "ready"
        assert kb.get_task(conn, nested_entry).status == "todo"
        assert kb.complete_task(conn, programme_entry)
        assert kb.get_task(conn, outer).status == "ready"
        assert kb.complete_task(conn, outer)
        assert kb.get_task(conn, nested_entry).status == "ready"


def test_decompose_without_external_prerequisites_keeps_entry_parent_free(kanban_home):
    with kbc.connect() as conn:
        root = _create_triage(conn)
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {"title": "entry", "assignee": "worker", "parents": []},
                {"title": "dependent", "assignee": "worker", "parents": [0]},
            ],
        )
        assert child_ids is not None
        entry, dependent = child_ids

        assert kb.parent_ids(conn, entry) == []
        assert kb.parent_ids(conn, dependent) == [entry]
        assert kb.get_task(conn, entry).status == "ready"
        assert kb.get_task(conn, dependent).status == "todo"


def test_decompose_auto_promote_false_keeps_inherited_entries_in_todo(kanban_home):
    with kbc.connect() as conn:
        prerequisite = kb.create_task(conn, title="completed approval")
        assert kb.complete_task(conn, prerequisite)
        root = _create_triage(conn)
        kb.link_tasks(conn, prerequisite, root)

        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[{"title": "manual-review entry", "assignee": "worker", "parents": []}],
            auto_promote=False,
        )
        assert child_ids is not None
        entry = child_ids[0]

        assert kb.parent_ids(conn, entry) == [prerequisite]
        assert kb.get_task(conn, entry).status == "todo"
        assert kb.claim_task(conn, entry, claimer="worker") is None

        kb.recompute_ready(conn)
        assert kb.get_task(conn, entry).status == "ready"
