"""Manual real-conditions verification of the link guards (documented in the task handoff).

Creates a REAL junction from a work dir to a sibling "victim" repo, then calls the REAL
terminal_tool (local backend, no mocks) with:
  1. `rm -rf <junction>`      -> must be BLOCKED, victim intact
  2. `mklink /J ...`          -> must be BLOCKED, no link created
  3. `rm -rf dist` (in-tree)  -> must RUN
  4. `rm <junction>` (no -r)  -> must RUN (removes the link only, victim intact)
Run: python scripts/verify_link_guard_manual.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.terminal_tool import terminal_tool  # noqa: E402


def make_link(link, target):
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except OSError:
        pass
    if sys.platform.startswith("win"):
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
        if r.returncode == 0:
            return "junction"
    raise SystemExit("cannot create a link on this filesystem")


LINK_GUARD_MARKERS = ("follow a link out of the working directory", "symlink", "junction")


def blocked_by_link_guard(result):
    """True only when the LINK guard blocked it — the ambient approval layer (single-query
    mode, approvals.mode) also blocks recursive deletes, and that is not what we verify here."""
    if result.get("status") != "blocked":
        return False
    error = (result.get("error") or "").lower()
    return any(marker in error for marker in LINK_GUARD_MARKERS)


def run(command, workdir):
    return json.loads(terminal_tool(command=command, workdir=workdir, timeout=30))


def main():
    root = tempfile.mkdtemp(prefix="hermes-linkguard-")
    try:
        work = os.path.join(root, "work")
        victim = os.path.join(root, "victim_repo")
        os.makedirs(os.path.join(work, "dist"))
        os.makedirs(os.path.join(victim, ".git"))
        canary = os.path.join(victim, "AGENTS.md")
        with open(canary, "w", encoding="utf-8") as fh:
            fh.write("DO NOT DELETE")
        link = os.path.join(work, "CRM_Atoms")
        kind = make_link(link, victim)
        print(f"created real {kind}: {link} -> {victim}\n")

        checks = []

        r = run("rm -rf CRM_Atoms", work)
        checks.append(("recursive delete through link blocked by LINK guard",
                       blocked_by_link_guard(r)))
        checks.append(("victim canary intact", os.path.exists(canary)))
        print("1) rm -rf CRM_Atoms ->", r.get("status"), "|", (r.get("error") or "")[:300], "\n")

        r = run("mklink /J EvilLink " + victim, work)
        checks.append(("mklink blocked by LINK guard", blocked_by_link_guard(r)))
        checks.append(("no link created", not os.path.lexists(os.path.join(work, "EvilLink"))))
        print("2) mklink /J ->", r.get("status"), "|", (r.get("error") or "")[:200], "\n")

        r = run("rm -rf dist", work)
        # The ambient approval layer may still gate this (that is the normal dangerous-command
        # prompt, present before this change); only the LINK guard must stay silent.
        checks.append(("in-tree recursive delete NOT blocked by the link guard",
                       not blocked_by_link_guard(r)))
        print("3) rm -rf dist ->", r.get("status"), "| exit", r.get("exit_code"),
              "|", (r.get("error") or "<no error>")[:200], "\n")

        r = run("rm CRM_Atoms" if kind == "symlink" else "rmdir CRM_Atoms", work)
        checks.append(("non-recursive link removal not blocked", not blocked_by_link_guard(r)))
        checks.append(("victim STILL intact after link removal", os.path.exists(canary)))
        print("4) non-recursive link removal ->", r.get("status"), "| exit", r.get("exit_code"), "\n")

        for name, ok in checks:
            print(("PASS  " if ok else "FAIL  ") + name)
        return 0 if all(ok for _, ok in checks) else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
