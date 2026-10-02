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


def log(workspace, n=10):
    r = _git(workspace, "log", f"-{n}", "--stat", "--format=%h %an · %ar%n  %s", check=False)
    return r.stdout.strip()


def is_repo_root(workspace):
    r = _git(workspace, "rev-parse", "--show-toplevel", check=False)
    return r.returncode == 0 and r.stdout.strip() == str(workspace.resolve())


def status_snapshot(workspace):
    """Set of `git status --porcelain` lines for a project repo (not ~), else an empty set."""
    from pathlib import Path
    if Path(workspace).resolve() == Path.home() or not is_repo_root(Path(workspace)):
        return set()
    r = _git(workspace, "status", "--porcelain", "--untracked-files=all", check=False)
    return set(r.stdout.splitlines()) if r.returncode == 0 else set()
