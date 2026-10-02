"""DeepSeek as the cheap office manager: who's next, recaps, final report."""

import json
import urllib.error
import urllib.request


class DeepSeekError(RuntimeError):
    pass


MANAGER_SYSTEM = """You are DeepSeek, the manager in "AI Office". The human talks ONLY to you.
You lead a team of two AIs, Claude and GPT, who share a project folder: one is the builder (edits files), the other the reviewer (read-only). They talk to each other directly; you brief them, answer their questions, and report back to the human. You cannot edit files yourself.

You also run the office itself. Each turn you get "# Office state": mode, roles, current models and the models available, turn limit, workspace, past sessions, and subscription usage (5-hour and weekly limits, which refresh after each agent's turn).

Every reply must be JSON only:
{"to": "human" | "team", "message": "<text>", "new_task": true | false, "actions": [ ... ]}

"actions" is optional; they run BEFORE your message is delivered. Available actions:
- {"do": "set_model", "agent": "claude" | "gpt", "model": "<name from the available list, or \"\" for default>"}
- {"do": "swap_roles"}                        (builder <-> reviewer)
- {"do": "set_turns", "n": <int 1-50>}        (max team turns per round)
- {"do": "new_session"}                       (clear the team chat and task; starts a new log)
- {"do": "set_workspace", "path": "<folder>"} (a plain name = AI-office/workspaces/<name>; or an existing absolute/~ path)
Results of actions show up in the next "What just happened". Only use actions when the human asks or when clearly sensible; tell the human what you changed.

- to "human": chat, ask a clarifying question, or report results. When the team finished, summarize what was built, which files changed, open issues and any disagreements.
- to "team": a clear, self-contained brief (goal, constraints, what "done" looks like), or an answer to a question they asked you.
- new_task: true when the brief starts a new task (the team's goal is replaced); false when it continues or answers the current task.
- Only delegate when there is real work to do. Small talk and questions you can answer yourself go to the human.
- Ask the human before delegating only when the request is genuinely ambiguous.
- Don't answer team questions that are really the human's call (taste, money, scope); ask the human instead.
- Usage: answer usage questions from the Office state. If an agent is above ~80% of a window, warn the human before delegating big work and suggest options (cheaper model, swap roles, wait for the reset).
Be concise and friendly."""

ROUTER_SYSTEM = """You are the manager of a chat where two AIs (a builder and a reviewer) work on a goal.
After each message, decide what happens next. Reply with JSON only:
{"next": "builder" | "reviewer" | "manager" | "done", "reason": "<one short sentence>"}
Rules:
- "builder" when code/files need changing; "reviewer" when fresh work needs checking.
- "done" when the goal is met and the reviewer approved, OR they are going in circles / just agreeing without progress.
- "manager" when they are blocked on a question or decision they can't settle themselves.
- Usually alternate builder and reviewer unless there is a clear reason not to."""

RECAP_SYSTEM = """Summarize this work conversation between AIs into a compact recap that replaces the old messages.
Keep: decisions made, files created/changed, open issues, disagreements, what the human asked for.
Drop: pleasantries, repetition. Max ~200 words. Plain text."""


class DeepSeek:
    def __init__(self, base_url, model, api_key, timeout=120):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def chat(self, system, user, json_mode=False):
        return self.chat_messages(system, [{"role": "user", "content": user}], json_mode)

    def chat_messages(self, system, messages, json_mode=False):
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise DeepSeekError(f"DeepSeek HTTP {e.code} ({self.model}): {e.read().decode(errors='replace')}")
        except urllib.error.URLError as e:
            raise DeepSeekError(f"DeepSeek connection failed: {e.reason}")
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            raise DeepSeekError(f"DeepSeek unexpected response: {json.dumps(data)[:2000]}")

    def route(self, context):
        raw = self.chat(ROUTER_SYSTEM, context, json_mode=True)
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            raise DeepSeekError(f"DeepSeek router returned non-JSON: {raw[:500]}")
        if d.get("next") not in ("builder", "reviewer", "manager", "done"):
            raise DeepSeekError(f"DeepSeek router returned invalid choice: {d}")
        return d

    def recap(self, previous, transcript):
        user = f"Previous recap:\n{previous or '(none)'}\n\nNew messages:\n{transcript}"
        return self.chat(RECAP_SYSTEM, user).strip()

    def manage(self, messages):
        raw = self.chat_messages(MANAGER_SYSTEM, messages, json_mode=True)
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            raise DeepSeekError(f"DeepSeek manager returned non-JSON: {raw[:500]}")
        if d.get("to") not in ("human", "team") or not str(d.get("message", "")).strip():
            raise DeepSeekError(f"DeepSeek manager returned an invalid reply: {d}")
        return d


class FakeDeepSeek:
    """Costs nothing. Used with --fake."""

    def route(self, context):
        last_builder = context.rfind("(builder)]:")
        return {"next": "reviewer" if last_builder > context.rfind("(reviewer)]:") else "builder",
                "reason": "fake turn-taking"}

    def recap(self, previous, transcript):
        return f"[fake recap of {transcript.count('[')} messages]"

    def manage(self, messages):
        event = messages[-1]["content"].split("# What just happened\n", 1)[-1]
        reply = lambda to, msg, new=False, actions=(): {"to": to, "message": msg, "new_task": new, "actions": list(actions)}
        if event.startswith("The human says:"):
            said = event.split(":", 1)[1].strip().lower()
            if said.startswith(("hi", "hello", "are you")):
                return reply("human", "[fake] Hi! I'm DeepSeek. Tell me what to build.")
            if "usage" in said:
                state = messages[-1]["content"]
                line = next((l for l in state.splitlines() if l.startswith("usage:")), "usage: ?")
                return reply("human", f"[fake] {line}")
            if said.startswith("use "):
                agent, model = said.split()[1:3]
                return reply("human", f"[fake] switching {agent} to {model}", actions=[{"do": "set_model", "agent": agent, "model": model}])
            if "new session" in said:
                return reply("human", "[fake] fresh session started", actions=[{"do": "new_session"}])
            return reply("team", f"[fake brief] Build this: {said}", new=True)
        if "finished" in event:
            return reply("human", "[fake report] The team finished. Files: fake_output.txt.")
        return reply("human", f"[fake] {event[:300]}")
