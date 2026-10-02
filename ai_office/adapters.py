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


def _run_stream(cmd, stdin, cwd, timeout, who, on_line):
    """Like _run, but hands every stdout line to on_line as it arrives (live progress)."""
    if cancelled.is_set():
        raise AgentCancelled(f"{who}: stopped")
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             cwd=cwd, text=True, bufsize=1, start_new_session=True)
    except FileNotFoundError:
        raise AgentError(f"{who}: command not found: {cmd[0]}")
    with _lock:
        _running.add(p)
    err, timed_out = [], []

    def feed():
        try:
            p.stdin.write(stdin)
            p.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def kill():
        timed_out.append(True)
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    threading.Thread(target=feed, daemon=True).start()
    t_err = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
    t_err.start()
    timer = threading.Timer(timeout, kill)
    timer.start()
    out = []
    try:
        for line in p.stdout:
            out.append(line)
            try:
                on_line(line)
            except Exception:
                pass  # a display problem must never break the agent run
        p.wait()
    finally:
        timer.cancel()
        t_err.join(2)
        with _lock:
            _running.discard(p)
    stdout, stderr = "".join(out), "".join(err)
    if timed_out:
        raise AgentError(f"{who}: timed out after {timeout}s")
    if cancelled.is_set():
        raise AgentCancelled(f"{who}: stopped")
    if p.returncode != 0:
        raise AgentError(f"{who} exited with code {p.returncode}\n--- stderr ---\n{stderr.strip()}\n"
                         f"--- stdout (tail) ---\n{stdout.strip()[-3000:]}")
    return subprocess.CompletedProcess(cmd, p.returncode, stdout, stderr)


def _short(text, n=90):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _rel(path, workspace):
    try:
        return str(Path(path).resolve().relative_to(Path(workspace).resolve()))
    except (ValueError, OSError, TypeError):
        return str(path)


def describe_tool(name, inp, workspace):
    """(kind, short text) for one Claude tool call."""
    path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
    if name == "Bash":
        return "command", f"ran {_short(inp.get('command', ''))}"
    if name == "Read":
        return "read", f"read {_rel(path, workspace)}"
    if name == "Write":
        return "edit", f"wrote {_rel(path, workspace)}"
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return "edit", f"edited {_rel(path, workspace)}"
    if name == "Grep":
        return "search", f"searched for “{_short(inp.get('pattern', ''), 50)}”"
    if name == "Glob":
        return "search", f"looked for {_short(inp.get('pattern', ''), 50)}"
    if name == "WebFetch":
        return "web", f"fetched {_short(inp.get('url', ''), 70)}"
    if name == "WebSearch":
        return "web", f"searched the web for “{_short(inp.get('query', ''), 50)}”"
    if name == "TodoWrite":
        return "plan", "updated its todo list"
    if name in ("Task", "Agent"):
        return "other", f"started a sub-agent: {_short(inp.get('description', ''), 50)}"
    return "other", f"used {name}"


class ClaudeAgent:
    key = "claude"
    label = "Claude"

    def __init__(self, model="", timeout=1200):
        self.model = model
        self.timeout = timeout
        self.last_rate_limit = None  # rate_limit_info from the last run

    def run(self, prompt, system, workspace, can_edit, perms=None, on_event=None):
        """perms (from the office): mode, allowed, disallowed, mcp_config (permission prompts → AI Office).
        on_event(kind, text) is called live for each tool call."""
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
        state = {"result": None}

        def on_line(line):
            ev = json.loads(line)
            kind = ev.get("type")
            if kind == "rate_limit_event":
                self.last_rate_limit = ev.get("rate_limit_info") or self.last_rate_limit
            elif kind == "result":
                state["result"] = ev
            elif kind == "assistant" and on_event:
                for c in (ev.get("message") or {}).get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        on_event(*describe_tool(c.get("name", ""), c.get("input") or {}, workspace))

        p = _run_stream(cmd, prompt, workspace, self.timeout, self.label, on_line)
        result = state["result"]
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

    def run(self, prompt, system, workspace, can_edit, perms=None, on_event=None):
        """perms: network, bypass, writable, auto_review. GPT can't route approvals to the chat; the sandbox decides.
        on_event(kind, text) is called live (commands, file changes, searches)."""
        perms = perms or {}
        fd, out_path = tempfile.mkstemp(prefix="ai-office-gpt-", suffix=".txt")
        os.close(fd)
        cmd = [
            "codex", "exec",
            "-C", str(workspace),
            *(["--dangerously-bypass-approvals-and-sandbox"] if perms.get("bypass") and can_edit
              else ["-s", "workspace-write" if can_edit or perms.get("writable") else "read-only"]),
            "-o", out_path,
            "--color", "never",
            "--json",
        ]
        if self.model:
            cmd += ["-m", self.model]
        if can_edit and perms.get("network"):
            cmd += ["-c", "sandbox_workspace_write.network_access=true"]
        if perms.get("auto_review"):
            cmd.append("--approve-for-me")
        cmd.append("-")  # prompt from stdin
        last_message = []

        def on_line(line):
            ev = json.loads(line)
            item = ev.get("item") or {}
            kind, typ = ev.get("type"), item.get("type")
            if typ == "agent_message" and kind == "item.completed":
                last_message.append(item.get("text", ""))
            if not on_event:
                return
            if typ == "command_execution" and kind == "item.started":
                c = item.get("command", "")
                c = c.split(" -lc ", 1)[1].strip("'\"") if " -lc " in c else c
                on_event("command", f"ran {_short(c)}")
            elif typ == "file_change" and kind == "item.completed":
                for ch in item.get("changes") or []:
                    verb = {"add": "created", "delete": "deleted"}.get(ch.get("kind"), "edited")
                    on_event("edit", f"{verb} {_rel(ch.get('path', ''), workspace)}")
            elif typ == "web_search" and kind == "item.started":
                on_event("web", f"searched the web for “{_short(item.get('query', ''), 50)}”")
            elif typ == "mcp_tool_call" and kind == "item.started":
                on_event("other", f"used {item.get('tool') or 'a tool'}")
            elif typ == "todo_list" and kind == "item.started":
                on_event("plan", "made a todo list")

        try:
            _run_stream(cmd, f"{system}\n\n{prompt}", workspace, self.timeout, self.label, on_line)
            text = Path(out_path).read_text().strip()
            return text or (last_message[-1].strip() if last_message else "")
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

    def run(self, prompt, system, workspace, can_edit, perms=None, on_event=None):
        delay = float(os.environ.get("AI_OFFICE_FAKE_DELAY", "0.3"))
        steps = [("read", "read README.md"), ("command", "ran npm test"), ("search", "searched for “TODO”")]
        for kind, text in steps:
            _run(["sleep", str(delay / len(steps))], "", workspace, 60, self.label)
            if on_event:
                on_event(kind, text)
        if on_event and os.environ.get("AI_OFFICE_FAKE_SNEAKY") and not can_edit:
            on_event("edit", "edited app.py")  # a reviewer misbehaving, for tests
            (Path(workspace) / "sneaky.txt").write_text("reviewer was here\n")
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
            long = "".join(f"- detail line {i} about the change\n" for i in range(12)) if os.environ.get("AI_OFFICE_FAKE_LONG") else ""
            return (f"[fake] {self.label} appended a line to fake_output.txt.{asked}\n{long}"
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
