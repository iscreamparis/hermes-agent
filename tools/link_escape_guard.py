"""Symlink/junction guards for the terminal tool.

Two independent, cumulative rules, both enforced BEFORE a command executes:

1. **Creation** — an agent never creates a symlink/junction itself (``ln -s``,
   ``mklink``, PowerShell ``New-Item -ItemType SymbolicLink|Junction``). Package
   managers and git create the links a project legitimately needs; an agent typing
   one by hand is how a sibling repo ends up reachable from inside the work tree.
2. **Recursive deletion that escapes through a link** — a destructive recursive
   command whose operand *is* a link, sits *under* a link, or *contains* a link
   that resolves outside the agent's current working directory is refused. No
   distinction is made between a link an agent created and one npm/git created:
   a recursive delete must never leave the working directory by following a link.

Real incident behind rule 2: a ``CRM_Atoms -> ../atoms`` junction inside a repo
turned a routine recursive clean into a wipe of a *sibling* repo's ``.git``,
``dist`` and ``AGENTS.md``.

The link-following logic mirrors ``tools/skill_manager_guards.py``
(``_is_path_redirect`` / ``_validate_delete_target``, port of Kilo Code #11227):
same idea — resolve for real, refuse anything that leaves the tree — applied to
shell commands instead of a skill directory. That module is deliberately left
untouched; only its principle is reused here.
"""

import logging
import os
import re
import shlex
import stat
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

logger = logging.getLogger("tools.terminal_tool")

# Bound the subtree scan: a recursive delete of a huge tree must not stall the
# tool thread. Past the budget the scan stops (fail-open on the deep part); the
# operand itself and its ancestors are always checked.
_MAX_SCAN_ENTRIES = 20000


# --- 1. link CREATION ------------------------------------------------------------------------

# (regex, description, scan_raw). Matched against a quote-masked command so prose
# (`git commit -m "use ln -s"`) and filenames cannot trip a shell-level rule. ``scan_raw``
# marks the scripted spellings, which are also matched INSIDE quotes: `python -c "os.symlink(
# ... )"` is a quoted *payload*, i.e. code, and has no prose form worth protecting.
_LINK_CREATION_PATTERNS = [
    # ln -s / -sf / --symbolic (flags before or after operands, GNU permutes them).
    (r"\bln\s+(?:[^\n;|&]*\s)?-[a-z]*s[a-z]*\b", "create symlink (ln -s)", False),
    (r"\bln\s+[^\n;|&]*--symbolic\b", "create symlink (ln --symbolic)", False),
    # Windows cmd built-in: every mklink form creates a link (/D dir symlink, /J junction,
    # /H hardlink, bare = file symlink).
    (r"\bmklink\b", "create symlink/junction (mklink)", False),
    # PowerShell New-Item / ni with a link ItemType, flags in any order.
    (r"\b(?:new-item|ni)\b[^\n;|&]*-itemtype\s+[\"']?(?:symboliclink|junction|hardlink)\b",
     "create symlink/junction (New-Item -ItemType)", False),
    # python -c "os.symlink(...)" / node fs.symlinkSync — the scripted spellings of the same act.
    (r"\bos\.symlink\s*\(", "create symlink (os.symlink)", True),
    (r"\bfs\.symlink(?:sync)?\s*\(", "create symlink (fs.symlink)", True),
]
_LINK_CREATION_COMPILED = [
    (re.compile(p, re.IGNORECASE), d, raw) for p, d, raw in _LINK_CREATION_PATTERNS
]

LINK_CREATION_ADVICE = (
    "Creating symlinks/junctions from the agent is not allowed: a link inside a work tree "
    "silently redirects later recursive operations (copy, clean, delete) into another repo or "
    "directory. Let npm/pnpm (file: deps, workspaces) or git create the links a project needs, "
    "or ask the operator to create this one by hand if it is genuinely required."
)


def _mask_quoted(command: str) -> str:
    """Blank quoted and backtick spans so quoted prose can't match a creation pattern."""
    masked = re.sub(r"'[^']*'", "''", command)
    masked = re.sub(r'"(?:[^"\\]|\\.)*"', '""', masked)
    return re.sub(r"`[^`]*`", "``", masked)


# Commands that hand a quoted argument to another interpreter to EXECUTE: inside them, quoted
# text is code, not prose, so the raw string is scanned as well (`powershell -Command "New-Item
# -ItemType Junction ..."`). Same principle as ``_SHELL_CARRIER_NAMES`` in approval_detection,
# widened to the Windows interpreters because mklink/New-Item are Windows-only spellings.
_SHELL_CARRIER_RE = re.compile(
    r"(?:^|[\n;&|`]|\$\()\s*(?:sudo\s+)?(?:[^\s]*[/\\])?"
    r"(?:powershell|pwsh|cmd|sh|bash|zsh|ksh|dash|eval|python[0-9.]*|node)(?:\.exe)?\b",
    re.IGNORECASE,
)


def _carries_shell_payload(command: str) -> bool:
    """True when *command* invokes an interpreter, so its quoted argument is code."""
    return bool(_SHELL_CARRIER_RE.search(command))


def detect_link_creation(command: str) -> Tuple[bool, Optional[str]]:
    """``(True, description)`` when *command* would create a symlink/junction."""
    if not isinstance(command, str) or not command.strip():
        return (False, None)
    masked = _mask_quoted(command)
    scan_quoted_too = _carries_shell_payload(command)
    for pattern_re, description, scan_raw in _LINK_CREATION_COMPILED:
        if pattern_re.search(masked):
            return (True, description)
        if (scan_raw or scan_quoted_too) and pattern_re.search(command):
            return (True, description)
    return (False, None)


# --- 2. recursive deletion that ESCAPES through a link ---------------------------------------

# Command words consumed by the destructive invocation itself: never path operands.
_NON_PATH_WORDS = frozenset({
    "sudo", "env", "exec", "nohup", "setsid", "time", "xargs", "then", "do", "done", "fi",
    "rm", "rmdir", "rd", "del", "erase", "unlink", "shred",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "remove-item", "ri", "get-childitem", "gci", "dir", "ls",
    "-command", "-c", "-path", "-literalpath", "-recurse", "-force", "-erroraction",
    "silentlycontinue", "stop", "continue",
})

# Windows switch tokens (/s /q /f ...). A POSIX absolute path (/home/kevin) has more than one
# character after the slash, so it is not mistaken for a switch.
_WIN_SWITCH_RE = re.compile(r"^/[a-zA-Z]$")
_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "\n"})


def _is_link(path: str) -> bool:
    """Symlink, Windows junction, or any other reparse-point redirect.

    ``os.path.islink`` is False for NTFS junctions and ``Path.is_junction`` only exists on
    3.12+, so the reparse tag (``os.lstat().st_reparse_tag``, Windows 3.8+) is the fallback
    that makes this work on the 3.11 floor.
    """
    try:
        if os.path.islink(path):
            return True
        p = Path(path)
        is_junction = getattr(p, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        tag = getattr(os.lstat(path), "st_reparse_tag", 0)
        link_tags = {getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None),
                     getattr(stat, "IO_REPARSE_TAG_SYMLINK", None)}
        return bool(tag) and tag in link_tags
    except (OSError, ValueError):
        return False


def _is_recursive_delete(description: Optional[str]) -> bool:
    """True for the DANGEROUS_PATTERNS descriptions that mean 'recursive/destructive delete'.

    Description-based rather than an index into the pattern table so reordering or rewording
    the table cannot silently disarm this guard.
    """
    if not description:
        return False
    d = description.lower()
    if "delete" not in d and "remove-item" not in d:
        return False
    return any(word in d for word in ("recursive", "recurse", "destructive", "root path"))


def _command_operands(command: str) -> List[str]:
    """Path-ish operands of *command*: tokens that are not flags, switches, or command words.

    Over-collects on purpose (a stray word is just a path that does not exist, and a path that
    does not exist can't be a link) and never raises: an untokenizable command falls back to a
    whitespace split so the guard still sees something.
    """
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        tokens = command.split()
    operands: List[str] = []
    for token in tokens:
        raw = token.strip()
        if not raw or raw in _SEPARATORS:
            continue
        if raw.startswith(("$", "%", "-")) or _WIN_SWITCH_RE.match(raw):
            continue
        unquoted = raw.strip("\"'")
        if not unquoted or unquoted.lower() in _NON_PATH_WORDS:
            continue
        if any(ch in unquoted for ch in "*?"):  # globs: the literal prefix is checked below
            unquoted = unquoted.split("*")[0].split("?")[0]
            if not unquoted:
                continue
        operands.append(unquoted)
    return operands


def _within(base: str, candidate: str) -> bool:
    """True when *candidate* is *base* or lives under it (both already realpath'd)."""
    try:
        return os.path.commonpath([base, candidate]) == base
    except (ValueError, OSError):  # different drives / malformed
        return False


def _ancestor_link_escape(base: str, abs_path: str) -> Optional[Tuple[str, str]]:
    """``(link_path, resolved)`` if *abs_path* or one of its ancestors is a link resolving
    outside *base*, else None."""
    parts = Path(abs_path).parts
    for depth in range(1, len(parts) + 1):
        prefix = str(Path(*parts[:depth]))
        if not _is_link(prefix):
            continue
        resolved = os.path.realpath(prefix)
        if not _within(base, resolved):
            return (prefix, resolved)
    return None


def _subtree_link_escape(base: str, abs_path: str) -> Optional[Tuple[str, str]]:
    """``(link_path, resolved)`` for the first link INSIDE *abs_path* that resolves outside
    *base* — the npm case: ``node_modules/@scope/pkg -> ../../other-repo`` makes a recursive
    delete of ``node_modules`` walk out of the work tree (``rd /s`` and ``shutil.rmtree``
    follow junctions). Links are never descended into, and the walk is budget-bounded."""
    if not os.path.isdir(abs_path) or _is_link(abs_path):
        return None
    seen = 0
    try:
        for dirpath, dirnames, filenames in os.walk(abs_path, followlinks=False):
            seen += len(dirnames) + len(filenames)
            for name in list(dirnames):
                child = os.path.join(dirpath, name)
                if not _is_link(child):
                    continue
                dirnames.remove(name)  # never descend through a link
                resolved = os.path.realpath(child)
                if not _within(base, resolved):
                    return (child, resolved)
            if seen > _MAX_SCAN_ENTRIES:
                logger.debug("link-escape subtree scan budget reached under %s", abs_path)
                break
    except OSError:
        return None
    return None


def detect_link_escaping_delete(command: str, cwd: str,
                                operands: Optional[Iterable[str]] = None) -> Optional[str]:
    """Message when a recursive delete would leave *cwd* by following a link, else None.

    Caller must have established that *command* IS a recursive/destructive delete
    (:func:`_is_recursive_delete` against the dangerous-pattern description).
    """
    try:
        base = os.path.realpath(cwd)
    except (OSError, ValueError):
        return None
    if not os.path.isdir(base):
        return None
    for operand in (operands if operands is not None else _command_operands(command)):
        try:
            expanded = os.path.expanduser(operand)
            abs_path = expanded if os.path.isabs(expanded) else os.path.join(base, expanded)
            abs_path = os.path.normpath(abs_path)
        except (OSError, ValueError):
            continue
        if not os.path.lexists(abs_path):
            continue
        escape = _ancestor_link_escape(base, abs_path) or _subtree_link_escape(base, abs_path)
        if escape is None:
            continue
        link_path, resolved = escape
        return (
            f"Blocked: this recursive delete would follow a link out of the working directory. "
            f"'{link_path}' is a symlink/junction pointing to '{resolved}', which is OUTSIDE "
            f"'{base}'. Deleting '{operand}' recursively would therefore destroy files in "
            f"another location (recursive delete follows junctions on Windows). "
            f"Delete the link itself without recursion (e.g. `rm {link_path}` / `rmdir "
            f"{link_path}`), or run the recursive delete from the real target directory after "
            f"confirming with the operator that '{resolved}' is meant to be destroyed."
        )
    return None


def check_link_guards(command: str, cwd: str) -> Optional[str]:
    """Both link rules for *command* run in *cwd*; returns the block message or None.

    Creation is refused on text alone; deletion is refused only when a real filesystem
    inspection shows an operand escaping *cwd* through a link.
    """
    creates, description = detect_link_creation(command)
    if creates:
        return f"Blocked: {description}. {LINK_CREATION_ADVICE}"
    try:
        from tools.approval_detection import detect_dangerous_command
        _, _, dangerous_description = detect_dangerous_command(command)
    except Exception:  # detection unavailable: no recursive-delete signal to act on
        return None
    if not _is_recursive_delete(dangerous_description):
        return None
    return detect_link_escaping_delete(command, cwd)
