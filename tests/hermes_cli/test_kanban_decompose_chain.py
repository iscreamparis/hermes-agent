"""Decomposed children must form a STRICT SERIAL CHAIN, never parallel siblings.

House rule: one triage idea fans out into dependent tasks only. Children of a
single idea touch the same files, so two workers dispatched at once collide and
overwrite each other. The prompt asks for a chain; ``_chainify`` guarantees it
regardless of what the model actually returns.
"""
from __future__ import annotations

import pytest

from hermes_cli.kanban_decompose import _chainify


def _titles(children: list[dict]) -> list[str]:
    return [c["title"] for c in children]


def _assert_strict_chain(children: list[dict]) -> None:
    assert children[0]["parents"] == []
    for idx in range(1, len(children)):
        assert children[idx]["parents"] == [idx - 1], f"child {idx} is not chained: {children}"


def test_all_parallel_children_are_serialised():
    """The real regression: a decompose that emitted 3 rootless children."""
    out = _chainify([
        {"title": "investigate", "parents": []},
        {"title": "create timeline", "parents": []},
        {"title": "render CUT", "parents": []},
        {"title": "tests", "parents": [2]},
    ], "t_test")
    _assert_strict_chain(out)
    assert _titles(out) == ["investigate", "create timeline", "render CUT", "tests"]


def test_declared_dependencies_become_the_chain_order():
    """A model that emits a valid DAG out of order still gets a sane sequence."""
    out = _chainify([
        {"title": "tests", "parents": [2]},
        {"title": "investigate", "parents": []},
        {"title": "build", "parents": [1]},
    ], "t_test")
    _assert_strict_chain(out)
    assert _titles(out) == ["investigate", "build", "tests"]


def test_diamond_is_linearised_without_losing_children():
    out = _chainify([
        {"title": "A", "parents": []},
        {"title": "B", "parents": [0]},
        {"title": "C", "parents": [0]},
        {"title": "D", "parents": [1, 2]},
    ], "t_test")
    _assert_strict_chain(out)
    assert _titles(out) == ["A", "B", "C", "D"]


def test_cycle_does_not_hang_or_drop_children():
    out = _chainify([
        {"title": "X", "parents": [1]},
        {"title": "Y", "parents": [0]},
    ], "t_test")
    _assert_strict_chain(out)
    assert sorted(_titles(out)) == ["X", "Y"]


@pytest.mark.parametrize("children", [[], [{"title": "solo", "parents": []}]])
def test_degenerate_inputs_pass_through(children):
    out = _chainify([dict(c) for c in children], "t_test")
    assert len(out) == len(children)


def test_other_child_fields_survive_reordering():
    out = _chainify([
        {"title": "second", "parents": [1], "assignee": "r2-opus-med", "body": "B"},
        {"title": "first", "parents": [], "assignee": "a-sol-med", "body": "A"},
    ], "t_test")
    assert _titles(out) == ["first", "second"]
    assert [c["assignee"] for c in out] == ["a-sol-med", "r2-opus-med"]
    assert [c["body"] for c in out] == ["A", "B"]
