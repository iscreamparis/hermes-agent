# Kanban dependency inheritance on decomposed child tasks — fix report

Task: t_f3c40a3e — "Fix decomposer to preserve external prerequisites on child tasks"
Date: 2026-09-20 (UTC+2) · Worker: o-ds-flash · Repo HEAD at dispatch: 4f6ef8806f

## Summary

`hermes_cli/kanban_db.py -> decompose_triage_task` (line 3525) is the shared core mutator
behind the CLI (`hermes kanban decompose`), the dashboard Decompose button, and the
gateway auto-decompose tick. Before this fix, decomposing a triage card that already had
external prerequisite links (e.g. a coarse roadmap card waiting on planning approvals)
dropped those holds: the generated children had no link to the prerequisites, so children
became claimable while the root's prerequisites were still unsatisfied (and children could
finish before the holds cleared, letting the root — and everything downstream — proceed
early).

The fix: inside the single fan-out transaction, snapshot the root's pre-existing external
parent ids BEFORE any child is created, then link those prerequisites onto the graph-entry
children (children with no sibling parents). Root external edges, downstream dependents,
and sibling topology are untouched; no child is ever treated as an external parent and no
self-links/cycles are possible (entries are brand-new leaves at the moment they are linked).

## Implementation (narrow, additive)

`hermes_cli/kanban_db.py` (+24 lines, 2 hunks inside `decompose_triage_task`):

1. Before `_insert_decomposed_child` runs:
   `SELECT parent_id FROM task_links WHERE child_id = ?` (the root's current parents) —
   this is the atomic snapshot, taken inside the same `write_txn` as the fan-out.
2. After sibling edges are created, for each child with no `parents` entry:
   `_link(conn, external_parent_id, child_id)` + a `linked` event carrying
   `{inherited_from: task_id}` for auditability.
3. Existing behavior unchanged: root is linked under EVERY child (root waits for the whole
   graph), root flips triage -> todo, `recompute_ready` runs outside the txn when
   `auto_promote=True`.

### Why graph-entry children only (documented justification)

Descendants are gated transitively by the sibling topology. In `entryA, entryB ->
dependent`, linking P to the dependent as well would add a redundant direct edge
(P -> dependent) on top of P -> entryA/B -> dependent — a clique that keeps the dependent
in `todo` until P completes (correct but redundant) while adding O(n_children) extra links
and audit events per decompose. Linking only entries is sufficient and minimal: a child
with siblings-parents cannot become claimable before its parents (which chain back to an
entry) complete, so P's hold propagates to every descendant through the DAG edges the
decomposer already creates. Tested assertion (no cliques):
`parent_ids(dependent) == {entry_a, entry_b}` and `parent_ids(entry_a) == {P_a, P_b}`.

## Regression tests (added before the fix, red first)

File: `tests/hermes_cli/test_kanban_decompose_db.py` (6 tests, LLM-free, isolated temp
`HERMES_HOME` via the standard `kanban_home` fixture):

- `test_decompose_creates_children_and_promotes_root` — pre-existing behavior guard.
- `test_decompose_records_audit_comment_and_event` — audit guard.
- `test_decompose_preserves_external_prerequisites_across_the_workgraph` — P_a/P_b -> R ->
  downstream; parallel entries + dependent; claims blocked before P completes; readiness
  after; correct root/downstream progression; reopen of an external prerequisite
  invalidates the full chain.
- `test_nested_decompose_preserves_transitive_external_prerequisites` — nested split:
  inner decomposition while outer still open; outer keeps P; inner entry gated transitively.
- `test_decompose_without_external_prerequisites_keeps_entry_parent_free` — no-parent root.
- `test_decompose_auto_promote_false_keeps_inherited_entries_in_todo` — planning hold +
  manual review; claim blocked; `recompute_ready` promotes only after P done.

### Red evidence (base code)

- Original capture (run 10): `artifacts/kanban-dependency-inheritance/red-test-output.txt`
  — 3 failed / 3 passed: exactly the three dependency-loss tests, with
  `AssertionError: assert set() == {prerequisite ids}` (entries had no inherited parents).
- Final-tests-on-base rerun (2026-09-20, isolated shared clone at 4f6ef8806f, current
  final test file copied in): `artifacts/kanban-dependency-inheritance/red-test-output-final.txt`
  — same 3 failed / 3 passed in 2.31s. The committed tests demonstrably fail on base.

### Green evidence (fixed code)

- `artifacts/kanban-dependency-inheritance/green-test-output.txt` (run 10) and
  `green-test-output-2.txt` (rerun this session): 6/6 pass.
- Related kanban suites (no regressions): `related-test-output.txt` — 15/15 across
  test_kanban_decompose.py, test_kanban_decompose_db.py, test_kanban_parent_reopen_invalidation.py,
  test_kanban_worktree_isolation.py.

### Pre-existing failures (not caused by this change)

`artifacts/kanban-dependency-inheritance/kanban-db-test-output.txt` shows 3 failures in the
full `test_kanban_db.py` file: `test_rate_limit_exit_requeues_without_counting_failure`,
`test_worktree_workspace_explicit_target_materializes_linked_worktree`,
`test_resolve_hermes_argv_falls_back_to_module_form_when_no_path_shim`
(e.g. `hermes.EXE` shim argv vs expected `python -m hermes_cli.main`). Reproduced
IDENTICALLY on the pristine base commit in an isolated clone:
`artifacts/kanban-dependency-inheritance/kanban-db-base-check-output.txt` (3 failed,
29 deselected). These are environment/venv-layout issues on this Windows box, unrelated to
`decompose_triage_task` (the diff touches only that function).

## Deterministic smoke harness (real mutation APIs, isolated DB)

`scripts/prove_kanban_dependency_inheritance.py` (committed, no LLM, no live board):

- Creates a disposable temp `HERMES_HOME`, `init_db()`, and drives only
  `hermes_cli.kanban_db` mutation APIs (`create_task`, `link_tasks`, `decompose_triage_task`,
  `claim_task`, `complete_task`, `parent_ids`, `child_ids`, `get_task`).
- Primary scenario: `P -> R -> downstream`; decompose R into parallel entries + dependent;
  asserts entry links {P}, dependent links {entries}, both entries `todo` (P unfinished);
  claims blocked before P completes; after P completes entries `ready` and claims succeed;
  dependent waits for BOTH entries; R waits for the graph; downstream waits for R; every
  claim completes; final statuses all `done`.
- Nested scenario: `nested_P -> outer -> inner`; inner decomposed FIRST (inner entry gated
  by outer), outer decomposed later (outer entry gated by nested_P); claims blocked at each
  level until the prerequisite chain completes; inner entry `ready` only after
  nested_P -> outer_entry -> outer all complete (transitive preservation).
- Emits `proof.json` (full id/link/status snapshots at every stage) + `proof.txt`.
- Repeated regeneration is safe: fresh temp home per run, temp dir removed after run,
  no configured board ever opened. Run twice (run 10 and this session) — both PASS.

Regeneration command (from repo root):

    ./venv/Scripts/python.exe scripts/prove_kanban_dependency_inheritance.py --output-dir artifacts/kanban-dependency-inheritance

Result (this session, `artifacts/kanban-dependency-inheritance/smoke-command-output-2.txt`):

    PASS: .../proof.json
    PASS: .../proof.txt

Key evidence in `proof.txt` (real run):

    Primary real links: 9
    Claims blocked before P completed:  {'entry_a': True, 'entry_b': True}
    Claims successful after P completed: {'entry_a': True, 'entry_b': True, 'dependent': True, 'R': True, 'downstream': True}
    Primary final statuses: {'P': done, 'R': done, 'downstream': done, entry_a/b/dependent: done}
    Nested inner entry after outer prerequisite chain completed: ready

## Coarse-card scheduling inspection (todo cards are NOT auto-decomposed)

Question: are future coarse cards in `todo` automatically decomposed when their
prerequisites complete?

Answer: NO — they are merely executed. Findings, from `gateway/kanban_watchers.py`,
`gateway/kanban_watchers_dispatcher.py`, `hermes_cli/kanban_decompose.py` and config:

- The ONLY automatic decomposition path is the gateway dispatcher's
  `auto_decompose_tick()` (`gateway/kanban_watchers_dispatcher.py:237`), which runs
  BEFORE dispatch fans out, once per dispatcher tick, and selects tasks via
  `kanban_decompose.list_triage_ids()` — a SQL filter on `status='triage'`
  (`hermes_cli/kanban_decompose.py:334`). Decomposition is a TRIAGE-column operation only.
- `kanban_decompose.decompose_task()` calls `_load_triage_task()`
  (`hermes_cli/kanban_specify.py:126`), which refuses any task whose status is not
  `triage` ("task is not in triage (status=...)"). There is no code path that decomposes a
  `todo` card, on prerequisite completion or otherwise.
- What actually happens on prerequisite completion: the notifier/recompute promotes the
  card `todo -> ready` once all `task_links` parents are done; the dispatcher then claims
  it and spawns its assignee profile to EXECUTE the card as a single work unit (the
  goal-mode loop, when enabled, iterates the same card — it does not fan out children).
- Limits of auto-decompose: gated by `kanban.auto_decompose` (default true, re-read live
  every tick, `kanban_watchers_common.py`), capped at `kanban.auto_decompose_per_tick`
  (default 3) triage tasks per tick across boards, suspended under `hermes pause`
  (emergency stop), requires an aux LLM (no aux client -> skipped that tick), and a
  malformed LLM reply yields a `ok=False` skip until the next tick. Dispatcher tick
  interval default: `kanban.dispatch_interval_seconds` = 60.
- Practical consequence: a coarse card queued as `triage` with unsatisfied prerequisites
  stays in `triage` and WILL be auto-decomposed once the gateway tick runs; a coarse card
  that was promoted/specified directly into `todo` (or moved out of triage by a manual
  `specify`) is never re-decomposed — it executes wholesale. With this fix, either path
  now preserves the external holds (decomposition inherits them; direct execution was
  already gated by the same `task_links`).

## Activation scope (observed processes; NOTHING was restarted or killed)

`hermes_cli/kanban_db.py` is imported at boot by long-running daemons. Live processes on
this host (wmic-verified, `artifacts/kanban-dependency-inheritance/runtime-status-2.txt`):

- PID 43028 — dashboard (`...\hermes.exe dashboard`), machine-level web server serving the
  kanban pages (Decompose button / specify endpoints). Runs on this repo's
  `.hermes-runtime` python -> imports the working tree. Needs a restart to pick up the fix.
  Supported: `hermes dashboard --stop`, then start again (`hermes dashboard`); `hermes
  update` also respawns dashboards as part of its update path.
- PID 29688 — gateway for the ORCHESTRATOR profile (`venv\Scripts\hermes.exe -p
  orchestrator gateway run`), running the kanban dispatcher / auto-decompose / notifier —
  i.e. the runtime that would call `decompose_triage_task` automatically. Also imports
  this tree. Needs a restart. Supported: `hermes -p orchestrator gateway restart`, or
  fleet-wide `hermes gateway restart --all`; in-band `/restart` drains active runs first.
  (Note: `hermes gateway status` from the default profile reports "not running"; that is
  profile-scoped — the orchestrator gateway above is live.)
- No default-profile gateway and no `serve`/desktop backend were observed at scan time.
- Short-lived processes (CLI commands, kanban workers) import fresh code on every spawn and
  need no reload.

Per task constraints this worker did NOT stop/restart any process, did not push, and did not
touch live board data. The operator should apply the restart scope above (dashboard +
orchestrator gateway) after verifying the artifact/commit.

## Committed paths (named only)

- hermes_cli/kanban_db.py
- tests/hermes_cli/test_kanban_decompose_db.py
- scripts/prove_kanban_dependency_inheritance.py
- artifacts/kanban-dependency-inheritance/ (REPORT.md + all receipts; other tasks' dirs
  under artifacts/ were left untouched and uncommitted)

## Artifacts (absolute paths)

- C:\Users\kevin\AppData\Local\hermes\hermes-agent\artifacts\kanban-dependency-inheritance\REPORT.md
- C:\Users\kevin\AppData\Local\hermes\hermes-agent\artifacts\kanban-dependency-inheritance\proof.json
- C:\Users\kevin\AppData\Local\hermes\hermes-agent\artifacts\kanban-dependency-inheritance\proof.txt
- ...\red-test-output.txt (run 10 red capture)
- ...\red-test-output-final.txt (final tests on pristine base clone)
- ...\green-test-output.txt, ...\green-test-output-2.txt (6/6 after fix)
- ...\related-test-output.txt (15/15 kanban suites)
- ...\kanban-db-test-output.txt (full test_kanban_db.py: 3 pre-existing env failures)
- ...\kanban-db-base-check-output.txt (same 3 failures reproduced on base)
- ...\smoke-command-output.txt, ...\smoke-command-output-2.txt (harness runs)
- ...\runtime-status.txt (initial status scan), ...\runtime-status-2.txt (corrected,
  wmic-verified process picture)

## Regeneration commands (from repo root)

    # regression tests
    scripts/run_tests.sh tests/hermes_cli/test_kanban_decompose_db.py
    # related suites
    scripts/run_tests.sh tests/hermes_cli/test_kanban_decompose.py tests/hermes_cli/test_kanban_decompose_db.py tests/hermes_cli/test_kanban_parent_reopen_invalidation.py tests/hermes_cli/test_kanban_worktree_isolation.py
    # smoke proof (isolated temp board, no LLM)
    ./venv/Scripts/python.exe scripts/prove_kanban_dependency_inheritance.py --output-dir artifacts/kanban-dependency-inheritance