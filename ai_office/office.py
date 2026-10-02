"""The office: you ⇄ DeepSeek (manager) ⇄ Claude + GPT (team)."""

import json
import re
from datetime import datetime
from pathlib import Path

from . import gitops, ui
from .adapters import AgentError
from .deepseek import DeepSeekError
from .usage import Usage

STATUS_RE = re.compile(r"STATUS:\s*(CONTINUE|DONE|ASK_MANAGER|NEED_HUMAN)", re.I)

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
  /swap            swap builder and reviewer
  /turns N         max team turns per round (now {turns})
  /deepseek on|off on: you talk to DeepSeek, it runs the team (now {ds})
                   off: you talk to the team directly
  /usage           Claude + GPT 5-hour / weekly usage
  /model claude|gpt [name]  change a model (no name = default)
  /new             new session (clear the team chat)
  /status          show roles, models and settings
  /quit            leave
While the team works: type a line + Enter to interject; Ctrl+C to pause.
With DeepSeek on, you can also just ask it for any of this in plain words."""


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
        loop = cfg.get("loop", {})
        self.max_turns = loop.get("max_turns", 8)
        self.recap_every = loop.get("recap_every", 4)
        self.keep_recent = loop.get("keep_recent", 2)
        self.no_recap_window = loop.get("no_recap_window", 10)
        self.manager_window = loop.get("manager_window", 16)
        self.max_handoffs = loop.get("max_handoffs", 3)
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

    def save_settings(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "settings.json").write_text(json.dumps({
            "models": {k: a.model for k, a in self.agents.items()},
            "roles": self.roles,
            "max_turns": self.max_turns,
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
        self.goal, self.resume_role = "", "builder"
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
        gitops.ensure_repo(p)
        return f"workspace is now {p}"

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

    def add(self, who, text, tag="", private=False):
        """private = shown to you and logged, but not part of the team's chat."""
        self.history.append({"who": who, "text": text, "private": private})
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
        team = self.context() if self.goal else "(no task yet; the team is idle)"
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
        return d

    def manager_loop(self, event):
        handoffs = 0
        while True:
            d = self.ask_manager(event)
            if d is None:
                return
            self.apply_actions(d.get("actions"))
            if d["to"] == "human":
                self.add("deepseek", d["message"], tag="→ you", private=True)
                return
            handoffs += 1
            if handoffs > self.max_handoffs:
                self.system(f"DeepSeek handed work to the team {self.max_handoffs} times in a row; stopping here.")
                return
            new_task = d.get("new_task", not self.goal)
            self.add("deepseek", d["message"], tag="→ team" + (" · new task" if new_task else ""))
            event, _ = self.run_team(d["message"], new_task)

    # ---------- team (Claude ⇄ GPT) ----------

    def take_turn(self, role):
        key = self.roles[role]
        other = self.roles["reviewer" if role == "builder" else "builder"]
        tmpl = BUILDER_SYSTEM if role == "builder" else REVIEWER_SYSTEM
        system = tmpl.format(
            me=self.label(key), other=self.label(other), ROLE=role.upper(),
            boss=BOSS_MANAGER if self.ds_on else BOSS_HUMAN,
            ask="ASK_MANAGER" if self.ds_on else "NEED_HUMAN ",
        )
        prompt = f"{self.context()}\n\n# Your turn\nYou are {self.label(key)} ({role}). Respond now."
        five_h = self.usage.pct(key)
        if five_h is not None and five_h >= 90:
            ui.say("error", f"{self.label(key)} is at {five_h}% of its 5-hour limit")
        ui.note(f"{self.label(key)} ({self.model_name(key)}) is working as {role}")
        started = datetime.now().timestamp()
        agent = self.agents[key]
        try:
            text = agent.run(prompt, system, self.workspace, can_edit=(role == "builder"))
        finally:
            if key == "claude":
                self.usage.update_claude(agent.last_rate_limit)
            else:
                self.usage.update_gpt(since=started)
        m = STATUS_RE.findall(text)
        status = m[-1].upper() if m else "CONTINUE"
        self.add(key, text.strip(), tag=role)
        sha = gitops.commit_turn(self.workspace, self.label(key), text)
        if sha:
            self.system(f"committed {sha}")
        return "ASK" if status in ("ASK_MANAGER", "NEED_HUMAN") else status

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
            d = self.deepseek.route(self.context())
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
        while turns < self.max_turns:
            for line in ui.pending_input():
                if self.ds_on:
                    self.add("human", line, tag="interjected", private=True)
                    self.resume_role = role
                    return f"The human interjected while the team worked: {line}", True
                self.add("human", line, tag="interjected")
            try:
                status = self.take_turn(role)
            except AgentError as e:
                ui.say("error", str(e))
                self.resume_role = role
                return f"{self.label(self.roles[role])} ({role}) crashed with an error:\n{e}", True
            except KeyboardInterrupt:
                print()
                self.resume_role = role
                return "The human pressed Ctrl+C to pause the team.", True
            turns += 1
            self.maybe_recap()
            nxt, reason = self.decide_next(role, status)
            if reason:
                ui.note(f"next: {nxt} ({reason})")
            if nxt == "done":
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

    def direct_loop(self, text):
        """DeepSeek off: you are the team's boss."""
        new_task = True
        while True:
            event, waiting = self.run_team(text, new_task)
            if not waiting:
                return
            self.system(f"{event}\nReply, Enter to let them continue, or /stop.")
            reply = ui.ask()
            if reply in ("/stop", "/quit"):
                return
            if reply:
                self.add("human", reply)
            text, new_task = reply, False

    # ---------- main loop ----------

    def on_human(self, text):
        if self.ds_on:
            self.add("human", text, tag="→ DeepSeek", private=True)
            self.manager_loop(f"The human says: {text}")
        else:
            self.add("human", text, tag="→ team")
            if self.goal:
                self.goal = f"{self.goal}\n\nLatest from human: {text}"
            self.direct_loop(text)

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
                f"turns: {self.max_turns}\nmodels: Claude={self.model_name('claude')} · GPT={self.model_name('gpt')}\n"
                f"workspace: {self.workspace}\nlog: {self.log_path}"
            )
        else:
            print(HELP.format(turns=self.max_turns, ds="on" if self.ds_on else "off"))

    def loop(self):
        if gitops.ensure_repo(self.workspace):
            self.system(f"initialized git in {self.workspace}")
        self.handle_command("/status")
        print(HELP.format(turns=self.max_turns, ds="on" if self.ds_on else "off"))
        while True:
            try:
                msg = ui.ask()
            except KeyboardInterrupt:
                print()
                break
            if not msg:
                continue
            if msg == "/quit":
                break
            if msg.startswith("/"):
                self.handle_command(msg)
                continue
            self.on_human(msg)
        self.system(f"bye. log saved to {self.log_path}")
