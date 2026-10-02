"""Terminal chat rendering and input."""

import select
import sys

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"

STYLE = {
    "human": ("🧑", "You", "\033[97m"),
    "claude": ("🟣", "Claude", "\033[95m"),
    "gpt": ("🟢", "GPT", "\033[92m"),
    "deepseek": ("🔵", "DeepSeek", "\033[94m"),
    "system": ("⚙ ", "office", "\033[90m"),
    "error": ("❌", "error", "\033[91m"),
}


def say(who, text, tag=""):
    icon, name, color = STYLE.get(who, ("•", who, ""))
    head = f"{color}{BOLD}{icon} {name}{RESET}"
    if tag:
        head += f" {DIM}{tag}{RESET}"
    print(f"\n{head}\n{color}{text}{RESET}", flush=True)


def note(text):
    print(f"{DIM}  … {text}{RESET}", flush=True)


def ask(prompt="you › "):
    try:
        return input(f"\n{BOLD}{prompt}{RESET}").strip()
    except EOFError:
        return "/quit"


def pending_input():
    """Lines the user typed (and hit Enter on) while the AIs were working."""
    lines = []
    if not sys.stdin.isatty():
        return lines
    while select.select([sys.stdin], [], [], 0)[0]:
        line = sys.stdin.readline()
        if not line:
            break
        if line.strip():
            lines.append(line.strip())
    return lines
