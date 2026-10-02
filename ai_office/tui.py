"""Full-screen AI Office (Textual): one chat panel, input box, approval dialogs.

The office logic is unchanged; this only replaces how things are shown and typed.
`ai-office --plain` still gives the classic inline terminal UI.
"""

import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from . import ui

COLORS = {"human": "#ffd7af", "claude": "#d787ff", "gpt": "#87d787", "deepseek": "#5fafff",
          "system": "#5fd7af", "error": "#ff5f5f"}
TAG_COLORS = {"human": "#d7af87", "claude": "#af87d7", "gpt": "#5faf87", "deepseek": "#5f87d7",
              "system": "#5faf87", "error": "#d75f5f"}
# single-width marks (emoji widths vary between terminals)
MARKS = {"human": "›", "claude": "●", "gpt": "●", "deepseek": "◆", "system": "◇", "error": "✗"}
DIM, FAINT, ACCENT, WARN = "#8787af", "#5f5f87", "#87afff", "#ffaf00"
NOTE = "#7f7fb5"  # progress notes: soft blue-violet instead of grey
STATUS_COLORS = {"CONTINUE": "#ffd75f", "DONE": "#87d787", "ASK_MANAGER": "#ff875f", "NEED_HUMAN": "#ff875f"}
TAIL_RE = re.compile(r"^\s*(REPORT|STATUS):\s*(.*)$", re.I)


def hard_breaks(md):
    """Keep the AIs' line breaks (REPORT/STATUS lines etc.) outside code blocks."""
    out, fence = [], False
    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        out.append(line if fence or not line.strip() else line + "  ")
    return "\n".join(out)


def short_path(p):
    p, home = Path(p), Path.home()
    return "~" if p == home else "~/" + str(p.relative_to(home)) if home in p.parents else str(p)


def split_tail(text):
    """Pull trailing REPORT:/STATUS: lines out of an agent message."""
    lines = text.rstrip().splitlines()
    tail = {}
    while lines and (m := TAIL_RE.match(lines[-1])):
        tail.setdefault(m.group(1).upper(), m.group(2).strip())
        lines.pop()
    return "\n".join(lines).rstrip(), tail


PREVIEW_LINES = 3
MD_NOISE = re.compile(r"^\s*(#{1,6}\s+|[-*+]\s+|>\s*)|\*\*|__(?=\S)|(?<=\S)__|`")


def preview_text(body, n=PREVIEW_LINES):
    lines = [MD_NOISE.sub("", l).strip() for l in body.splitlines()]
    lines = [l for l in lines if l and not l.startswith("```")]
    return "\n".join(lines[:n]), max(0, len([l for l in body.splitlines() if l.strip()]) - n)


class ToggleLine(Static):
    """A line you can click to expand/collapse its message."""

    def on_click(self, event):
        msg = next((a for a in self.ancestors if isinstance(a, ChatMessage)), None)
        if msg and msg.collapsible:
            msg.toggle()


class ChatMessage(Vertical):
    def __init__(self, who, text, tag):
        compact = (who in ("system", "error") and "\n" not in text.strip()) or tag == "live"
        super().__init__(classes=f"msg msg-{who}" + (" compact" if compact else ""))
        self.who, self.text, self.tag, self.compact = who, text, tag, compact
        self.body, self.tail = split_tail(text) if who in ("claude", "gpt") and not compact else (text, {})
        _, self.hidden = preview_text(self.body)
        # Claude/GPT replies start collapsed: you mostly read DeepSeek
        self.collapsible = who in ("claude", "gpt") and not compact and self.hidden > 1
        self.expanded = not self.collapsible

    def compose(self) -> ComposeResult:
        name = ui.STYLE.get(self.who, ("", self.who, ""))[1]
        color = COLORS.get(self.who, "white")
        head = Text.assemble((f"{MARKS.get(self.who, '•')} ", color), (name, f"bold {color}"))
        if self.tag:
            head.append(f"  {self.tag}", TAG_COLORS.get(self.who, DIM))
        if self.compact:  # one-liner: "◇ office  committed 3725f6b" / "◆ DeepSeek  live  Claude is …"
            yield Static(head + Text("  ") + Text(self.text.strip(), self.body_color()))
            return
        yield ToggleLine(head, classes="msg-head")
        if self.who in ("system", "error"):
            yield Static(Text(self.text, self.body_color()), classes="msg-body")
            return
        yield Vertical(classes="msg-content")
        if self.tail:
            t = Text()
            if "REPORT" in self.tail:
                t.append("report ", f"bold {TAG_COLORS[self.who]}")
                t.append(self.tail["REPORT"], "#bcbcd7")
            if "STATUS" in self.tail:
                st = self.tail["STATUS"].split()[0].upper() if self.tail["STATUS"] else "?"
                t.append("  ·  " if "REPORT" in self.tail else "", FAINT)
                t.append("status ", f"bold {TAG_COLORS[self.who]}")
                t.append(f"● {st}", f"bold {STATUS_COLORS.get(st, ACCENT)}")
            yield Static(t, classes="msg-tail")
        if self.collapsible:
            yield ToggleLine("", classes="msg-toggle")

    def on_mount(self):
        if not self.compact and self.who not in ("system", "error"):
            self.render_content()

    def render_content(self):
        box = self.query_one(".msg-content", Vertical)
        box.remove_children()
        if self.expanded:
            if self.body:
                box.mount(Markdown(hard_breaks(self.body), classes="msg-body"))
        else:
            box.mount(Static(Text(preview_text(self.body)[0], "#d0d0e0"), classes="msg-body"))
        if self.collapsible:
            hint = (Text(f"▾ collapse", ACCENT) if self.expanded else
                    Text.assemble((f"▸ {self.hidden} more lines", ACCENT), ("  click or ctrl+o", FAINT)))
            self.query_one(".msg-toggle", ToggleLine).update(hint)

    def toggle(self):
        self.expanded = not self.expanded
        self.render_content()

    def body_color(self):
        if self.who == "error":
            return "#ff8787"
        if self.who == "deepseek":
            return "#afd7ff"
        if self.text.lstrip().startswith(("🔐", "⚠")):
            return "#ffd787"
        return "#afd7d7"


def bar(p, width=10):
    if p is None:
        return Text("·" * width + "    ?", FAINT)  # same width as a real bar
    filled = max(0, min(width, round(p / 100 * width)))
    color = "#87d787" if p < 60 else "#ffd75f" if p < 85 else "#ff5f5f"
    return Text.assemble(("━" * filled, color), ("─" * (width - filled), FAINT), (f" {p:>3.0f}%", color))


def session_card(office):
    """The start-of-session info, posted into the chat (scrolls away with it)."""
    o = office
    t = Text()
    for k in ("claude", "gpt"):
        name = ui.STYLE[k][1]
        t.append(f"{MARKS[k]} ", COLORS[k])
        t.append(f"{name:<9}", f"bold {COLORS[k]}")
        t.append(f"{o.role_of(k):<9}", TAG_COLORS[k])
        t.append(f"{o.model_name(k):<14}", "#d7d7ff")
        t.append("5h ", DIM)
        t.append_text(bar(o.usage.pct(k, "five_hour")))
        t.append("   wk ", DIM)
        t.append_text(bar(o.usage.pct(k, "seven_day")))
        t.append("\n")
    t.append(f"{MARKS['deepseek']} ", COLORS["deepseek"])
    t.append(f"{'DeepSeek':<9}", f"bold {COLORS['deepseek']}")
    t.append(f"{'manager':<9}", TAG_COLORS["deepseek"])
    t.append("on: you talk to DeepSeek, it runs the team\n" if o.ds_on else "off: you talk to the team directly\n", "#d7d7ff")
    for label, value in (("workspace", short_path(o.workspace)), ("permissions", o.perm["mode"]),
                         ("turns", str(o.max_turns)), ("auto-commit", "on" if o.commits_enabled() else "off")):
        t.append(f"{label} ", DIM)
        t.append(f"{value}", "bold #87d7d7")
        t.append("   ")
    card = Static(t, classes="card")
    card.border_title = " session "
    card.border_subtitle = " /help · /status "
    return card


class PromptArea(TextArea):
    """Multi-line input: Enter sends, Shift+Enter / Ctrl+J / Alt+Enter / trailing \\ = new line."""

    BINDINGS = [
        Binding("up", "hist_up", show=False),
        Binding("down", "hist_down", show=False),
        Binding("tab", "complete", show=False),
    ]

    class Submitted(Message):
        def __init__(self, text):
            super().__init__()
            self.text = text

    async def _on_key(self, event):
        app = self.app
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            if app.popup_open():
                app.accept_popup()
                return
            row, col = self.cursor_location
            if col and self.document.get_line(row)[:col].endswith("\\"):
                self.delete((row, col - 1), (row, col))
                self.insert("\n")
            elif self.text.strip():
                self.post_message(self.Submitted(self.text))
            return
        if event.key in ("shift+enter", "ctrl+j", "alt+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        await super()._on_key(event)

    def action_hist_up(self):
        if self.app.popup_open():
            self.app.move_popup(-1)
        elif self.cursor_location[0] == 0:
            self.app.history_step(-1)
        else:
            self.action_cursor_up()

    def action_hist_down(self):
        if self.app.popup_open():
            self.app.move_popup(1)
        elif self.cursor_location[0] == self.document.line_count - 1:
            self.app.history_step(1)
        else:
            self.action_cursor_down()

    def action_complete(self):
        if self.app.popup_open():
            self.app.accept_popup()


class Dialog(Vertical, can_focus=True):
    """Focusable box so y/a/n reach the dialog instead of the reason input."""


class ApprovalScreen(ModalScreen):
    AUTO_FOCUS = "#dialog"  # y/a/n go to the dialog; Tab moves into the reason box
    BINDINGS = [
        Binding("y", "choose('y')", "allow"),
        Binding("a", "choose('a')", "always"),
        Binding("n", "choose('n')", "deny"),
        Binding("escape", "choose('n')", "deny", show=False),
    ]

    def __init__(self, text, rule):
        super().__init__()
        self.text, self.rule = text, rule

    def compose(self) -> ComposeResult:
        with Dialog(id="dialog") as d:
            d.border_title = "permission needed"
            d.border_subtitle = "y · a · n · tab to type a reason"
            yield Static(Text(self.text), id="dialog-text")
            opts = Text()
            opts.append("[y]", f"bold {COLORS['gpt']}"); opts.append(" allow once    ")
            if self.rule:
                opts.append("[a]", f"bold {ACCENT}"); opts.append(" always    ")
            opts.append("[n]", f"bold {COLORS['error']}"); opts.append(" deny")
            yield Static(opts, id="dialog-options")
            if self.rule:
                yield Static(Text(f"always saves the rule {self.rule}", DIM))
            yield Input(placeholder="reason to deny (sent to the AI) … enter", id="reason")

    def on_input_submitted(self, event):
        self.dismiss(event.value.strip() or "n")

    def action_choose(self, choice):
        if choice == "a" and not self.rule:
            return
        self.dismiss(choice)


class OfficeApp(App):
    TITLE = "AI Office"
    CSS = """
    * { background: ansi_default; scrollbar-size-vertical: 1; scrollbar-background: ansi_default;
        scrollbar-color: #3a3a5f; scrollbar-color-hover: #5f5f87; scrollbar-color-active: #87afff; }
    Screen { layout: vertical; }
    Screen > .screen--selection { background: #5f3d87; color: #ffffff; }
    #chat { height: 1fr; padding: 0; border: round #5f5f87; border-title-color: #87afff;
            border-title-style: bold; border-subtitle-color: #8787af; }
    .card { height: auto; margin: 0; padding: 0 1; border: round #5f5f87;
            border-title-color: #87d7d7; border-title-style: bold; border-subtitle-color: #5f5f87; }
    .msg { height: auto; margin: 0; padding: 0 0 0 1; border-left: heavy #5f5f87; }
    .msg-claude { border-left: heavy #d787ff; }
    .msg-gpt { border-left: heavy #87d787; }
    .msg-deepseek { border-left: heavy #5fafff; }
    .msg-human { border-left: heavy #ffd7af; }
    .msg-error { border-left: heavy #ff5f5f; }
    .msg-system { border-left: none; padding: 0; }
    .compact { margin: 0; }
    .msg-head { height: 1; }
    .msg-body { height: auto; }
    .msg-tail { height: auto; }
    .msg-content { height: auto; }
    .msg-toggle { height: 1; }
    .msg-toggle:hover, .msg-head:hover { text-style: underline; }
    Markdown { margin: 0; padding: 0; }
    .msg-body MarkdownParagraph { margin: 0; }
    .msg-body MarkdownFence { margin: 0; padding: 0 1; border: round #5f5f87; }
    .note { height: auto; color: #7f7fb5; text-style: italic; padding: 0; }
    #bottom { height: auto; }
    #popup { height: auto; max-height: 10; display: none; border: round #5f5fd7; padding: 0 1; }
    #popup > .option-list--option-highlighted { background: ansi_default; color: #87afff; text-style: bold; }
    #activity { height: 1; padding: 0; }
    PromptArea { height: auto; min-height: 3; max-height: 12; border: round #5f5fd7; padding: 0 1;
                 border-title-color: #87afff; border-title-style: bold; border-subtitle-color: #5f5f87; }
    PromptArea:focus { border: round #87afff; }
    PromptArea { scrollbar-size: 0 0; }
    PromptArea .text-area--cursor-line { background: ansi_default; }
    PromptArea .text-area--selection { background: #5f3d87; color: #ffffff; }
    ApprovalScreen { align: center middle; background: rgba(0,0,0,0); }
    #dialog { background: ansi_default; width: 86; height: auto; padding: 0 1; border: round #ffaf00;
              border-title-color: #ffaf00; border-title-style: bold; border-subtitle-color: #d7af5f; }
    #dialog-text { margin: 0 0 1 0; color: #ffd787; }
    #dialog-options { margin: 0 0 1 0; }
    #reason { border: round #5f5f87; margin: 0; }
    #reason:focus { border: round #ffaf00; }
    """
    BINDINGS = [
        Binding("escape", "stop", "stop AIs"),
        Binding("ctrl+c", "ctrl_c", "stop / clear / quit", priority=True),
        Binding("ctrl+d", "quit_if_empty", "quit", show=False),
        Binding("ctrl+l", "clear_chat", "clear chat"),
        Binding("ctrl+o", "toggle_last", "expand/collapse the latest Claude/GPT reply", priority=True),
    ]

    def __init__(self, office, history_path):
        super().__init__()
        self.office = office
        self.history_path = Path(history_path)
        try:
            self.history = json.loads(self.history_path.read_text())
        except (OSError, json.JSONDecodeError):
            self.history = []
        self.hist_pos = len(self.history)
        self.last_ctrl_c = 0.0
        self.popup_word = ""
        self.flash = None  # (text, until) short message in the activity line
        self._thread = None

    # ---------- layout ----------

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="chat")
        with Vertical(id="bottom"):
            yield OptionList(id="popup")
            yield Static(id="activity")
            yield PromptArea(id="input", soft_wrap=True, tab_behavior="focus", show_line_numbers=False)

    def on_mount(self):
        self._thread = threading.get_ident()
        self.theme = "ansi-dark"  # terminal's own background (kitty transparency shows through)
        self.query_one("#chat").border_title = self.chat_title()
        ui.set_sink(self)
        self.office.interactive = True
        self._mount(session_card(self.office))
        self.office.start(show_status=False)
        self.query_one("#input").focus()
        self.set_interval(0.25, self.tick)
        self.tick()

    # ---------- sink (called by the office, any thread) ----------

    def _on_ui(self, fn, *args):
        if threading.get_ident() == self._thread:
            fn(*args)
        else:
            try:
                self.call_from_thread(fn, *args)
            except RuntimeError:
                pass  # app is shutting down

    def say(self, who, text, tag=""):
        self._on_ui(self._mount, ChatMessage(who, text, tag))

    def note(self, text):
        self._on_ui(self._mount, Static(Text(f"  … {text}"), classes="note"))

    def request_approval(self, text, rule):
        self._on_ui(self._show_approval, text, rule)

    def _mount(self, widget):
        chat = self.query_one("#chat")
        follow = chat.max_scroll_y == 0 or chat.scroll_y >= chat.max_scroll_y - 2
        chat.mount(widget)
        if follow:
            chat.call_after_refresh(chat.scroll_end, animate=False)
        self.tick()

    def _show_approval(self, text, rule):
        def answered(choice):
            if self.office.pending:
                self.office.submit(choice or "n")
        self.push_screen(ApprovalScreen(text, rule), answered)

    def chat_title(self):
        return f" [b #87afff]AI Office[/] [#5f5f87]·[/] [#8787af]{short_path(self.office.workspace)}[/] "

    # ---------- side panel & status line ----------

    def tick(self):
        o = self.office
        if isinstance(self.screen, ApprovalScreen) and not o.pending:
            self.pop_screen()  # stopped / answered elsewhere
        chat = self.query_one("#chat")
        chat.border_title = self.chat_title()
        chat.border_subtitle = ""
        area = self.query_one("#input")
        area.border_title = " DeepSeek " if o.ds_on else " team "
        area.border_subtitle = " enter send · shift+enter newline · / commands · ↑ history · ctrl+o expand · esc stop "
        act = self.query_one("#activity", Static)
        spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 2) % 10]
        if self.flash and time.time() < self.flash[1]:
            act.update(Text(self.flash[0], WARN))
        elif o.busy:
            s = int(time.time() - ui.activity["since"]) if ui.activity["text"] else 0
            what = ui.activity["text"] or "working"
            act.update(Text.assemble((f"{spin} ", ACCENT), (what, "#ffd787"), (f" · {s // 60}:{s % 60:02d}", DIM),
                                     ("  esc to stop", FAINT)))
        else:
            act.update("")
        act.display = bool(o.busy or (self.flash and time.time() < self.flash[1]))

    # ---------- input ----------

    def on_prompt_area_submitted(self, event):
        text = event.text.strip()
        area = self.query_one("#input", PromptArea)
        area.clear()
        self.hide_popup()
        if not self.history or self.history[-1] != text:
            self.history.append(text)
            self.history = self.history[-500:]
            try:
                self.history_path.parent.mkdir(parents=True, exist_ok=True)
                self.history_path.write_text(json.dumps(self.history))
            except OSError:
                pass
        self.hist_pos = len(self.history)
        if text in ("/quit", "/exit"):
            self.exit()
            return
        if text in ("/status", "/usage"):
            self.office.usage.update_gpt()
            self._mount(session_card(self.office))
            return
        self.office.submit(text)
        self.tick()

    def history_step(self, step):
        if not self.history:
            return
        self.hist_pos = max(0, min(len(self.history), self.hist_pos + step))
        text = self.history[self.hist_pos] if self.hist_pos < len(self.history) else ""
        area = self.query_one("#input", PromptArea)
        area.load_text(text)
        area.move_cursor(area.document.end)

    # ---------- /command popup ----------

    def on_text_area_changed(self, event):
        text = event.text_area.text
        if not text.startswith("/") or "\n" in text:
            self.hide_popup()
            return
        words = text.split(" ")
        if len(words) == 1:
            items = [(c, m) for c, m in ui.COMMANDS.items() if c.startswith(words[0])]
        else:
            items = [(v, m) for v, m in self.office.completions(words[:-1]) if v.startswith(words[-1])]
        popup = self.query_one("#popup", OptionList)
        if not items or (len(items) == 1 and items[0][0] == words[-1]):
            self.hide_popup()
            return
        self.popup_word = words[-1]
        popup.set_options([Option(Text.assemble((v, "bold"), (f"  {m}", DIM)), id=v) for v, m in items])
        popup.highlighted = 0
        popup.display = True

    def popup_open(self):
        return self.query_one("#popup", OptionList).display

    def hide_popup(self):
        self.query_one("#popup", OptionList).display = False

    def move_popup(self, step):
        popup = self.query_one("#popup", OptionList)
        (popup.action_cursor_down if step > 0 else popup.action_cursor_up)()

    def accept_popup(self):
        popup = self.query_one("#popup", OptionList)
        if popup.highlighted is None:
            return
        value = popup.get_option_at_index(popup.highlighted).id
        area = self.query_one("#input", PromptArea)
        text = area.text
        area.load_text(text[: len(text) - len(self.popup_word)] + value + " ")
        area.move_cursor(area.document.end)
        self.hide_popup()

    def on_option_list_option_selected(self, event):
        self.accept_popup()
        self.query_one("#input").focus()

    # ---------- copy on select (like Claude Code / Codex) ----------

    def copy_text(self, text):
        if not text or not text.strip():
            return
        self.copy_to_clipboard(text)  # OSC 52 through the terminal
        if shutil.which("wl-copy"):  # Wayland clipboard too, in case the terminal blocks OSC 52
            try:
                subprocess.Popen(["wl-copy"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL).communicate(text.encode(), timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
        n = len(text)
        self.flash = (f"copied {n} character{'s' if n != 1 else ''}", time.time() + 1.5)
        self.tick()

    def on_text_selected(self, event: events.TextSelected):
        self.copy_text(self.screen.get_selected_text())

    def on_text_area_selection_changed(self, event):
        area = event.text_area
        if event.selection.is_empty:
            return
        # copy once the selection settles (mouse drag / shift+arrows)
        if getattr(self, "_copy_timer", None):
            self._copy_timer.stop()
        self._copy_timer = self.set_timer(0.4, lambda: self.copy_text(area.selected_text))

    # ---------- keys ----------

    def action_stop(self):
        if self.popup_open():
            self.hide_popup()
        elif self.office.busy:
            self.office.cancel()

    def action_ctrl_c(self):
        if isinstance(self.screen, ApprovalScreen):
            self.screen.dismiss("n")
            return
        area = self.query_one("#input", PromptArea)
        if self.office.busy:
            self.office.cancel()
        elif area.text:
            area.clear()
        elif time.time() - self.last_ctrl_c < 2:
            self.exit()
        else:
            self.last_ctrl_c = time.time()
            self.flash = ("press ctrl+c again to quit", time.time() + 2)
            self.tick()

    def action_quit_if_empty(self):
        if not self.query_one("#input", PromptArea).text:
            self.exit()

    def action_toggle_last(self):
        msgs = [m for m in self.query(ChatMessage) if m.collapsible]
        if msgs:
            msgs[-1].toggle()
            if msgs[-1].expanded:
                self.query_one("#chat").scroll_to_widget(msgs[-1], animate=False, top=True)

    def action_clear_chat(self):
        self.query_one("#chat").remove_children()


def run_app(office, history_path):
    app = OfficeApp(office, history_path)
    try:
        app.run()
    finally:
        ui.set_sink(None)
        office.stop()
