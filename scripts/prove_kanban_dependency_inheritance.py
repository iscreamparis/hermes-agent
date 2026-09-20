#!/usr/bin/env python
"""Prove decomposed Kanban graphs inherit external prerequisites.

The harness creates a disposable HERMES_HOME and exercises only the real
``hermes_cli.kanban_db`` mutation APIs. It never opens the configured board.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _status(kb: Any, conn: Any, task_id: str) -> str:
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task.status


def _snapshot(kb: Any, conn: Any, task_ids: dict[str, str]) -> dict[str, dict[str, Any]]:
    id_to_label = {task_id: label for label, task_id in task_ids.items()}
    return {
        label: {
            "id": task_id,
            "status": _status(kb, conn, task_id),
            "parents": [
                {"label": id_to_label.get(parent_id, "external"), "id": parent_id}
                for parent_id in kb.parent_ids(conn, task_id)
            ],
            "children": [
                {"label": id_to_label.get(child_id, "external"), "id": child_id}
                for child_id in kb.child_ids(conn, task_id)
            ],
        }
        for label, task_id in task_ids.items()
    }


def _links(snapshot: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for child_label, task in snapshot.items():
        for parent in task["parents"]:
            links.append(
                {
                    "parent_label": parent["label"],
                    "parent_id": parent["id"],
                    "child_label": child_label,
                    "child_id": task["id"],
                }
            )
    return sorted(links, key=lambda item: (item["parent_label"], item["child_label"]))


def _complete_claimed(kb: Any, conn: Any, task_id: str) -> None:
    assert kb.complete_task(conn, task_id, summary="smoke proof completion")


def _run_primary(kb: Any, conn: Any) -> dict[str, Any]:
    prerequisite = kb.create_task(conn, title="P: external prerequisite", assignee="operator")
    root = kb.create_task(
        conn,
        title="R: coarse roadmap card",
        assignee="orchestrator",
        parents=[prerequisite],
        triage=True,
    )
    downstream = kb.create_task(
        conn,
        title="downstream release",
        assignee="release-worker",
        parents=[root],
    )
    children = kb.decompose_triage_task(
        conn,
        root,
        root_assignee="orchestrator",
        children=[
            {"title": "parallel entry A", "assignee": "worker-a", "parents": []},
            {"title": "parallel entry B", "assignee": "worker-b", "parents": []},
            {"title": "dependent child", "assignee": "worker-c", "parents": [0, 1]},
        ],
        author="smoke-harness",
    )
    assert children is not None
    entry_a, entry_b, dependent = children
    ids = {
        "P": prerequisite,
        "R": root,
        "downstream": downstream,
        "entry_a": entry_a,
        "entry_b": entry_b,
        "dependent": dependent,
    }

    initial = _snapshot(kb, conn, ids)
    assert {parent["id"] for parent in initial["entry_a"]["parents"]} == {prerequisite}
    assert {parent["id"] for parent in initial["entry_b"]["parents"]} == {prerequisite}
    assert {parent["id"] for parent in initial["dependent"]["parents"]} == {entry_a, entry_b}
    assert initial["entry_a"]["status"] == initial["entry_b"]["status"] == "todo"

    blocked_claims = {
        "entry_a": kb.claim_task(conn, entry_a, claimer="smoke-before-p-a") is None,
        "entry_b": kb.claim_task(conn, entry_b, claimer="smoke-before-p-b") is None,
    }
    assert all(blocked_claims.values())
    before_prerequisite_completion = _snapshot(kb, conn, ids)

    assert kb.complete_task(conn, prerequisite, summary="planning hold satisfied")
    after_prerequisite_completion = _snapshot(kb, conn, ids)
    assert after_prerequisite_completion["entry_a"]["status"] == "ready"
    assert after_prerequisite_completion["entry_b"]["status"] == "ready"
    assert after_prerequisite_completion["dependent"]["status"] == "todo"

    successful_claims = {
        "entry_a": kb.claim_task(conn, entry_a, claimer="smoke-after-p-a") is not None,
        "entry_b": kb.claim_task(conn, entry_b, claimer="smoke-after-p-b") is not None,
    }
    assert all(successful_claims.values())
    _complete_claimed(kb, conn, entry_a)
    after_entry_a = _snapshot(kb, conn, ids)
    assert after_entry_a["dependent"]["status"] == "todo"
    _complete_claimed(kb, conn, entry_b)
    after_parallel_entries = _snapshot(kb, conn, ids)
    assert after_parallel_entries["dependent"]["status"] == "ready"

    successful_claims["dependent"] = kb.claim_task(
        conn, dependent, claimer="smoke-dependent"
    ) is not None
    assert successful_claims["dependent"]
    _complete_claimed(kb, conn, dependent)
    root_ready = _snapshot(kb, conn, ids)
    assert root_ready["R"]["status"] == "ready"
    assert root_ready["downstream"]["status"] == "todo"

    successful_claims["R"] = kb.claim_task(conn, root, claimer="smoke-root") is not None
    assert successful_claims["R"]
    _complete_claimed(kb, conn, root)
    downstream_ready = _snapshot(kb, conn, ids)
    assert downstream_ready["downstream"]["status"] == "ready"

    successful_claims["downstream"] = kb.claim_task(
        conn, downstream, claimer="smoke-downstream"
    ) is not None
    assert successful_claims["downstream"]
    _complete_claimed(kb, conn, downstream)
    final = _snapshot(kb, conn, ids)
    assert all(task["status"] == "done" for task in final.values())

    return {
        "ids": ids,
        "links_after_decompose": _links(initial),
        "blocked_claims_before_P_completed": blocked_claims,
        "successful_claims_after_P_completed": successful_claims,
        "snapshots": {
            "after_decompose": initial,
            "after_blocked_claim_attempts": before_prerequisite_completion,
            "after_P_completed": after_prerequisite_completion,
            "after_entry_a_completed": after_entry_a,
            "after_parallel_entries_completed": after_parallel_entries,
            "root_ready": root_ready,
            "downstream_ready": downstream_ready,
            "final": final,
        },
    }


def _run_nested(kb: Any, conn: Any) -> dict[str, Any]:
    prerequisite = kb.create_task(conn, title="nested prerequisite", assignee="operator")
    outer = kb.create_task(
        conn,
        title="outer coarse card",
        assignee="orchestrator",
        parents=[prerequisite],
        triage=True,
    )
    inner = kb.create_task(
        conn,
        title="inner coarse card",
        assignee="planner",
        parents=[outer],
        triage=True,
    )

    inner_children = kb.decompose_triage_task(
        conn,
        inner,
        root_assignee="planner",
        children=[
            {"title": "inner entry", "assignee": "worker", "parents": []},
            {"title": "inner dependent", "assignee": "worker", "parents": [0]},
        ],
        author="smoke-harness",
    )
    outer_children = kb.decompose_triage_task(
        conn,
        outer,
        root_assignee="orchestrator",
        children=[{"title": "outer entry", "assignee": "planner", "parents": []}],
        author="smoke-harness",
    )
    assert inner_children is not None and outer_children is not None
    inner_entry, inner_dependent = inner_children
    outer_entry = outer_children[0]
    ids = {
        "nested_P": prerequisite,
        "outer_root": outer,
        "inner_root": inner,
        "outer_entry": outer_entry,
        "inner_entry": inner_entry,
        "inner_dependent": inner_dependent,
    }

    initial = _snapshot(kb, conn, ids)
    assert kb.parent_ids(conn, outer_entry) == [prerequisite]
    assert kb.parent_ids(conn, inner_entry) == [outer]
    assert kb.parent_ids(conn, inner_dependent) == [inner_entry]
    assert kb.claim_task(conn, outer_entry, claimer="nested-before-p") is None
    assert kb.claim_task(conn, inner_entry, claimer="nested-before-outer") is None

    assert kb.complete_task(conn, prerequisite, summary="nested hold satisfied")
    after_prerequisite = _snapshot(kb, conn, ids)
    assert after_prerequisite["outer_entry"]["status"] == "ready"
    assert after_prerequisite["inner_entry"]["status"] == "todo"

    assert kb.claim_task(conn, outer_entry, claimer="nested-outer-entry") is not None
    _complete_claimed(kb, conn, outer_entry)
    outer_ready = _snapshot(kb, conn, ids)
    assert outer_ready["outer_root"]["status"] == "ready"
    assert outer_ready["inner_entry"]["status"] == "todo"

    assert kb.claim_task(conn, outer, claimer="nested-outer-root") is not None
    _complete_claimed(kb, conn, outer)
    inner_entry_ready = _snapshot(kb, conn, ids)
    assert inner_entry_ready["inner_entry"]["status"] == "ready"

    return {
        "ids": ids,
        "links_after_nested_decompose": _links(initial),
        "blocked_claims_before_prerequisites": {
            "outer_entry": True,
            "inner_entry": True,
        },
        "snapshots": {
            "after_nested_decompose": initial,
            "after_nested_P_completed": after_prerequisite,
            "outer_root_ready": outer_ready,
            "inner_entry_ready": inner_entry_ready,
        },
    }


def _proof_text(proof: dict[str, Any]) -> str:
    primary = proof["primary"]
    nested = proof["nested"]
    final_statuses = {
        label: task["status"] for label, task in primary["snapshots"]["final"].items()
    }
    nested_ready = nested["snapshots"]["inner_entry_ready"]["inner_entry"]["status"]
    return "\n".join(
        [
            "Kanban dependency inheritance smoke proof: PASS",
            "",
            f"Isolated temporary HERMES_HOME: {proof['isolation']['temporary_hermes_home']}",
            "The temporary directory was removed after the run; no configured board was opened.",
            f"Primary real links: {len(primary['links_after_decompose'])}",
            f"Claims blocked before P completed: {primary['blocked_claims_before_P_completed']}",
            f"Claims successful after P completed: {primary['successful_claims_after_P_completed']}",
            f"Primary final statuses: {final_statuses}",
            f"Nested inner entry after outer prerequisite chain completed: {nested_ready}",
            "",
            "Full task ids, links, and status snapshots: proof.json",
        ]
    ) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/kanban-dependency-inheritance"),
    )
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    previous_home = os.environ.get("HERMES_HOME")
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-kanban-dependency-proof-") as temp_dir:
            isolated_home = Path(temp_dir) / ".hermes"
            isolated_home.mkdir()
            os.environ["HERMES_HOME"] = str(isolated_home)

            from hermes_cli import kanban_db as kb
            from hermes_cli import kanban_db_connect as kbc

            kb.init_db()
            with kbc.connect() as conn:
                proof = {
                    "result": "PASS",
                    "isolation": {
                        "temporary_hermes_home": str(isolated_home),
                        "configured_board_touched": False,
                        "temporary_home_removed_after_run": True,
                    },
                    "primary": _run_primary(kb, conn),
                    "nested": _run_nested(kb, conn),
                }
    finally:
        if previous_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous_home

    json_path = output_dir / "proof.json"
    text_path = output_dir / "proof.txt"
    json_path.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text_path.write_text(_proof_text(proof), encoding="utf-8")
    print(f"PASS: {json_path}")
    print(f"PASS: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
