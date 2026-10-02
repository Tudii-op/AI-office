<!-- BEGIN:solo-developer's-rules -->

user: i always commit my self.
user: don't burn API/subscription credits testing; use `--fake` mode. i test live myself.

<!-- END:solo-developer's-rules -->

# AI Office

Terminal app: Claude (via `claude -p`) and GPT (via `codex exec`) work as builder/reviewer in one shared chat; the human chats only with DeepSeek (manager), which briefs/answers the team and reports back; it also routes turns and writes recaps. No DeepSeek key → human talks to the team directly.

- `ai_office/adapters.py`: one wrapper per CLI agent, plus `FakeAgent`
- `ai_office/deepseek.py`: DeepSeek API client (OpenAI-compatible)
- `ai_office/office.py`: chat history, turn loop, role prompts, commands
- `ai_office/gitops.py`: auto-commit per turn in the workspace
- `ai_office/tui.py`: default full-screen Textual app (one chat panel + session card, ANSI theme = terminal bg) (registers as `ui` sink; office output → chat widgets, approvals → modal)
- `ai_office/ui.py`: sink routing + `--plain` prompt_toolkit UI; plain input() when piped
- TUI test: Textual `run_test()` pilot (see scratch tests); never needs real AIs
- `ai_office/usage.py`: 5h/weekly usage from Claude stream-json + Codex session logs
- All AI instructions live in `prompts/*.md` (loaded per call via `ai_office/prompts.py`, `$var` templates)
- DeepSeek replies JSON `to: human|team|claude|gpt|office` + `actions` (settings, lookups: list_files/read_file/git_log, forget_chat): `Office.manager_loop`, `Office.apply_actions`
- Team turns end with `REPORT:` + `STATUS:`; DeepSeek sees reports via `Office.manager_context`
- DeepSeek chat memory persists in `state/manager_history.json`
- Permission modes full|ask|auto (`Office.perms_for`, `PERMISSION_MODES`); full mode auto-allows unless `ai_office/safety.py` flags a risk.
- Permissions: Claude gets `--permission-prompt-tool mcp__aioffice__approve` via `--mcp-config`; `ai_office/approval_mcp.py` (stdio MCP, stdlib) forwards to `approvals.ApprovalBridge` (unix socket) → `Office.handle_permission`: rules (`state/approvals.json`) → DeepSeek `approver.md` → human (y/a/n via `Office.submit`). Per-call settings: `Office.perms_for`. GPT: sandbox only.
- Fake approval test: `AI_OFFICE_FAKE_ASK="npm install x" ai-office --fake` (fake builder asks through the real MCP helper)
- `--fake` writes only to `logs/fake/` and `state/fake/` (but fake builder writes fake_output.txt into the workspace: never test with cwd = ~)
- Workspace defaults to cwd (can be ~). Auto-commit only in `workspaces/*` (`Office.commits_enabled`). Home guards in `safety._protected`.
- Dependencies: textual (full-screen UI), prompt_toolkit (--plain UI). Python 3.11+.
- Threads: UI/prompt on main thread, team work on one worker thread (`Office.work`); Esc/Ctrl+C → `adapters.cancel_running()` kills the agent's process group.
- Live: adapters stream stdout (`adapters._run_stream`) and call `on_event(kind, text)`; `Office.call_agent` updates `ui.set_activity`, logs, flags reviewer edits, runs `Office._narrate` (DeepSeek `narrator.md`) and a `git status` before/after check for non-builders.
- TUI: Claude/GPT `ChatMessage`s are collapsible (preview + REPORT/STATUS tail); Ctrl+O toggles the latest.
- Test scripts (fake, free): Textual pilot for app, live/narration/collapse; pty test for `--plain`.
