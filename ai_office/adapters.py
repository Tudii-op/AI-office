"""Wrappers around each AI. Every agent takes a prompt and returns plain text."""

import json
import os
import signal
import subprocess
import tempfile
import threading
from pathlib import Path


class AgentError(RuntimeError):
    pass


class AgentCancelled(AgentError):
    pass


_running = set()
_lock = threading.Lock()
cancelled = threading.Event()  # set by the office when you press Esc / Ctrl+C


def cancel_running():
    cancelled.set()
    with _lock:
        procs = list(_running)
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _run(cmd, stdin, cwd, timeout, who):
    if cancelled.is_set():
        raise AgentCancelled(f"{who}: stopped")
    try:
        # own process group: we can stop the whole tree, and the terminal's Ctrl+C doesn't reach it
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             cwd=cwd, text=True, start_new_session=True)
    except FileNotFoundError:
        raise AgentError(f"{who}: command not found: {cmd[0]}")
    with _lock:
        _running.add(p)
    try:
        out, err = p.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        p.communicate()
        raise AgentError(f"{who}: timed out after {timeout}s")
    finally:
        with _lock:
            _running.discard(p)
    if cancelled.is_set():
        raise AgentCancelled(f"{who}: stopped")
    if p.returncode != 0:
        raise AgentError(
            f"{who} exited with code {p.returncode}\n"
            f"--- stderr ---\n{err.strip()}\n"
            f"--- stdout (tail) ---\n{out.strip()[-3000:]}"
        )
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


class ClaudeAgent:
    key = "claude"
    label = "Claude"

    def __init__(self, model="", timeout=1200):
        self.model = model
        self.timeout = timeout
        self.last_rate_limit = None  # rate_limit_info from the last run

    def run(self, prompt, system, workspace, can_edit, perms=None):
        """perms (from the office): mode, allowed, disallowed, mcp_config (permission prompts → AI Office)."""
        perms = perms or {"mode": "acceptEdits" if can_edit else "dontAsk",
                          "allowed": [] if can_edit else ["Read", "Grep", "Glob"]}
        cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
               "--append-system-prompt", system, "--permission-mode", perms["mode"]]
        if self.model:
            cmd += ["--model", self.model]
        if perms.get("mcp_config"):
            cmd += ["--mcp-config", perms["mcp_config"], "--permission-prompt-tool", perms["prompt_tool"]]
        else:
            cmd += ["--permission-prompts", "none"]
        if perms.get("disallowed"):
            cmd += ["--disallowedTools", *perms["disallowed"]]
        if perms.get("allowed"):
            cmd += ["--allowedTools", *perms["allowed"]]
        p = _run(cmd, prompt, workspace, self.timeout, self.label)
        result = None
        for line in p.stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "rate_limit_event":
                self.last_rate_limit = ev.get("rate_limit_info") or self.last_rate_limit
            elif ev.get("type") == "result":
                result = ev
        if result is None:
            raise AgentError(f"Claude returned no result event:\n{p.stdout[-3000:]}\n{p.stderr}")
        if result.get("is_error"):
            raise AgentError(f"Claude reported an error:\n{json.dumps(result, indent=2)[:4000]}")
        return result.get("result", "")


class GptAgent:
    key = "gpt"
    label = "GPT"

    def __init__(self, model="", timeout=1200):
        self.model = model
        self.timeout = timeout

    def run(self, prompt, system, workspace, can_edit, perms=None):
        """perms: network (bool), auto_review (bool). GPT can't route approvals to the chat; the sandbox decides."""
        perms = perms or {}
        fd, out_path = tempfile.mkstemp(prefix="ai-office-gpt-", suffix=".txt")
        os.close(fd)
        cmd = [
            "codex", "exec",
            "-C", str(workspace),
            *(["--dangerously-bypass-approvals-and-sandbox"] if perms.get("bypass") and can_edit
              else ["-s", "workspace-write" if can_edit else "read-only"]),
            "-o", out_path,
            "--color", "never",
        ]
        if self.model:
            cmd += ["-m", self.model]
        if can_edit and perms.get("network"):
            cmd += ["-c", "sandbox_workspace_write.network_access=true"]
        if perms.get("auto_review"):
            cmd.append("--approve-for-me")
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
        self.model = ""
        self.calls = 0
        self.last_rate_limit = None

    def run(self, prompt, system, workspace, can_edit, perms=None):
        _run(["sleep", os.environ.get("AI_OFFICE_FAKE_DELAY", "0.3")], "", workspace, 60, self.label)
        asked = ""
        cmd = os.environ.get("AI_OFFICE_FAKE_ASK")
        if cmd and can_edit and perms and perms.get("mcp_config"):
            asked = f"\n(asked to run `{cmd}` → {self._ask_permission(perms, cmd)})"
        self.calls += 1
        if self.key == "claude":
            self.last_rate_limit = {"status": "allowed", "unifiedWindows": {
                "five_hour": {"utilization": 0.1 * self.calls, "resetsAt": None},
                "seven_day": {"utilization": 0.02 * self.calls, "resetsAt": None}}}
        if "# Question from DeepSeek" in prompt:
            q = prompt.split("# Question from DeepSeek (on behalf of the human)\n", 1)[-1]
            return f"[fake] {self.label}'s answer to: {q.strip()[:80]}"
        if can_edit:
            f = Path(workspace) / "fake_output.txt"
            with f.open("a") as fh:
                fh.write(f"{self.label} edit #{self.calls}\n")
            return (f"[fake] {self.label} appended a line to fake_output.txt.{asked}\n"
                    f"REPORT: changed fake_output.txt; no tests; blockers none\nSTATUS: CONTINUE")
        self.reviews = getattr(self, "reviews", 0) + 1
        if self.reviews >= 2:
            return f"[fake] {self.label} reviewed it. Looks complete.\nREPORT: approved; no issues\nSTATUS: DONE"
        return f"[fake] {self.label} reviewed: please add one more line.\nREPORT: needs one more line\nSTATUS: CONTINUE"


def _fake_ask_permission(self, perms, command):
    """Talk to the approval MCP helper exactly like Claude Code would."""
    import sys
    srv = json.loads(perms["mcp_config"])["mcpServers"]["aioffice"]
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "approve", "arguments": {"tool_name": "Bash", "input": {"command": command}}}},
    ]
    p = subprocess.run([srv["command"], *srv["args"]], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       capture_output=True, text=True, env={**os.environ, **srv["env"]}, timeout=600)
    last = json.loads(p.stdout.strip().splitlines()[-1])
    return json.loads(last["result"]["content"][0]["text"])["behavior"]


FakeAgent._ask_permission = _fake_ask_permission


def make_agent(key, cfg, fake=False):
    if fake:
        return FakeAgent(key, {"claude": "Claude", "gpt": "GPT"}[key] + " (fake)")
    c = cfg.get(key, {})
    cls = {"claude": ClaudeAgent, "gpt": GptAgent}[key]
    return cls(model=c.get("model", ""), timeout=c.get("timeout", 1200))
