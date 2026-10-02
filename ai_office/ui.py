"""Terminal UI: chat output above, an always-on editor-style input below.

Uses prompt_toolkit when stdin is a terminal; falls back to plain input() when piped.
"""

import sys
import threading
import time

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

# what the office is doing right now (shown in the bottom bar)
activity = {"text": "", "since": 0.0}
_print_lock = threading.Lock()


def say(who, text, tag=""):
    icon, name, color = STYLE.get(who, ("•", who, ""))
    head = f"{color}{BOLD}{icon} {name}{RESET}"
    if tag:
        head += f" {DIM}{tag}{RESET}"
    with _print_lock:
        print(f"\n{head}\n{color}{text}{RESET}", flush=True)


def note(text):
    activity["text"], activity["since"] = text, time.time()
    with _print_lock:
        print(f"{DIM}  … {text}{RESET}", flush=True)


def idle():
    activity["text"] = ""


# ---------- input ----------

COMMANDS = {
    "/usage": "Claude + GPT 5-hour / weekly usage",
    "/model": "change a model: /model claude|gpt [name]",
    "/swap": "swap builder and reviewer",
    "/turns": "max team turns per round: /turns N",
    "/new": "new session (clear the team chat)",
    "/forget": "clear DeepSeek's memory of your chat",
    "/permissions": "full | ask | auto: how much the AIs may do without asking",
    "/approvals": "permission settings and your 'always' rules",
    "/deepseek": "on = talk to DeepSeek, off = talk to the team",
    "/status": "roles, models, settings",
    "/help": "keys and commands",
    "/quit": "leave",
}

KEYS_HELP = """keys:
  Enter                send            Ctrl+J / Alt+Enter / \\ at line end   new line
  Esc or Ctrl+C        stop the AIs (while working) · clear input (while idle)
  Ctrl+←/→  Alt+B/F    jump word       Shift+arrows   select      Home/End   line start/end
  Ctrl+W / Ctrl+⌫      delete word     Ctrl+U / Ctrl+K   delete to start/end of line
  ↑/↓                  move lines, then history       Ctrl+R   search history
  Tab                  complete /commands and model names
  Ctrl+L               clear screen    Ctrl+D (empty) or Ctrl+C twice   quit"""


def run(office, history_path):
    """Main loop. office must provide: start(), submit(text), busy, cancel(), completions(words)."""
    if not sys.stdin.isatty():
        return _run_plain(office)
    try:
        import prompt_toolkit  # noqa: F401
    except ImportError:
        print("prompt_toolkit not installed; using plain input. Run: uv tool install -e ~/Workplace/AI-office --force")
        return _run_plain(office)
    return _run_tui(office, history_path)


def _run_plain(office):
    office.start()
    while True:
        try:
            msg = input(f"\n{BOLD}you › {RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if msg in ("/quit", "/exit"):
            break
        if msg:
            office.submit(msg, wait=True)
    office.stop()


def _run_tui(office, history_path):
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import get_app
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.filters import Condition, has_completions
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.styles import Style

    # Shift+Enter: kitty/xterm extended encodings → treat like Ctrl+J (new line)
    for seq in ("\x1b[13;2u", "\x1b[27;2;13~"):
        ANSI_SEQUENCES[seq] = Keys.ControlJ

    class OfficeCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/") or "\n" in text:
                return
            words = text.split(" ")
            if len(words) == 1:
                for cmd, meta in COMMANDS.items():
                    if cmd.startswith(words[0]):
                        yield Completion(cmd, -len(words[0]), display_meta=meta)
                return
            for value, meta in office.completions(words[:-1]):
                if value.startswith(words[-1]):
                    yield Completion(value, -len(words[-1]), display_meta=meta)

    busy = Condition(lambda: office.busy)
    kb = KeyBindings()
    last_ctrl_c = [0.0]

    @kb.add("enter", filter=~has_completions)
    def _(event):
        buf = event.current_buffer
        if buf.document.text_before_cursor.endswith("\\"):
            buf.delete_before_cursor(1)
            buf.insert_text("\n")
        elif buf.text.strip():
            buf.validate_and_handle()

    @kb.add("enter", filter=has_completions)
    def _(event):
        buf = event.current_buffer
        if buf.complete_state and buf.complete_state.current_completion:
            buf.apply_completion(buf.complete_state.current_completion)
        else:
            buf.complete_state = None
            if buf.text.strip():
                buf.validate_and_handle()

    @kb.add("c-j")
    @kb.add("escape", "enter")
    def _(event):
        event.current_buffer.insert_text("\n")

    @kb.add("c-left")
    def _(event):
        b = event.current_buffer
        b.cursor_position += b.document.find_previous_word_beginning(count=event.arg) or 0

    @kb.add("c-right")
    def _(event):
        b = event.current_buffer
        b.cursor_position += b.document.find_next_word_ending(count=event.arg) or 0

    @kb.add("c-h")  # Ctrl+Backspace in kitty
    def _(event):
        b = event.current_buffer
        pos = b.document.find_previous_word_beginning(count=event.arg)
        if pos:
            b.delete_before_cursor(count=-pos)

    @kb.add("escape", filter=busy, eager=True)
    def _(event):
        office.cancel()

    @kb.add("c-c")
    def _(event):
        buf = event.current_buffer
        if office.busy:
            office.cancel()
        elif buf.text:
            buf.reset()
        elif time.time() - last_ctrl_c[0] < 2:
            event.app.exit(exception=EOFError)
        else:
            last_ctrl_c[0] = time.time()
            note("press Ctrl+C again (or Ctrl+D) to quit")
            idle()

    @kb.add("c-d")
    def _(event):
        if not event.current_buffer.text:
            event.app.exit(exception=EOFError)
        else:
            event.current_buffer.delete()

    @kb.add("c-l")
    def _(event):
        event.app.renderer.clear()

    def prompt_message():
        if office.approval_prompt:
            return ANSI(f"\033[93m{BOLD}{office.approval_prompt}{RESET}")
        # only while working: one status line above the input; otherwise just the prompt
        if office.busy and activity["text"]:
            s = int(time.time() - activity["since"])
            return ANSI(f"{DIM}⏳ {activity['text']} · {s // 60}:{s % 60:02d} · esc to stop{RESET}\n{BOLD}› {RESET}")
        return ANSI(f"{BOLD}› {RESET}")

    style = Style.from_dict({
        "completion-menu": "bg:#262626 #d0d0d0",
        "completion-menu.completion.current": "bg:#5f5fd7 #ffffff",
        "completion-menu.meta.completion": "bg:#1c1c1c #8a8a8a",
        "completion-menu.meta.completion.current": "bg:#5f5fd7 #ffffff",
    })

    history_path.parent.mkdir(parents=True, exist_ok=True)
    session = PromptSession(
        message=prompt_message,
        multiline=True,
        prompt_continuation=lambda width, line_no, wrap: "  ",
        history=FileHistory(str(history_path)),
        completer=OfficeCompleter(),
        # only typing a /command pops the menu (and makes room for it)
        complete_while_typing=Condition(lambda: get_app().current_buffer.text.startswith("/")),
        key_bindings=kb,
        reserve_space_for_menu=6,
        style=style,
        refresh_interval=0.5,
        enable_history_search=False,
        mouse_support=False,
    )

    office.interactive = True
    with patch_stdout(raw=True):
        office.start()
        while True:
            try:
                msg = session.prompt().strip()
            except (EOFError, KeyboardInterrupt):
                break
            if msg in ("/quit", "/exit"):
                break
            if msg:
                office.submit(msg)
        office.stop()
