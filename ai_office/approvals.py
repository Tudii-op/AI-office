"""Permission requests from Claude → rules → DeepSeek → you.

ApprovalBridge listens on a unix socket; approval_mcp.py (started by Claude Code)
forwards each permission prompt here and waits for the answer.
"""

import json
import os
import shlex
import socket
import sys
import tempfile
import threading
from pathlib import Path

MCP_SCRIPT = Path(__file__).resolve().parent / "approval_mcp.py"
PROMPT_TOOL = "mcp__aioffice__approve"


class ApprovalBridge:
    def __init__(self, handler):
        self.handler = handler  # fn(request dict) -> decision dict
        self.dir = tempfile.mkdtemp(prefix="ai-office-")
        self.path = os.path.join(self.dir, "approve.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self.sock.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            try:
                decision = self.handler(json.loads(data))
            except Exception as e:  # never leave Claude hanging
                decision = {"behavior": "deny", "message": f"approval error: {type(e).__name__}: {e}"}
            conn.sendall((json.dumps(decision) + "\n").encode())

    def mcp_config(self, agent, role):
        return json.dumps({"mcpServers": {"aioffice": {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(MCP_SCRIPT)],
            "env": {"AI_OFFICE_APPROVAL_SOCK": self.path, "AI_OFFICE_AGENT": agent, "AI_OFFICE_ROLE": role},
        }}})

    def close(self):
        try:
            self.sock.close()
            os.unlink(self.path)
            os.rmdir(self.dir)
        except OSError:
            pass


def describe(tool_name, tool_input):
    """Short human-readable version of a request."""
    if tool_name == "Bash":
        return f"run: {tool_input.get('command', '')}"
    if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return f"{tool_name.lower()} file: {tool_input.get('file_path') or tool_input.get('notebook_path', '')}"
    if tool_name in ("WebFetch", "WebSearch"):
        return f"{tool_name}: {tool_input.get('url') or tool_input.get('query', '')}"
    return f"{tool_name}: {json.dumps(tool_input)[:200]}"


def rule_for(tool_name, tool_input):
    """The 'always allow' rule for a request, in Claude Code's --allowedTools syntax."""
    if tool_name != "Bash":
        return tool_name
    try:
        words = shlex.split(tool_input.get("command", ""))
    except ValueError:
        words = tool_input.get("command", "").split()
    if not words:
        return None
    prefix = words[:2] if len(words) > 1 and not words[1].startswith(("-", "/", ".", "~")) else words[:1]
    return f"Bash({' '.join(prefix)}:*)"


SHELL_CONTROL = (";", "&&", "||", "|", "`", "$(", ">", "<", "\n", "&")


def matches(rules, tool_name, tool_input):
    """True if a saved rule covers this request. Chained/redirected commands never match."""
    if tool_name == "Bash" and any(c in tool_input.get("command", "") for c in SHELL_CONTROL):
        return False
    for r in rules:
        if r == tool_name:
            return True
        if tool_name == "Bash" and r.startswith("Bash(") and r.endswith(")"):
            pat = r[5:-1]
            cmd = tool_input.get("command", "").strip()
            if pat.endswith(":*"):
                if cmd == pat[:-2] or cmd.startswith(pat[:-2] + " "):
                    return True
            elif cmd == pat:
                return True
    return False
