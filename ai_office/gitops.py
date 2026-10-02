"""Auto-commit each turn's file changes in the workspace.

Only paths inside the workspace are ever staged/committed, so running inside a
bigger repo never sweeps up unrelated files.
"""

import subprocess


def _git(workspace, *args, check=True):
    return subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True, check=check)


def ensure_repo(workspace):
    """Give the workspace its own repo unless it's a tracked part of an existing one."""
    inside = _git(workspace, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0
    ignored = inside and _git(workspace, "check-ignore", "-q", ".", check=False).returncode == 0
    if not inside or ignored:
        _git(workspace, "init", "-q")
        return True
    return False


def commit_turn(workspace, label, message):
    """Returns the short hash if something was committed, else None."""
    _git(workspace, "add", "-A", "--", ".")
    if _git(workspace, "diff", "--cached", "--quiet", "--", ".", check=False).returncode == 0:
        return None
    first = next((l.strip() for l in message.splitlines() if l.strip()), "changes")[:72]
    _git(
        workspace,
        "-c", f"user.name=AI Office ({label})",
        "-c", "user.email=ai-office@localhost",
        "commit", "-q", "-m", f"[{label}] {first}", "--", ".",
    )
    return _git(workspace, "rev-parse", "--short", "HEAD").stdout.strip()
