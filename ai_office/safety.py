"""Risk check for "full" permission mode: everything runs, except things that touch the OS,
your secrets, or stuff outside the workspace. Those are sent to DeepSeek / you.

check() returns None when a request is fine, or a short reason why it should be asked.
Tune the lists below freely.
"""

import os
import re
import shlex
from pathlib import Path

HOME = Path.home()

# first word of a command → always ask
ASK_COMMANDS = {
    "sudo": "runs as root", "su": "switches user", "doas": "runs as root", "pkexec": "runs as root",
    "pacman": "system packages", "yay": "system packages", "paru": "system packages", "apt": "system packages",
    "apt-get": "system packages", "dnf": "system packages", "zypper": "system packages", "snap": "system packages",
    "flatpak": "system apps", "systemctl": "system services", "service": "system services",
    "reboot": "reboots", "shutdown": "shuts down", "poweroff": "shuts down", "halt": "shuts down",
    "dd": "raw disk writes", "mkfs": "formats disks", "fdisk": "partitions", "parted": "partitions",
    "wipefs": "wipes disks", "mount": "mounts disks", "umount": "unmounts disks", "shred": "destroys files",
    "kill": "kills processes", "pkill": "kills processes", "killall": "kills processes",
    "crontab": "scheduled jobs", "hyprctl": "controls your desktop", "chsh": "changes your shell",
    "passwd": "passwords", "useradd": "users", "usermod": "users", "visudo": "sudo config",
}

# commands that write/delete their path arguments
WRITERS = {"rm", "rmdir", "mv", "cp", "ln", "touch", "mkdir", "chmod", "chown", "chgrp", "truncate", "tee",
           "install", "rsync", "unlink"}

SECRETS = (".ssh", ".aws", ".gnupg", ".git-credentials", ".netrc", ".docker/config.json", ".kube",
           ".config/gh/hosts.yml", ".claude/.credentials.json", ".codex/auth.json", ".pypirc", ".npmrc")

GIT_ASK = [
    (r"\bpush\b.*(--force|-f\b|--force-with-lease)", "force-pushes"),
    (r"\bpush\b", "pushes to a remote"),
    (r"\breset\b.*--hard", "discards work (reset --hard)"),
    (r"\bclean\b.*-[a-z]*f", "deletes untracked files"),
    (r"\bfilter-branch\b|\bfilter-repo\b", "rewrites history"),
    (r"\bremote\b.*\b(add|set-url|remove)\b", "changes remotes"),
    (r"\bconfig\b.*--global", "changes your global git config"),
]

# home guards (always on, even when the workspace is ~): your config and whole folders
CONFIG_AREAS = [HOME / x for x in (".config", ".local", ".mydotfiles", ".oh-my-zsh", ".claude", ".codex", ".ssh", ".gnupg")]
BIG_PARENTS = [HOME, HOME / "Workplace"]
REMOVERS = {"rm", "rmdir", "mv", "unlink"}

SPLIT_RE = re.compile(r"\|\||&&|;|\||&|\n|\$\(|`|\)")


def _inside(path_str, workspace, cwd=None):
    p = Path(os.path.expanduser(path_str))
    p = ((cwd or workspace) / p).resolve() if not p.is_absolute() else p.resolve()
    ws = workspace.resolve()
    return p == ws or ws in p.parents, p


def _short(p):
    return "~/" + str(p.relative_to(HOME)) if HOME in p.parents else str(p)


def _protected(resolved, workspace, removing=False):
    """Reason if this path is your config/dotfiles or a whole top-level folder, else None."""
    ws = workspace.resolve()
    for area in CONFIG_AREAS:
        if (resolved == area or area in resolved.parents) and not (ws == area or area in ws.parents):
            return f"changes your config ({_short(resolved)})"
    if resolved.parent == HOME and resolved.name.startswith(".") and ws != resolved and resolved not in ws.parents:
        return f"changes your dotfiles ({_short(resolved)})"
    if removing:
        if resolved == ws:
            return "deletes the whole project folder"
        if resolved == HOME or resolved in BIG_PARENTS or (resolved.parent in BIG_PARENTS and resolved.is_dir()):
            return f"deletes/moves a whole folder ({_short(resolved)})"
    return None


def _secret(path_str, workspace):
    s = os.path.expanduser(path_str)
    if any(f"/{x}" in s or s.startswith(x) for x in SECRETS):
        return True
    name = Path(s).name
    return name == ".env" or name.startswith(".env.") and not name.endswith((".example", ".sample", ".template"))


def check_command(command, workspace):
    workspace = Path(workspace)
    cwd = workspace  # follows `cd` so relative paths resolve correctly
    if re.search(r"(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|da|fi)?sh\b", command):
        return "pipes a download into a shell"
    cd_out = any(not _inside(m.group(1), workspace)[0] for m in re.finditer(r"\bcd\s+([^\s;&|)]+)", command))
    if cd_out and re.search(r">>?\s*[^\s;&|/~$&]", command):
        return "changes directory out of the project and writes files"
    for m in re.finditer(r">>?\s*([^\s;&|]+)", command):  # redirections
        target = m.group(1)
        if target.startswith("&") or target in ("/dev/null", "/dev/stdout", "/dev/stderr"):
            continue
        if target.startswith(("$", "/tmp/")):
            continue
        inside, resolved = _inside(target, workspace)
        if not inside:
            return f"writes to {target} (outside the project)"
        guard = _protected(resolved, workspace)
        if guard:
            return guard
    for part in SPLIT_RE.split(command):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        while words and ("=" in words[0] and not words[0].startswith("-")):  # FOO=bar cmd
            words = words[1:]
        if not words:
            continue
        cmd, args = os.path.basename(words[0]), words[1:]
        if cmd in ("env", "nohup", "time", "nice", "xargs", "exec", "command") and args:
            cmd, args = os.path.basename(args[0]), args[1:]
        if cmd in ASK_COMMANDS:
            return f"`{cmd}` {ASK_COMMANDS[cmd]}"
        if cmd.startswith("mkfs."):
            return "formats disks"
        if cmd in ("npm", "pnpm", "yarn", "bun") and any(a in ("-g", "--global") for a in args):
            return "installs globally"
        if cmd in ("pip", "pip3") and ("--break-system-packages" in args or "--user" in args):
            return "installs into your system Python"
        if cmd == "git":
            rest = " ".join(args)
            for pat, why in GIT_ASK:
                if re.search(pat, rest):
                    return f"git {why}"
        paths = [a for a in args if not a.startswith("-")]
        for a in paths:
            if _secret(a, workspace):
                return f"touches secrets ({a})"
        if cmd in WRITERS or (cmd == "sed" and any(a.startswith("-i") for a in args)):
            for a in paths:
                inside, resolved = _inside(a, workspace, cwd)
                if not inside:
                    return f"`{cmd}` changes {a} (outside the project)"
                guard = _protected(resolved, workspace, removing=cmd in REMOVERS)
                if guard:
                    return guard
        if cmd == "cd":
            cwd = _inside(paths[0] if paths else str(HOME), workspace, cwd)[1]
    return None


def check(tool_name, tool_input, workspace):
    """None = fine to run without asking; otherwise the reason to ask."""
    workspace = Path(workspace)
    if tool_name == "Bash":
        return check_command(tool_input.get("command", ""), workspace)
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
    if path:
        if _secret(path, workspace):
            return f"touches secrets ({path})"
        if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            inside, resolved = _inside(path, workspace)
            if not inside:
                return f"edits {path} (outside the project)"
            return _protected(resolved, workspace)
    return None
