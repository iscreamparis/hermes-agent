"""Symlink/junction guards for the terminal tool — real filesystem, not regex-only.

Two independent rules (``tools/link_escape_guard.py``):
  1. an agent may not CREATE a symlink/junction;
  2. a recursive delete may not ESCAPE the working directory by following one.

The escape tests create a REAL link on the test filesystem (junction on Windows when
symlinks need privileges) and assert the block happens before execution, mirroring
``TestDeleteSkillRmtreeGuard`` in ``tests/tools/test_skill_manager_tool.py``.
"""

import json
import os
import subprocess
import sys
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from tools.link_escape_guard import (
    _is_link, _is_recursive_delete, check_link_guards, detect_link_creation,
    detect_link_escaping_delete,
)


# --- real link creation on the test filesystem ------------------------------------------------

def _make_real_link(link: str, target: str) -> bool:
    """Create a real directory link at *link* -> *target*. False when the platform refuses
    (POSIX-less Windows without developer mode AND no mklink); the caller skips."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if sys.platform.startswith("win"):
        try:
            result = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                                    capture_output=True, timeout=30)
            return result.returncode == 0 and os.path.exists(link)
        except (OSError, subprocess.SubprocessError):
            return False
    return False


@pytest.fixture
def linked_tree(tmp_path):
    """work/ (the agent's cwd) with a link out to a sibling victim/ repo.

    Shape of the real incident: ``work/CRM_Atoms`` is a junction to a SIBLING directory,
    so a recursive delete inside work/ reaches files that are not in work/ at all.
    """
    work = tmp_path / "work"
    work.mkdir()
    victim = tmp_path / "victim_repo"
    (victim / ".git").mkdir(parents=True)
    (victim / "AGENTS.md").write_text("DO NOT DELETE", encoding="utf-8")
    link = work / "CRM_Atoms"
    if not _make_real_link(str(link), str(victim)):
        pytest.skip("filesystem/platform refuses symlink and junction creation")
    return {"work": work, "victim": victim, "link": link}


# --- rule 1: creation is refused ---------------------------------------------------------------

class TestLinkCreationBlocked:
    @pytest.mark.parametrize("command", [
        "ln -s /q/WebProjects/atoms ./CRM_Atoms",
        "ln -sf ../atoms node_modules/atoms",
        "ln --symbolic ../atoms ./atoms",
        "mklink /J CRM_Atoms Q:\\WebProjects\\atoms",
        "cmd /c mklink /D link target",
        'powershell -Command "New-Item -ItemType SymbolicLink -Path link -Target ..\\atoms"',
        'powershell -Command "New-Item -Path j -ItemType Junction -Value ..\\atoms"',
        'python -c "import os; os.symlink(\'/etc\', \'./etc\')"',
    ])
    def test_creation_spellings_detected(self, command):
        creates, description = detect_link_creation(command)
        assert creates is True, command
        assert description

    @pytest.mark.parametrize("command", [
        "ls -la",
        "npm install",                              # npm may create links: npm is not blocked
        "git clone https://example.com/repo.git",   # git may create links too
        "learn -s something",                       # 'ln' must be a word, not a substring
        'git commit -m "document why we use ln -s here"',   # quoted prose
        'echo "mklink is forbidden"',
        "ln -h",                                    # no -s: hardlink/help, not a symlink
    ])
    def test_benign_commands_not_flagged(self, command):
        assert detect_link_creation(command)[0] is False, command

    def test_message_names_the_rule_and_the_allowed_creators(self, tmp_path):
        message = check_link_guards("ln -s ../atoms ./atoms", str(tmp_path))
        assert message
        assert "symlink" in message.lower()
        assert "npm" in message.lower() and "git" in message.lower()


# --- rule 2: recursive delete escaping through a real link -------------------------------------

class TestRecursiveDeleteLinkEscape:
    """Real junction/symlink on disk; the guard must refuse BEFORE anything is deleted."""

    def test_recursive_delete_of_the_link_itself_is_blocked(self, linked_tree):
        message = detect_link_escaping_delete("rm -rf CRM_Atoms", str(linked_tree["work"]))
        assert message, "recursive delete through a junction must be blocked"
        assert "symlink/junction" in message
        assert str(linked_tree["victim"]) in message, "resolved out-of-tree path must be shown"
        assert (linked_tree["victim"] / "AGENTS.md").exists(), "nothing may be deleted"

    def test_recursive_delete_of_parent_containing_the_link_is_blocked(self, linked_tree):
        """The npm case: deleting a DIRECTORY that merely CONTAINS a link out of the tree."""
        message = detect_link_escaping_delete("rm -rf .", str(linked_tree["work"]))
        assert message
        assert str(linked_tree["victim"]) in message

    def test_npm_style_legitimate_link_also_blocks(self, tmp_path):
        """Kevin's explicit decision: even a legitimate npm ``file:`` dep link blocks a
        recursive delete that would traverse it. No good-link/bad-link distinction."""
        work = tmp_path / "app"
        (work / "node_modules" / "@real3d").mkdir(parents=True)
        shared = tmp_path / "CRM_Atoms"
        (shared / "src").mkdir(parents=True)
        (shared / "src" / "index.ts").write_text("export const x = 1;", encoding="utf-8")
        link = work / "node_modules" / "@real3d" / "atoms"
        if not _make_real_link(str(link), str(shared)):
            pytest.skip("filesystem/platform refuses symlink and junction creation")
        message = detect_link_escaping_delete("rm -rf node_modules", str(work))
        assert message, "an npm link out of the tree must still block a recursive delete"
        assert str(shared) in message
        assert (shared / "src" / "index.ts").exists()

    def test_windows_spellings_are_covered(self, linked_tree):
        for command in ("rd /s /q CRM_Atoms",
                        "Remove-Item -Recurse -Force CRM_Atoms",
                        "rm -rf ./CRM_Atoms"):
            assert detect_link_escaping_delete(command, str(linked_tree["work"])), command

    # --- what must STILL be allowed ---

    def test_non_recursive_removal_of_the_link_is_allowed(self, linked_tree):
        """Removing the link itself without recursion is the documented remedy."""
        for command in ("rm CRM_Atoms", "unlink CRM_Atoms", "rmdir CRM_Atoms"):
            assert check_link_guards(command, str(linked_tree["work"])) is None, command

    def test_recursive_delete_of_a_real_in_tree_path_is_allowed(self, linked_tree):
        """No false positive on legitimate in-tree cleanup."""
        build = linked_tree["work"] / "dist"
        (build / "assets").mkdir(parents=True)
        (build / "assets" / "app.js").write_text("//", encoding="utf-8")
        assert check_link_guards("rm -rf dist", str(linked_tree["work"])) is None
        assert check_link_guards("rm -rf ./dist/assets", str(linked_tree["work"])) is None

    def test_link_resolving_INSIDE_the_tree_is_allowed(self, tmp_path):
        """A link that stays in the work tree cannot escape it, so it must not block."""
        work = tmp_path / "work"
        (work / "real").mkdir(parents=True)
        (work / "pkg").mkdir()
        link = work / "pkg" / "inner"
        if not _make_real_link(str(link), str(work / "real")):
            pytest.skip("filesystem/platform refuses symlink and junction creation")
        assert check_link_guards("rm -rf pkg", str(work)) is None

    def test_nonexistent_path_is_not_blocked(self, linked_tree):
        assert check_link_guards("rm -rf does-not-exist", str(linked_tree["work"])) is None

    def test_non_delete_commands_are_not_blocked(self, linked_tree):
        for command in ("ls -R CRM_Atoms", "grep -r foo CRM_Atoms", "cp -r CRM_Atoms /tmp/x"):
            assert check_link_guards(command, str(linked_tree["work"])) is None, command


class TestRecursiveDeleteClassification:
    @pytest.mark.parametrize("description,expected", [
        ("recursive delete", True),
        ("recursive delete (long flag)", True),
        ("recursive delete (flags after operands)", True),
        ("delete in root path", True),
        ("Windows destructive delete (recursive/quiet switch)", True),
        ("PowerShell destructive delete (Remove-Item)", True),
        ("Windows cmd destructive delete", True),
        ("force kill processes", False),
        ("SQL DELETE without WHERE", False),
        ("pipe remote content to shell", False),
        (None, False),
    ])
    def test_only_recursive_delete_descriptions_arm_the_guard(self, description, expected):
        assert _is_recursive_delete(description) is expected


class TestIsLink:
    def test_real_link_detected(self, linked_tree):
        assert _is_link(str(linked_tree["link"])) is True

    def test_real_directory_is_not_a_link(self, linked_tree):
        assert _is_link(str(linked_tree["work"])) is False

    def test_missing_path_is_not_a_link(self, tmp_path):
        assert _is_link(str(tmp_path / "nope")) is False


# --- terminal_tool wiring: blocked BEFORE execution --------------------------------------------

def _make_env_config(cwd, **overrides):
    config = {
        "env_type": "local", "timeout": 180, "cwd": str(cwd), "host_cwd": None,
        "modal_mode": "auto", "docker_image": "", "singularity_image": "",
        "modal_image": "", "daytona_image": "",
    }
    config.update(overrides)
    return config


def _run_terminal(command, config, **kwargs):
    """Drive the real terminal_tool with a mocked env; returns (result, env)."""
    from tools.terminal_tool import terminal_tool

    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "ok", "returncode": 0}
    mock_env.cwd = config["cwd"]
    with ExitStack() as stack:
        stack.enter_context(patch("tools.terminal_tool._get_env_config", return_value=config))
        stack.enter_context(patch("tools.terminal_tool._start_cleanup_thread"))
        stack.enter_context(
            patch("tools.terminal_tool._active_environments", {"default": mock_env}))
        stack.enter_context(patch("tools.terminal_tool._last_activity", {"default": 0}))
        stack.enter_context(patch("tools.terminal_tool._session_cwd", {}))
        stack.enter_context(
            patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}))
        result = json.loads(terminal_tool(command=command, **kwargs))
    return result, mock_env


class TestTerminalToolWiring:
    def test_escaping_delete_blocked_before_execution(self, linked_tree):
        config = _make_env_config(linked_tree["work"])
        result, env = _run_terminal("rm -rf CRM_Atoms", config)
        assert result["status"] == "blocked", result
        assert "symlink/junction" in result["error"]
        assert str(linked_tree["victim"]) in result["error"]
        env.execute.assert_not_called()
        assert (linked_tree["victim"] / "AGENTS.md").exists()

    def test_force_cannot_bypass_the_delete_guard(self, linked_tree):
        config = _make_env_config(linked_tree["work"])
        result, env = _run_terminal("rm -rf CRM_Atoms", config, force=True)
        assert result["status"] == "blocked"
        env.execute.assert_not_called()

    def test_link_creation_blocked_before_execution(self, tmp_path):
        config = _make_env_config(tmp_path)
        result, env = _run_terminal("mklink /J CRM_Atoms Q:\\WebProjects\\atoms", config)
        assert result["status"] == "blocked", result
        assert "symlink" in result["error"].lower()
        env.execute.assert_not_called()
        assert not (tmp_path / "CRM_Atoms").exists(), "no link may be created"

    def test_ln_s_blocked_before_execution(self, tmp_path):
        config = _make_env_config(tmp_path)
        result, env = _run_terminal("ln -s ../atoms ./atoms", config)
        assert result["status"] == "blocked"
        env.execute.assert_not_called()
        assert not os.path.lexists(str(tmp_path / "atoms"))

    def test_workdir_is_the_guard_root(self, linked_tree):
        """The guard resolves against the per-command workdir, not the session cwd."""
        config = _make_env_config(linked_tree["work"].parent)
        result, env = _run_terminal("rm -rf CRM_Atoms", config,
                                    workdir=str(linked_tree["work"]))
        assert result["status"] == "blocked", result
        env.execute.assert_not_called()

    def test_in_tree_recursive_delete_still_runs(self, linked_tree):
        (linked_tree["work"] / "dist").mkdir()
        config = _make_env_config(linked_tree["work"])
        result, env = _run_terminal("rm -rf dist", config)
        assert result.get("status") != "blocked", result
        env.execute.assert_called()

    def test_remote_backend_skips_filesystem_inspection(self, linked_tree):
        """A remote path must not be resolved against THIS host's filesystem."""
        config = _make_env_config(linked_tree["work"], env_type="ssh")
        result, env = _run_terminal("rm -rf CRM_Atoms", config)
        assert result.get("status") != "blocked", result
        env.execute.assert_called()

    def test_remote_backend_still_blocks_link_creation(self, linked_tree):
        """Creation is text-only, so it applies on every backend."""
        config = _make_env_config(linked_tree["work"], env_type="ssh")
        result, env = _run_terminal("ln -s /srv/atoms ./atoms", config)
        assert result["status"] == "blocked", result
        env.execute.assert_not_called()
