"""The office: you ⇄ DeepSeek (manager) ⇄ Claude + GPT (team)."""

import json
import os
import queue
import re
import threading
from datetime import datetime
from pathlib import Path

from . import adapters, approvals, gitops, prompts, safety, ui
from .adapters import AgentCancelled, AgentError
from .deepseek import DeepSeekError
from .usage import Usage

STATUS_RE = re.compile(r"STATUS:\s*(CONTINUE|DONE|ASK_MANAGER|NEED_HUMAN)", re.I)
REPORT_RE = re.compile(r"^\s*REPORT:\s*(.+)$", re.I | re.M)
MAX_RESULT_CHARS = 6000
DEFAULT_ALLOW = ["Bash(ls:*)", "Bash(cat:*)", "Bash(pwd)", "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)"]
READ_TOOLS = ["Read", "Grep", "Glob"]
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".next", "dist", "build", ".cache"}
WEB_TOOLS = ["WebFetch", "WebSearch"]
PERMISSION_MODES = {
    "full": "everything runs; only risky/system stuff (safety.py) is asked",
    "ask": "every command not pre-allowed is approved by DeepSeek or you",
    "auto": "Claude Code's own auto mode decides; nothing is asked",
}
EDIT_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"]

TEAM_INTRO = """You are {me}, the {ROLE} in "AI Office". You work with another AI, {other}, as a coworker; you talk to each other directly in a shared chat.
{boss} You (and {other}) share the current workspace directory."""

BOSS_MANAGER = "Your manager is DeepSeek: it briefs you, answers your questions and reports to the human. The human only talks to DeepSeek."
BOSS_HUMAN = "A human boss gives you the goal and may jump in."

BUILDER_SYSTEM = TEAM_INTRO + """
You create and edit files. {other} is the reviewer and cannot edit files.
Keep replies short: what you did, why, and what you want {other} to check. Don't repeat the history.
If {other} pushes back and you disagree, say so with reasons instead of blindly complying.
End your reply with exactly one final line:
STATUS: CONTINUE    (more work or review needed)
STATUS: DONE        (you believe the goal is complete)
STATUS: {ask} (you need a decision you two can't make; put the question in your reply)"""

REVIEWER_SYSTEM = TEAM_INTRO + """
You can read files but must NOT edit them. {other} is the builder.
Review {other}'s latest work against the goal. Find at least one concrete problem or improvement, or explicitly explain why there is none. Be specific: file, what's wrong, what to change.
Don't just agree to be nice. Keep it short.
End your reply with exactly one final line:
STATUS: CONTINUE    (the builder should change something)
STATUS: DONE        (goal complete, you approve)
STATUS: {ask} (you need a decision you two can't make; put the question in your reply)"""

HELP = """commands:
  /usage           Claude + GPT 5-hour / weekly usage
  /model claude|gpt [name]  change a model (no name = default)
  /swap            swap builder and reviewer
  /turns N         max team turns per round (now {turns})
  /new             new session (clear the team chat)
  /permissions [full|ask|auto]  how much Claude/GPT may do without asking
  /approvals [clear]  show (or clear) permission settings and your "always" rules
  /forget          clear DeepSeek's memory of your chat (kept across restarts otherwise)
  /deepseek on|off on: you talk to DeepSeek, it runs the team (now {ds})
                   off: you talk to the team directly
  /status          roles, models, settings
  /quit            leave
With DeepSeek on, just ask in plain words: settings, usage, "ask GPT ...", "check the files".
Instructions for every AI are editable in AI-office/prompts/."""


CLAUDE_MODELS = ["fable", "opus", "sonnet", "haiku"]
CODEX_MODELS_CACHE = Path.home() / ".codex" / "models_cache.json"


def gpt_models():
    try:
        data = json.loads(CODEX_MODELS_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [m["slug"] for m in data.get("models", []) if m.get("visibility") == "list" and m.get("slug")]


class Office:
    def __init__(self, agents, workspace, cfg, deepseek=None, log_dir=None, state_dir=None, workspaces_dir=None):
        self.agents = agents  # {"claude": agent, "gpt": agent}
        self.workspace = Path(workspace)
        self.workspaces_dir = Path(workspaces_dir or self.workspace.parent)
        self.state_dir = Path(state_dir or "state")
        self.usage = Usage(self.state_dir / "usage.json")
        self.models = {"claude": cfg.get("claude", {}).get("models", CLAUDE_MODELS), "gpt": gpt_models()}
        self.notes = []  # action results / warnings for DeepSeek's next call
        self.jobs = queue.Queue()  # your messages while idle → worker thread
        self.inbox = queue.Queue()  # your messages while the team works → next turn
        self.busy = False
        self.team_waiting = False
        self.interactive = False  # set by the TUI; without it nobody can answer approvals
        perm = cfg.get("permissions", {})
        self.perm = {
            "mode": perm.get("mode", "full"),
            "approver": perm.get("approver", "deepseek"),
            "reviewer_can_run": perm.get("reviewer_can_run", True),
            "claude_allow": perm.get("claude_allow", DEFAULT_ALLOW),
            "gpt_network": perm.get("gpt_network", False),
            "gpt_auto_review": perm.get("gpt_auto_review", False),
            "gpt_unsandboxed": perm.get("gpt_unsandboxed", False),
        }
        self.pending = None  # {"req", "event", "answer"} while waiting for your y/a/n
        self.bridge = approvals.ApprovalBridge(self.handle_permission)
        loop = cfg.get("loop", {})
        self.max_turns = loop.get("max_turns", 8)
        self.auto_commit = loop.get("auto_commit", "sandbox")  # sandbox | always | never
        self.recap_every = loop.get("recap_every", 4)
        self.keep_recent = loop.get("keep_recent", 2)
        self.no_recap_window = loop.get("no_recap_window", 10)
        self.manager_window = loop.get("manager_window", 16)
        self.max_handoffs = loop.get("max_handoffs", 3)
        self.max_manager_steps = loop.get("max_manager_steps", 8)
        self.live_every = loop.get("live_summary_every", 45)  # seconds; 0 = off
        self.roles = {"builder": loop.get("builder", "claude"), "reviewer": loop.get("reviewer", "gpt")}
        self.deepseek = deepseek
        self.ds_on = deepseek is not None
        self.history = []  # team chat: [{"who", "text", "private"}]
        self.mgr_history = []  # DeepSeek's own conversation (role/content)
        self.recap = ""
        self.recap_upto = 0
        self.goal = ""
        self.resume_role = "builder"
        self.log_dir = Path(log_dir or "logs")
        self.load_settings()
        self.load_manager_memory()
        self.usage.update_gpt()  # free: read Codex's last known limits
        self.start_log()

    # ---------- settings & sessions ----------

    def load_settings(self):
        try:
            s = json.loads((self.state_dir / "settings.json").read_text())
        except (OSError, json.JSONDecodeError):
            return
        for k, m in s.get("models", {}).items():
            if k in self.agents:
                self.agents[k].model = m
        if sorted(s.get("roles", {}).values()) == ["claude", "gpt"]:
            self.roles = s["roles"]
        self.max_turns = s.get("max_turns", self.max_turns)
        if s.get("permission_mode") in PERMISSION_MODES:
            self.perm["mode"] = s["permission_mode"]

    def load_rules(self):
        try:
            return json.loads((self.state_dir / "approvals.json").read_text())
        except (OSError, json.JSONDecodeError):
            return []

    def save_rules(self, rules):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "approvals.json").write_text(json.dumps(sorted(set(rules)), indent=1))

    # ---------- permissions ----------

    def perms_for(self, key, role):
        """Permission settings for one call. role: builder | reviewer | consult."""
        mode, can_edit = self.perm["mode"], role == "builder"
        if key == "gpt":  # codex can't forward approvals: its sandbox is the safety net
            return {"network": mode == "full" or self.perm["gpt_network"],
                    "bypass": mode == "full" and self.perm["gpt_unsandboxed"],
                    # reviewer/consult may write (test caches etc.); edits to project files are flagged
                    "writable": can_edit or self.perm["reviewer_can_run"],
                    "auto_review": self.perm["gpt_auto_review"]}
        allowed = list(self.perm["claude_allow"]) + self.load_rules() + READ_TOOLS
        if mode == "full":
            allowed += WEB_TOOLS
        if not can_edit and not self.perm["reviewer_can_run"]:
            return {"mode": "dontAsk", "allowed": READ_TOOLS, "disallowed": EDIT_TOOLS}
        if mode == "auto":
            return {"mode": "auto", "allowed": allowed, "disallowed": [] if can_edit else EDIT_TOOLS}
        # "manual" even for the builder: Claude Code would auto-accept edits anywhere under the
        # workspace (incl. your dotfiles when it's ~); our handler approves safe edits instantly instead
        return {"mode": "manual", "allowed": allowed,
                "disallowed": [] if can_edit else EDIT_TOOLS,
                "mcp_config": self.bridge.mcp_config(key, role), "prompt_tool": approvals.PROMPT_TOOL}

    def set_permission_mode(self, mode):
        if mode not in PERMISSION_MODES:
            raise ValueError(f"mode must be one of {', '.join(PERMISSION_MODES)}")
        self.perm["mode"] = mode
        self.save_settings()
        return f"permissions → {mode}: {PERMISSION_MODES[mode]}"

    def handle_permission(self, req):
        """Runs on the bridge thread while Claude waits. Returns a Claude permission decision."""
        tool, tin = req.get("tool_name", "?"), req.get("input") or {}
        who = f"{self.label(req.get('agent', 'claude'))} ({req.get('role') or '?'})"
        what = approvals.describe(tool, tin)
        allow = {"behavior": "allow", "updatedInput": tin}
        if approvals.matches(self.load_rules(), tool, tin):
            self.system(f"🔐 {who} {what} → allowed by your rule")
            return allow
        if adapters.cancelled.is_set():
            return {"behavior": "deny", "message": "The human stopped the team."}
        risk = ""
        if tool in EDIT_TOOLS and req.get("role") == "builder" and self.perm["mode"] == "ask":
            if not safety.check(tool, tin, self.workspace):
                return allow  # ask mode: edits in the project are fine, commands get approved
        if self.perm["mode"] == "full":
            risk = safety.check(tool, tin, self.workspace)
            if not risk:
                with self.log_path.open("a") as f:
                    f.write(f"\n> 🔓 {who} {what}\n")
                return allow
            ui.note(f"⚠ risky: {risk}")
        if self.perm["approver"] == "deepseek" and self.deepseek is not None and self.ds_on:
            ui.note(f"DeepSeek is checking: {who} wants to {what}")
            ctx = (f"agent: {who}\nworkspace: {self.workspace}\ngoal: {self.goal or '(none)'}\n"
                   f"request: {what}\n" + (f"flagged as risky: {risk}\n" if risk else "") +
                   f"raw: {json.dumps({'tool': tool, 'input': tin})[:2000]}")
            try:
                d = self.deepseek.approve(ctx)
            except DeepSeekError as e:
                ui.say("error", str(e))
                d = {"decision": "ask_human", "reason": "DeepSeek couldn't decide"}
            if d["decision"] == "allow":
                self.system(f"🔐 {who} {what} → DeepSeek allowed ({d.get('reason', '')})")
                return allow
            if d["decision"] == "deny":
                self.system(f"🔐 {who} {what} → DeepSeek denied ({d.get('reason', '')})")
                return {"behavior": "deny", "message": f"Denied by DeepSeek (manager): {d.get('reason', '')}"}
            reason = d.get("reason", "")
        else:
            reason = ""
        return self.ask_human_permission(who, what, tool, tin, reason, risk)

    def ask_human_permission(self, who, what, tool, tin, reason, risk=""):
        if not self.interactive:
            self.system(f"🔐 {who} {what} → denied (nobody to ask in this mode)")
            return {"behavior": "deny", "message": "No human available to approve this."}
        rule = approvals.rule_for(tool, tin)
        self.pending = {"event": threading.Event(), "answer": None}
        text = (f"🔐 {who} wants to {what}" + (f"\n   ⚠ flagged: {risk}" if risk else "") +
                (f"\n   DeepSeek: {reason}" if reason else ""))
        hint = "" if ui.has_sink() else f"\n   [y] allow  [a] always ({rule or 'n/a'})  [n] deny  · or type a reason to deny"
        ui.say("system", text + hint)
        ui.request_approval(text, rule)
        while not self.pending["event"].wait(0.2):
            if adapters.cancelled.is_set():
                self.pending = None
                return {"behavior": "deny", "message": "The human stopped the team."}
        answer, self.pending = self.pending["answer"], None
        low = answer.lower()
        if low in ("y", "yes", "allow"):
            self.system(f"🔐 allowed once")
            return {"behavior": "allow", "updatedInput": tin}
        if low in ("a", "always") and rule:
            self.save_rules(self.load_rules() + [rule])
            self.system(f"🔐 allowed; saved rule {rule}")
            return {"behavior": "allow", "updatedInput": tin}
        msg = "Denied by the human." if low in ("n", "no", "deny", "") else f"Denied by the human: {answer}"
        self.system(f"🔐 {msg}")
        return {"behavior": "deny", "message": msg}

    @property
    def approval_prompt(self):
        return "🔐 [y]es [a]lways [n]o › " if self.pending else None

    def load_manager_memory(self):
        try:
            self.mgr_history = json.loads((self.state_dir / "manager_history.json").read_text())
        except (OSError, json.JSONDecodeError):
            self.mgr_history = []

    def save_manager_memory(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.mgr_history = self.mgr_history[-200:]
        (self.state_dir / "manager_history.json").write_text(json.dumps(self.mgr_history, indent=1))

    def forget_chat(self):
        self.mgr_history = []
        self.save_manager_memory()
        return "DeepSeek's memory of your chat is cleared"

    def save_settings(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "settings.json").write_text(json.dumps({
            "models": {k: a.model for k, a in self.agents.items()},
            "roles": self.roles,
            "max_turns": self.max_turns,
            "permission_mode": self.perm["mode"],
        }, indent=2))

    def start_log(self):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp, n = f"{datetime.now():%Y-%m-%d_%H-%M-%S}", 1
        self.log_path = self.log_dir / f"{stamp}.md"
        while self.log_path.exists():
            n += 1
            self.log_path = self.log_dir / f"{stamp}_{n}.md"
        self.log_path.write_text(f"# AI Office session\n\nworkspace: `{self.workspace}`\n")

    def new_session(self):
        self.history, self.recap, self.recap_upto = [], "", 0
        self.goal, self.resume_role, self.team_waiting = "", "builder", False
        self.start_log()
        return f"new session; log: {self.log_path.name}"

    def set_workspace(self, path):
        raw = str(path).strip()
        if not raw:
            raise ValueError("empty path")
        if "/" in raw or raw.startswith("~"):
            p = Path(raw).expanduser().resolve()
            if not p.is_dir():
                raise ValueError(f"{p} doesn't exist (only workspaces/<name> folders get created automatically)")
        else:
            p = (self.workspaces_dir / raw).resolve()
            p.mkdir(parents=True, exist_ok=True)
        self.workspace = p
        if self.is_sandbox():
            gitops.ensure_repo(p)
        return f"workspace is now {p}"

    def _inside_workspace(self, rel):
        p = (self.workspace / str(rel or ".")).resolve()
        if p != self.workspace.resolve() and self.workspace.resolve() not in p.parents:
            raise ValueError(f"{rel!r} is outside the workspace")
        return p

    def lookup(self, do, a):
        """Read-only peeks for DeepSeek. Returns text (truncated)."""
        if do == "git_log":
            n = max(1, min(int(a.get("n") or 10), 50))
            out = gitops.log(self.workspace, n) or "(no commits yet)"
        elif do == "list_files":
            p = self._inside_workspace(a.get("path"))
            if not p.is_dir():
                raise ValueError(f"{a.get('path')!r} is not a folder (try find)")
            items, level = [], [p]
            for depth in range(2):  # breadth-first: the whole top level before going deeper
                nxt = []
                for d in level:
                    try:
                        entries = sorted(d.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
                    except OSError:
                        continue
                    for f in entries:
                        if f.name in SKIP_DIRS or (f.name.startswith(".") and f.is_dir()):
                            continue
                        rel = f.relative_to(self.workspace)
                        items.append(f"{rel}/" if f.is_dir() else f"{rel}  ({f.stat().st_size if f.exists() else 0} B)")
                        if f.is_dir():
                            nxt.append(f)
                level = nxt
                if len(items) >= 300:
                    break
            if len(items) > 300:
                items = items[:300] + ["… (truncated; list a subfolder or use find)"]
            out = "\n".join(items) or "(empty)"
        elif do == "find":
            name = str(a.get("name", "")).strip().lower()
            if not name:
                raise ValueError("find needs a name")
            hits = []
            for root, dirs, files in os.walk(self.workspace):
                if len(Path(root).relative_to(self.workspace).parts) >= 5:
                    dirs[:] = []
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
                for n in dirs + files:
                    if name in n.lower():
                        f = Path(root) / n
                        hits.append(f"{f.relative_to(self.workspace)}{'/' if f.is_dir() else ''}")
                if len(hits) >= 50:
                    break
            out = "\n".join(hits[:50]) or f"nothing named like {name!r} (searched 5 levels deep)"
        else:  # read_file
            p = self._inside_workspace(a.get("path"))
            if not p.is_file():
                raise ValueError(f"{a.get('path')!r} is not a file")
            out = p.read_text(errors="replace")
        return out if len(out) <= MAX_RESULT_CHARS else out[:MAX_RESULT_CHARS] + "\n… (truncated)"

    def is_sandbox(self):
        ws, root = self.workspace.resolve(), self.workspaces_dir.resolve()
        return root in ws.parents

    def commits_enabled(self):
        if self.auto_commit == "never":
            return False
        if self.auto_commit == "sandbox":
            return self.is_sandbox()
        return gitops.is_repo_root(self.workspace) and self.workspace.resolve() != Path.home()

    def model_name(self, key):
        return self.agents[key].model or "default"

    def office_state(self):
        labels = {k: self.label(k) for k in self.agents}
        sessions = sorted(self.log_dir.glob("*.md"))[-5:]
        return "\n".join([
            f"mode: {'human ⇄ DeepSeek ⇄ team' if self.ds_on else 'human ⇄ team'}",
            f"roles: builder={self.label(self.roles['builder'])}, reviewer={self.label(self.roles['reviewer'])}",
            f"models: Claude={self.model_name('claude')}, GPT={self.model_name('gpt')}",
            f"available models: claude: {', '.join(self.models['claude']) or '?'} (or a full claude-* id); "
            f"gpt: {', '.join(self.models['gpt']) or '?'}",
            f"max turns per round: {self.max_turns}",
            f"permissions: {self.perm['mode']} ({PERMISSION_MODES[self.perm['mode']]})",
            f"workspace: {self.workspace}",
            f"current session log: {self.log_path.name}; recent sessions: {', '.join(p.name for p in sessions)}",
            f"usage: {self.usage.summary(labels)}",
        ])

    def apply_actions(self, actions, by="DeepSeek"):
        for a in actions or []:
            do = a.get("do") if isinstance(a, dict) else None
            try:
                if do == "set_model":
                    key, model = a.get("agent"), str(a.get("model", "")).strip()
                    if key not in self.agents:
                        raise ValueError(f"unknown agent {key!r}")
                    ok = self.models[key] or [model]
                    if model and model not in ok and not (key == "claude" and model.startswith("claude-")):
                        raise ValueError(f"{model!r} isn't an available {key} model")
                    self.agents[key].model = model
                    result = f"{self.label(key)} model → {model or 'default'}"
                elif do == "swap_roles":
                    self.roles = {"builder": self.roles["reviewer"], "reviewer": self.roles["builder"]}
                    result = f"builder: {self.label(self.roles['builder'])}, reviewer: {self.label(self.roles['reviewer'])}"
                elif do == "set_turns":
                    n = int(a.get("n"))
                    if not 1 <= n <= 50:
                        raise ValueError("turns must be 1-50")
                    self.max_turns = n
                    result = f"max turns → {n}"
                elif do == "new_session":
                    result = self.new_session()
                elif do == "set_workspace":
                    result = self.set_workspace(a.get("path", ""))
                elif do == "set_permissions":
                    result = self.set_permission_mode(a.get("mode"))
                elif do == "forget_chat":
                    result = self.forget_chat()
                elif do in ("list_files", "read_file", "git_log", "find"):
                    arg = a.get("path") or a.get("name") or ""
                    ui.note(f"{by} looks up: {do} {arg}".rstrip())
                    self.notes.append(f"{do} {arg}:\n{self.lookup(do, a)}")
                    continue
                else:
                    raise ValueError(f"unknown action {a!r}")
                self.save_settings()
                self.system(f"{by}: {result}")
                self.notes.append(f"action ok: {result}")
            except (ValueError, TypeError) as e:
                self.system(f"{by}: action failed: {e}")
                self.notes.append(f"action FAILED ({do}): {e}")

    # ---------- chat ----------

    def label(self, who):
        return ui.STYLE.get(who, ("", who, ""))[1]

    def role_of(self, key):
        if key == "deepseek":
            return "manager"
        return next((r for r, k in self.roles.items() if k == key), None)

    def add(self, who, text, tag="", private=False, report=""):
        """private = shown to you and logged, but not part of the team's chat."""
        self.history.append({"who": who, "text": text, "private": private, "report": report})
        ui.say(who, text, tag)
        with self.log_path.open("a") as f:
            f.write(f"\n## {self.label(who)}{' · ' + tag if tag else ''}\n\n{text}\n")

    def system(self, text):
        ui.say("system", text)
        with self.log_path.open("a") as f:
            f.write(f"\n> {text}\n")

    def transcript(self, msgs):
        out = []
        for m in msgs:
            if m["private"]:
                continue
            role = self.role_of(m["who"])
            name = self.label(m["who"]) + (f" ({role})" if role else "")
            out.append(f"[{name}]:\n{m['text']}")
        return "\n\n".join(out)

    def manager_context(self):
        """Team status for DeepSeek: REPORT lines instead of full messages (the latest one in full)."""
        if not self.goal:
            return "(no task yet; the team is idle)"
        recent = [m for m in self.history[self.recap_upto:] if not m["private"]]
        lines = []
        for i, m in enumerate(recent):
            role = self.role_of(m["who"])
            name = self.label(m["who"]) + (f" ({role})" if role else "")
            last = i == len(recent) - 1
            if m.get("report") and not last:
                status = STATUS_RE.findall(m["text"])
                lines.append(f"[{name}] REPORT: {m['report']}" + (f" · STATUS: {status[-1].upper()}" if status else ""))
            else:
                lines.append(f"[{name}]{' (latest, full)' if last else ''}:\n{m['text']}")
        parts = [f"# Goal\n{self.goal}"]
        if self.recap:
            parts.append(f"# Recap of earlier conversation\n{self.recap}")
        parts.append("# Recent team messages\n" + "\n\n".join(lines))
        return "\n\n".join(parts)

    def context(self):
        if self.ds_on:
            recent = self.history[self.recap_upto:]
        else:
            recent = self.history[-self.no_recap_window:]
        parts = [f"# Goal\n{self.goal}"]
        if self.ds_on and self.recap:
            parts.append(f"# Recap of earlier conversation\n{self.recap}")
        parts.append(f"# Recent messages\n{self.transcript(recent)}")
        return "\n\n".join(parts)

    def maybe_recap(self):
        if not self.ds_on:
            return
        unrecapped = len(self.history) - self.recap_upto
        if unrecapped < self.recap_every + self.keep_recent:
            return
        cut = len(self.history) - self.keep_recent
        ui.note("DeepSeek is writing a recap")
        try:
            self.recap = self.deepseek.recap(self.recap, self.transcript(self.history[self.recap_upto:cut]))
            self.recap_upto = cut
            with self.log_path.open("a") as f:
                f.write(f"\n<details><summary>recap</summary>\n\n{self.recap}\n\n</details>\n")
        except DeepSeekError as e:
            ui.say("error", str(e))

    # ---------- manager (DeepSeek) ----------

    def ask_manager(self, event):
        team = self.manager_context()
        msg = f"# Office state\n{self.office_state()}\n\n# Team status\n{team}\n\n# What just happened\n{event}"
        if self.notes:
            msg += "\n\n# Results of your last actions\n" + "\n".join(self.notes)
            self.notes = []
        messages = self.mgr_history[-self.manager_window:] + [{"role": "user", "content": msg}]
        ui.note("DeepSeek is thinking")
        try:
            d = self.deepseek.manage(messages)
        except DeepSeekError as e:
            ui.say("error", f"{e}\n(use /deepseek off to talk to the team directly)")
            return None
        # keep DeepSeek's own memory light: the event, not the whole team status
        self.mgr_history += [{"role": "user", "content": event}, {"role": "assistant", "content": json.dumps(d)}]
        self.save_manager_memory()
        return d

    def manager_loop(self, event):
        handoffs = steps = 0
        while True:
            steps += 1
            if steps > self.max_manager_steps:
                self.system(f"DeepSeek took {self.max_manager_steps} steps without answering you; stopping here.")
                return
            d = self.ask_manager(event)
            if d is None:
                return
            self.apply_actions(d.get("actions"))
            to, msg = d["to"], str(d.get("message", "")).strip()
            if to == "human":
                self.add("deepseek", msg, tag="→ you", private=True)
                return
            if to == "office":
                if msg:
                    ui.note(f"DeepSeek: {msg}")
                event = "(lookup) Your actions ran; see the results below. Continue."
                continue
            if to in self.agents:
                event = self.consult(to, msg)
                if event is None or adapters.cancelled.is_set():
                    return
                continue
            # to == "team"
            handoffs += 1
            if handoffs > self.max_handoffs:
                self.system(f"DeepSeek handed work to the team {self.max_handoffs} times in a row; stopping here.")
                return
            new_task = d.get("new_task", not self.goal)
            self.add("deepseek", msg, tag="→ team" + (" · new task" if new_task else ""))
            event, _ = self.run_team(msg, new_task)
            if adapters.cancelled.is_set():
                return

    # ---------- team (Claude ⇄ GPT) ----------

    def call_agent(self, key, prompt, system, role, doing):
        """Run Claude or GPT once: live actions, DeepSeek narration, usage, read-only check.
        Raises AgentError/AgentCancelled."""
        label = self.label(key)
        five_h = self.usage.pct(key)
        if five_h is not None and five_h >= 90:
            ui.say("error", f"{label} is at {five_h}% of its 5-hour limit")
        ui.note(f"{label} ({self.model_name(key)}) is {doing}")
        started = datetime.now().timestamp()
        agent = self.agents[key]
        live = {"actions": [], "done": threading.Event(), "warned": set()}
        before = gitops.status_snapshot(self.workspace) if role != "builder" else None

        def on_event(kind, text):
            live["actions"].append(text)
            ui.set_activity(f"{label} › {text}")
            with self.log_path.open("a") as f:
                f.write(f"\n> ▸ {label}: {text}\n")
            if kind == "edit" and role != "builder" and text not in live["warned"]:
                live["warned"].add(text)
                ui.say("error", f"{label} ({role}) {text}, but it's supposed to be read-only")
                self.notes.append(f"WARNING: the {role} {label} {text} although it must not edit files.")

        narrator = None
        if self.ds_on and self.live_every > 0:
            narrator = threading.Thread(target=self._narrate, args=(key, role, live), daemon=True)
            narrator.start()
        try:
            return agent.run(prompt, system, self.workspace, can_edit=(role == "builder"),
                             perms=self.perms_for(key, role), on_event=on_event)
        finally:
            live["done"].set()
            if key == "claude":
                self.usage.update_claude(agent.last_rate_limit)
            else:
                self.usage.update_gpt(since=started)
            if before is not None:
                changed = gitops.status_snapshot(self.workspace) - before
                if changed:
                    files = ", ".join(sorted(line[3:] for line in changed)[:8])
                    ui.say("error", f"{label} ({role}) changed project files although it's read-only: {files}")
                    self.notes.append(f"WARNING: {label} ({role}) changed files while read-only: {files}")

    def _narrate(self, key, role, live):
        """Every live_every seconds, DeepSeek turns new actions into one line for you."""
        seen, last_line = 0, ""
        while not live["done"].wait(self.live_every):
            new = live["actions"][seen:]
            if not new:
                continue
            seen += len(new)
            ctx = (f"agent: {self.label(key)} ({role})\ngoal: {self.goal or '(answering a question)'}\n"
                   f"previous update: {last_line or '(none)'}\nrecent actions (oldest first):\n- " +
                   "\n- ".join(live["actions"][-25:]))
            try:
                line = self.deepseek.narrate(ctx)
            except DeepSeekError:
                continue
            if line and not live["done"].is_set():
                last_line = line
                ui.say("deepseek", line, tag="live")

    def take_turn(self, role):
        key = self.roles[role]
        other = self.label(self.roles["reviewer" if role == "builder" else "builder"])
        me = self.label(key)
        boss = prompts.load("boss_manager" if self.ds_on else "boss_human")
        system = prompts.load("team", me=me, other=other, role=role.upper(), boss=boss) + "\n\n" + \
            prompts.load(role, me=me, other=other, ask="ASK_MANAGER" if self.ds_on else "NEED_HUMAN")
        prompt = f"{self.context()}\n\n# Your turn\nYou are {me} ({role}). Respond now."
        text = self.call_agent(key, prompt, system, role, doing=f"working as {role}")
        m = STATUS_RE.findall(text)
        status = m[-1].upper() if m else "CONTINUE"
        r = REPORT_RE.findall(text)
        self.add(key, text.strip(), tag=role, report=r[-1].strip() if r else "")
        sha = gitops.commit_turn(self.workspace, me, text) if self.commits_enabled() else None
        if sha:
            self.system(f"committed {sha}")
        return "ASK" if status in ("ASK_MANAGER", "NEED_HUMAN") else status

    def consult(self, key, question):
        """DeepSeek asks one agent a question (read-only, no team task). Returns the event for DeepSeek."""
        self.add("deepseek", question, tag=f"→ {self.label(key)}", private=True)
        team = self.context() if self.goal else "(no team task right now)"
        prompt = f"# Context: current team work\n{team}\n\n# Question from DeepSeek (on behalf of the human)\n{question}"
        try:
            text = self.call_agent(key, prompt, prompts.load("consult", me=self.label(key)), "consult",
                                   doing="answering DeepSeek")
        except AgentCancelled:
            return None
        except AgentError as e:
            ui.say("error", str(e))
            return f"{self.label(key)} failed to answer:\n{e}"
        self.add(key, text.strip(), tag="→ DeepSeek", private=True)
        return f"{self.label(key)} answered your question:\n{text.strip()}"

    def decide_next(self, last_role, status):
        if status == "ASK":
            return "ask", "asked a question"
        if last_role == "reviewer" and status == "DONE":
            return "done", "reviewer approved"
        fallback = "reviewer" if last_role == "builder" else "builder"
        if not self.ds_on:
            return fallback, ""
        ui.note("DeepSeek is deciding who's next")
        try:
            d = self.deepseek.route(self.manager_context())
        except DeepSeekError as e:
            ui.say("error", f"{e}\n(falling back to simple turn-taking)")
            return fallback, ""
        nxt = {"manager": "ask"}.get(d["next"], d["next"])
        return nxt, d.get("reason", "")

    def run_team(self, message, new_task):
        """Run Claude ⇄ GPT until they finish or need someone.
        Returns (event text for whoever is in charge, waiting) where waiting=True means
        the team is mid-task and expects an answer."""
        if new_task:
            self.goal = message
            self.resume_role = "builder"
        role, turns = self.resume_role, 0
        self.team_waiting = True
        while turns < self.max_turns:
            for line in self.take_inbox():
                if self.ds_on:
                    self.add("human", line, tag="interjected → DeepSeek", private=True)
                    self.resume_role = role
                    return f"The human interjected while the team worked: {line}", True
                self.add("human", line, tag="interjected")
            try:
                status = self.take_turn(role)
            except AgentCancelled:
                self.resume_role = role
                return "The human stopped the team.", True
            except AgentError as e:
                ui.say("error", str(e))
                self.resume_role = role
                return f"{self.label(self.roles[role])} ({role}) crashed with an error:\n{e}", True
            turns += 1
            self.maybe_recap()
            nxt, reason = self.decide_next(role, status)
            if reason:
                ui.note(f"next: {nxt} ({reason})")
            if nxt == "done":
                self.team_waiting = False
                self.system(f"team finished after {turns} turns ({reason})")
                return f"The team finished after {turns} turns ({reason}). Report to the human.", False
            if nxt == "ask":
                self.resume_role = role
                who = f"{self.label(self.roles[role])} ({role})"
                return f"{who} needs input ({reason}). See their last message in the team chat.", True
            role = nxt
        self.resume_role = role
        self.system(f"team hit the {self.max_turns}-turn limit")
        return f"The team hit the {self.max_turns}-turn limit without finishing.", True

    # ---------- your messages ----------

    def take_inbox(self):
        lines = []
        while not self.inbox.empty():
            lines.append(self.inbox.get_nowait())
        return lines

    def on_human(self, text):
        if self.ds_on:
            self.add("human", text, tag="→ DeepSeek", private=True)
            self.manager_loop(f"The human says: {text}")
            return
        # DeepSeek off: you are the team's boss
        self.add("human", text, tag="→ team")
        new_task = not self.goal
        if self.goal and not self.team_waiting:
            self.goal = f"{self.goal}\n\nLatest from human: {text}"
        event, waiting = self.run_team(text, new_task)
        if waiting and not adapters.cancelled.is_set():
            self.system(f"{event}\nReply to continue, or /new for a fresh start.")

    def work(self, text):
        """One unit of work for the worker thread."""
        adapters.cancelled.clear()
        self.busy = True
        try:
            self.on_human(text)
        except Exception as e:  # never let the worker die silently
            ui.say("error", f"{type(e).__name__}: {e}")
        finally:
            self.busy = False
            ui.idle()
            if adapters.cancelled.is_set():
                self.system("stopped. Your next message continues from here.")
                self.notes.append("The human pressed stop; the team is paused mid-task.")
                adapters.cancelled.clear()

    def _worker(self):
        while True:
            text = self.jobs.get()
            if text is None:
                return
            self.work(text)

    # ---------- UI hooks ----------

    def start(self, show_status=True):
        if self.is_sandbox() and gitops.ensure_repo(self.workspace):
            self.system(f"initialized git in {self.workspace}")
        if self.workspace.resolve() == Path.home():
            self.system("workspace is your home folder: the AIs can work anywhere in ~. Your config/dotfiles, "
                        "secrets and whole top-level folders are still protected for Claude (asked first). "
                        "GPT's sandbox covers all of ~ and can't ask first.")
        if show_status:
            self.handle_command("/status")
        self.system("type a message · /help for keys and commands")
        threading.Thread(target=self._worker, daemon=True).start()

    def stop(self):
        self.cancel()
        if self.bridge:
            self.bridge.close()
        self.jobs.put(None)
        self.system(f"bye. log saved to {self.log_path}")

    def cancel(self):
        if self.busy:
            ui.note("stopping…")
            adapters.cancel_running()

    def submit(self, text, wait=False):
        if self.pending and not text.startswith("/"):
            self.pending["answer"] = text.strip()
            self.pending["event"].set()
            return
        if text.startswith("/"):
            cmd = text.split()[0]
            if self.busy and cmd in ("/new", "/deepseek", "/forget"):
                self.system(f"{cmd} can't run while the team works. Press Esc to stop them first.")
            else:
                self.handle_command(text)
            return
        if wait:  # plain (piped) mode: run synchronously
            self.work(text)
        elif self.busy:
            self.inbox.put(text)
            ui.note("📨 queued, delivered before the next turn")
        else:
            self.busy = True  # set now so the status line/Esc react immediately
            self.jobs.put(text)

    def completions(self, words):
        if words[0] == "/model":
            if len(words) == 1:
                return [(k, f"{self.label(k)} · now {self.model_name(k)}") for k in self.agents]
            if len(words) == 2 and words[1] in self.models:
                return [(m, "") for m in self.models[words[1]]]
        if words[0] == "/permissions" and len(words) == 1:
            return [(m, d) for m, d in PERMISSION_MODES.items()]
        if words[0] == "/deepseek" and len(words) == 1:
            return [("on", "you talk to DeepSeek"), ("off", "you talk to the team")]
        return []

    def handle_command(self, cmd):
        parts = cmd.split()
        if parts[0] == "/swap":
            self.roles = {"builder": self.roles["reviewer"], "reviewer": self.roles["builder"]}
            self.save_settings()
            self.system(f"builder: {self.label(self.roles['builder'])}, reviewer: {self.label(self.roles['reviewer'])}")
        elif parts[0] == "/turns" and len(parts) == 2 and parts[1].isdigit():
            self.max_turns = int(parts[1])
            self.save_settings()
            self.system(f"max turns: {self.max_turns}")
        elif parts[0] == "/usage":
            self.usage.update_gpt()
            self.system(self.usage.render({k: self.label(k) for k in self.agents}))
        elif parts[0] == "/model" and len(parts) in (2, 3) and parts[1] in self.agents:
            self.apply_actions([{"do": "set_model", "agent": parts[1], "model": parts[2] if len(parts) == 3 else ""}], by="you")
        elif parts[0] == "/new":
            self.system(self.new_session())
        elif parts[0] == "/permissions":
            if len(parts) == 2:
                try:
                    self.system(self.set_permission_mode(parts[1]))
                except ValueError as e:
                    self.system(str(e))
            else:
                self.system("\n".join(f"{'▶' if m == self.perm['mode'] else ' '} {m:5} {d}" for m, d in PERMISSION_MODES.items()))
        elif parts[0] == "/approvals":
            if len(parts) == 2 and parts[1] == "clear":
                self.save_rules([])
                self.system("saved approval rules cleared")
            else:
                rules = self.load_rules()
                self.system(
                    f"permissions: {self.perm['mode']} · first approver: {self.perm['approver']} · "
                    f"reviewer can run commands: {self.perm['reviewer_can_run']} · GPT network: {self.perm['gpt_network']}\n"
                    f"built-in allow: {', '.join(self.perm['claude_allow'])}\n"
                    f"your 'always' rules: {', '.join(rules) or '(none)'}   (/approvals clear to remove)")
        elif parts[0] == "/forget":
            self.system(self.forget_chat())
        elif parts[0] == "/deepseek" and len(parts) == 2:
            if self.deepseek is None:
                self.system("DeepSeek isn't configured: put DEEPSEEK_API_KEY in .env (or enable it in config.toml)")
            else:
                self.ds_on = parts[1] == "on"
                self.system("you now talk to DeepSeek" if self.ds_on else "you now talk to the team directly")
        elif parts[0] == "/status":
            mode = "you ⇄ DeepSeek ⇄ team" if self.ds_on else "you ⇄ team (DeepSeek off)"
            self.system(
                f"{mode}\nbuilder: {self.label(self.roles['builder'])} · reviewer: {self.label(self.roles['reviewer'])} · "
                f"turns: {self.max_turns} · permissions: {self.perm['mode']}\nmodels: Claude={self.model_name('claude')} · GPT={self.model_name('gpt')}\n"
                f"workspace: {self.workspace} · auto-commit: {'on' if self.commits_enabled() else 'off'}\nlog: {self.log_path}"
            )
        elif parts[0] == "/help":
            self.system(HELP.format(turns=self.max_turns, ds="on" if self.ds_on else "off") + "\n\n" + ui.KEYS_HELP)
        else:
            self.system(f"unknown command: {cmd}  (try /help)")
