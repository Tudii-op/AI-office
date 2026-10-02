"""DeepSeek as the cheap office manager: who's next, recaps, final report."""

import json
import urllib.error
import urllib.request

from . import prompts


class DeepSeekError(RuntimeError):
    pass


TARGETS = ("human", "team", "claude", "gpt", "office")



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
        raw = self.chat(prompts.load("router"), context, json_mode=True)
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            raise DeepSeekError(f"DeepSeek router returned non-JSON: {raw[:500]}")
        if d.get("next") not in ("builder", "reviewer", "manager", "done"):
            raise DeepSeekError(f"DeepSeek router returned invalid choice: {d}")
        return d

    def recap(self, previous, transcript):
        user = f"Previous recap:\n{previous or '(none)'}\n\nNew messages:\n{transcript}"
        return self.chat(prompts.load("recap"), user).strip()

    def narrate(self, context):
        line = self.chat(prompts.load("narrator"), context).strip().splitlines()
        return line[0].strip().strip('"')[:200] if line else ""

    def approve(self, request):
        raw = self.chat(prompts.load("approver"), request, json_mode=True)
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            raise DeepSeekError(f"DeepSeek approver returned non-JSON: {raw[:500]}")
        if d.get("decision") not in ("allow", "deny", "ask_human"):
            raise DeepSeekError(f"DeepSeek approver returned an invalid decision: {d}")
        return d

    def manage(self, messages):
        raw = self.chat_messages(prompts.load("manager"), messages, json_mode=True)
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            raise DeepSeekError(f"DeepSeek manager returned non-JSON: {raw[:500]}")
        if d.get("to") not in TARGETS or (d["to"] != "office" and not str(d.get("message", "")).strip()):
            raise DeepSeekError(f"DeepSeek manager returned an invalid reply: {d}")
        return d


class FakeDeepSeek:
    """Costs nothing. Used with --fake."""

    def narrate(self, context):
        last = context.strip().splitlines()[-1].lstrip("- ")
        return f"[fake live] {context.splitlines()[0].split(': ', 1)[1]} is at it: {last}"

    def approve(self, request):
        if "run: ls" in request or "run: cat" in request:
            return {"decision": "allow", "reason": "[fake] read-only"}
        if "rm -rf" in request:
            return {"decision": "deny", "reason": "[fake] destructive"}
        return {"decision": "ask_human", "reason": "[fake] needs your call"}

    def route(self, context):
        last_builder = context.rfind("(builder)]")
        return {"next": "reviewer" if last_builder > context.rfind("(reviewer)]") else "builder",
                "reason": "fake turn-taking"}

    def recap(self, previous, transcript):
        return f"[fake recap of {transcript.count('[')} messages]"

    def manage(self, messages):
        content = messages[-1]["content"]
        event = content.split("# What just happened\n", 1)[-1].split("\n\n# Results of your last actions", 1)[0]
        results = content.split("# Results of your last actions\n", 1)[1] if "# Results of your last actions" in content else ""
        reply = lambda to, msg, new=False, actions=(): {"to": to, "message": msg, "new_task": new, "actions": list(actions)}
        if event.startswith("The human says:"):
            said = event.split(":", 1)[1].strip().lower()
            if said.startswith(("hi", "hello", "are you")):
                return reply("human", "[fake] Hi! I'm DeepSeek. Tell me what to build.")
            if "usage" in said:
                line = next((l for l in content.splitlines() if l.startswith("usage:")), "usage: ?")
                return reply("human", f"[fake] {line}")
            if said.startswith("use "):
                agent, model = said.split()[1:3]
                return reply("human", f"[fake] switching {agent} to {model}", actions=[{"do": "set_model", "agent": agent, "model": model}])
            if said.startswith("ask "):
                agent = said.split()[1]
                return reply(agent, f"[fake] The human asks: {said.split(' ', 2)[2]}")
            if "files" in said:
                return reply("office", "checking files", actions=[{"do": "list_files"}, {"do": "git_log", "n": 3}])
            if "new session" in said:
                return reply("human", "[fake] fresh session started", actions=[{"do": "new_session"}])
            return reply("team", f"[fake brief] Build this: {said}", new=True)
        if "answered" in event:
            return reply("human", f"[fake relay] {event[:300]}")
        if event.startswith("(lookup") or results:
            return reply("human", f"[fake] here's what I found:\n{results[:400]}")
        if "finished" in event:
            return reply("human", "[fake report] The team finished. Files: fake_output.txt.")
        return reply("human", f"[fake] {event[:300]}")
