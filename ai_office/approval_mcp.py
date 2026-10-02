"""Tiny MCP server that Claude Code uses as its --permission-prompt-tool.

Claude starts it over stdio. Each permission request is forwarded to the running
AI Office over a unix socket, and the office's decision is returned to Claude.
Standard library only, so it runs with any Python.
"""

import json
import os
import socket
import sys

SOCK = os.environ.get("AI_OFFICE_APPROVAL_SOCK", "")
AGENT = os.environ.get("AI_OFFICE_AGENT", "claude")
ROLE = os.environ.get("AI_OFFICE_ROLE", "")

TOOL = {
    "name": "approve",
    "description": "Ask AI Office (DeepSeek / the human) whether a tool call may run.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "tool_name": {"type": "string"},
            "input": {"type": "object"},
            "tool_use_id": {"type": "string"},
        },
        "required": ["tool_name", "input"],
    },
}


def ask_office(args):
    req = {"agent": AGENT, "role": ROLE, "tool_name": args.get("tool_name", "?"), "input": args.get("input", {})}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(SOCK)
            s.sendall((json.dumps(req) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        decision = json.loads(data)
    except (OSError, json.JSONDecodeError) as e:
        decision = {"behavior": "deny", "message": f"AI Office approval bridge unreachable: {e}"}
    if decision.get("behavior") == "allow":
        decision.setdefault("updatedInput", req["input"])
    return decision


def reply(msg_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:  # notification
            continue
        if method == "initialize":
            reply(msg_id, {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ai-office-approvals", "version": "1"},
            })
        elif method == "tools/list":
            reply(msg_id, {"tools": [TOOL]})
        elif method == "tools/call":
            params = msg.get("params", {})
            if params.get("name") != "approve":
                reply(msg_id, error={"code": -32602, "message": f"unknown tool {params.get('name')}"})
                continue
            decision = ask_office(params.get("arguments", {}))
            reply(msg_id, {"content": [{"type": "text", "text": json.dumps(decision)}]})
        elif method == "ping":
            reply(msg_id, {})
        else:
            reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})


if __name__ == "__main__":
    main()
