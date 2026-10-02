"""Subscription usage (5-hour and weekly windows) for Claude and GPT.

Neither CLI has a free "check usage" call, so numbers refresh whenever an agent runs:
- Claude: `rate_limit_event` lines in `claude -p --output-format stream-json`
- GPT: `rate_limits` in Codex's local session logs (~/.codex/sessions)
"""

import json
import time
from datetime import datetime
from pathlib import Path

CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
WINDOWS = ("five_hour", "seven_day")
WINDOW_LABEL = {"five_hour": "5h", "seven_day": "week"}


def _pct(v):
    if v is None:
        return None
    v = float(v)
    return round(v * 100 if v <= 1 else v, 1)


def _newest_codex_log(since=0):
    if not CODEX_SESSIONS.exists():
        return None
    files = [p for p in CODEX_SESSIONS.glob("*/*/*/*.jsonl") if p.stat().st_mtime >= since]
    return max(files, key=lambda p: p.stat().st_mtime, default=None)


def _last_codex_rate_limits(path):
    last = None
    with path.open(errors="replace") as fh:
        for line in fh:
            if '"rate_limits"' not in line:
                continue
            try:
                payload = json.loads(line).get("payload", {})
            except json.JSONDecodeError:
                continue
            rl = payload.get("rate_limits") or (payload.get("info") or {}).get("rate_limits")
            if rl:
                last = rl
    return last


class Usage:
    def __init__(self, path):
        self.path = Path(path)
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))

    def update_claude(self, info):
        """info = rate_limit_info from a Claude rate_limit_event."""
        if not info:
            return
        entry = self.data.setdefault("claude", {})
        for w, v in (info.get("unifiedWindows") or {}).items():
            if w in WINDOWS and v:
                entry[w] = {"pct": _pct(v.get("utilization")), "resets_at": v.get("resetsAt")}
        w = info.get("rateLimitType")
        if w in WINDOWS and info.get("utilization") is not None and w not in (info.get("unifiedWindows") or {}):
            entry[w] = {"pct": _pct(info["utilization"]), "resets_at": info.get("resetsAt")}
        entry["status"] = info.get("status")
        entry["updated"] = int(time.time())
        self._save()

    def update_gpt(self, since=0):
        log = _newest_codex_log(since)
        rl = log and _last_codex_rate_limits(log)
        if not rl:
            return
        entry = self.data.setdefault("gpt", {})
        for key, w in (("primary", "five_hour"), ("secondary", "seven_day")):
            v = rl.get(key)
            if v:
                entry[w] = {"pct": _pct(v.get("used_percent")), "resets_at": v.get("resets_at")}
        entry["plan"] = rl.get("plan_type")
        entry["status"] = rl.get("rate_limit_reached_type") or "allowed"
        entry["updated"] = int(log.stat().st_mtime)
        self._save()

    def pct(self, agent, window="five_hour"):
        return (self.data.get(agent, {}).get(window) or {}).get("pct")

    @staticmethod
    def _when(ts):
        if not ts:
            return "?"
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%H:%M" if dt.date() == datetime.now().date() else "%a %H:%M")

    @staticmethod
    def _ago(ts):
        if not ts:
            return "never"
        m = int((time.time() - ts) // 60)
        return "just now" if m < 1 else f"{m}m ago" if m < 60 else f"{m // 60}h ago" if m < 1440 else f"{m // 1440}d ago"

    def render(self, labels):
        lines = []
        for agent, label in labels.items():
            e = self.data.get(agent)
            if not e:
                lines.append(f"{label:<7} no data yet (updates after its next turn)")
                continue
            parts = []
            for w in WINDOWS:
                v = e.get(w)
                if not v or v.get("pct") is None:
                    parts.append(f"{WINDOW_LABEL[w]:>4} ?")
                    continue
                p = v["pct"]
                bar = "█" * round(p / 12.5) + "░" * (8 - round(p / 12.5))
                parts.append(f"{WINDOW_LABEL[w]:>4} {bar} {p:>5.1f}%  resets {self._when(v.get('resets_at'))}")
            extra = f"  ({e['plan']})" if e.get("plan") else ""
            lines.append(f"{label:<7}" + "   ".join(parts) + f"   · updated {self._ago(e.get('updated'))}{extra}")
        return "\n".join(lines)

    def summary(self, labels):
        """Compact text for DeepSeek."""
        out = []
        for agent, label in labels.items():
            e = self.data.get(agent)
            if not e:
                out.append(f"{label}: unknown")
                continue
            ws = []
            for w in WINDOWS:
                v = e.get(w) or {}
                ws.append(f"{WINDOW_LABEL[w]} {v.get('pct', '?')}% (resets {self._when(v.get('resets_at'))})")
            out.append(f"{label}: " + ", ".join(ws) + f", updated {self._ago(e.get('updated'))}")
        return "; ".join(out)
