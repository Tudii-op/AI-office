"""Wrappers around each AI. Every agent takes a prompt and returns plain text."""

import json
import os
import subprocess
import tempfile
from pathlib import Path


class AgentError(RuntimeError):
    pass


def _run(cmd, stdin, cwd, timeout, who):
    try:
        p = subprocess.run(
            cmd, input=stdin, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        raise AgentError(f"{who}: command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        raise AgentError(f"{who}: timed out after {timeout}s")
    if p.returncode != 0:
        raise AgentError(
            f"{who} exited with code {p.returncode}\n"
            f"--- stderr ---\n{p.stderr.strip()}\n"
            f"--- stdout (tail) ---\n{p.stdout.strip()[-3000:]}"
        )
    return p


class ClaudeAgent:
    key = "claude"
    label = "Claude"

    def __init__(self, model="", timeout=1200):
        self.model = model
        self.timeout = timeout

    def run(self, prompt, system, workspace, can_edit):
        cmd = ["claude", "-p", "--output-format", "json", "--append-system-prompt", system]
        if self.model:
            cmd += ["--model", self.model]
        if can_edit:
            cmd += ["--permission-mode", "acceptEdits"]
        else:
            cmd += ["--permission-mode", "dontAsk", "--allowedTools", "Read,Grep,Glob"]
        p = _run(cmd, prompt, workspace, self.timeout, self.label)
        try:
            data = json.loads(p.stdout)
        except json.JSONDecodeError:
            raise AgentError(f"Claude returned non-JSON output:\n{p.stdout[-3000:]}\n{p.stderr}")
        if data.get("is_error"):
            raise AgentError(f"Claude reported an error:\n{json.dumps(data, indent=2)[:4000]}")
        return data.get("result", "")


class GptAgent:
    key = "gpt"
    label = "GPT"

    def __init__(self, model="", timeout=1200):
        self.model = model
        self.timeout = timeout

    def run(self, prompt, system, workspace, can_edit):
        fd, out_path = tempfile.mkstemp(prefix="ai-office-gpt-", suffix=".txt")
        os.close(fd)
        cmd = [
            "codex", "exec",
            "-C", str(workspace),
            "-s", "workspace-write" if can_edit else "read-only",
            "-o", out_path,
            "--color", "never",
        ]
        if self.model:
            cmd += ["-m", self.model]
        cmd.append("-")  # prompt from stdin
        try:
            p = _run(cmd, f"{system}\n\n{prompt}", workspace, self.timeout, self.label)
            text = Path(out_path).read_text().strip()
            return text or p.stdout.strip()
        finally:
            Path(out_path).unlink(missing_ok=True)


class FakeAgent:
    """Costs nothing. Used with --fake to test the loop, git and UI."""

    def __init__(self, key, label):
        self.key = key
        self.label = label
        self.calls = 0

    def run(self, prompt, system, workspace, can_edit):
        self.calls += 1
        if can_edit:
            f = Path(workspace) / "fake_output.txt"
            with f.open("a") as fh:
                fh.write(f"{self.label} edit #{self.calls}\n")
            return f"[fake] {self.label} appended a line to fake_output.txt.\nSTATUS: CONTINUE"
        if self.calls >= 2:
            return f"[fake] {self.label} reviewed it. Looks complete.\nSTATUS: DONE"
        return f"[fake] {self.label} reviewed: please add one more line.\nSTATUS: CONTINUE"


def make_agent(key, cfg, fake=False):
    if fake:
        return FakeAgent(key, {"claude": "Claude", "gpt": "GPT"}[key] + " (fake)")
    c = cfg.get(key, {})
    cls = {"claude": ClaudeAgent, "gpt": GptAgent}[key]
    return cls(model=c.get("model", ""), timeout=c.get("timeout", 1200))
