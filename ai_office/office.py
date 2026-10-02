"""The office: you ⇄ DeepSeek (manager) ⇄ Claude + GPT (team)."""

import json
import re
from datetime import datetime
from pathlib import Path

from . import gitops, ui
from .adapters import AgentError
from .deepseek import DeepSeekError

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
  /status          show roles and settings
  /quit            leave
While the team works: type a line + Enter to interject; Ctrl+C to pause."""


class Office:
    def __init__(self, agents, workspace, cfg, deepseek=None, log_dir=None):
        self.agents = agents  # {"claude": agent, "gpt": agent}
        self.workspace = Path(workspace)
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
        log_dir = Path(log_dir or "logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / f"{datetime.now():%Y-%m-%d_%H-%M-%S}.md"
        self.log_path.write_text(f"# AI Office session\n\nworkspace: `{self.workspace}`\n")

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
        msg = f"# Team status\n{team}\n\n# What just happened\n{event}"
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
        ui.note(f"{self.label(key)} is working as {role}")
        text = self.agents[key].run(prompt, system, self.workspace, can_edit=(role == "builder"))
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
            self.system(f"builder: {self.label(self.roles['builder'])}, reviewer: {self.label(self.roles['reviewer'])}")
        elif parts[0] == "/turns" and len(parts) == 2 and parts[1].isdigit():
            self.max_turns = int(parts[1])
            self.system(f"max turns: {self.max_turns}")
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
                f"turns: {self.max_turns}\nworkspace: {self.workspace}\nlog: {self.log_path}"
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
