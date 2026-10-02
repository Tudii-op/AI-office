# AI Office

```
you  ⇄  DeepSeek (manager)
             ⇅  briefs · answers · reports
        Claude  ⇄  GPT   (builder / reviewer)
```
You chat only with DeepSeek. It briefs the team, answers their questions (or asks you), decides who speaks next, writes recaps to save tokens, and reports back. Claude and GPT talk to each other directly, and you see everything in one chat. With no DeepSeek key (or `/deepseek off`), you talk to the team directly.

Uses your **subscriptions** via the official CLIs (`claude`, `codex`). There are no Anthropic/OpenAI API keys involved. DeepSeek uses your API key.

## Setup
```bash
cp .env.example .env    # put your DeepSeek key in (optional)
claude                  # make sure you're logged in
codex login             # make sure you're logged in
```

## Run
```bash
ai-office                         # the AIs work where you run it (like claude / codex); ~ works too
ai-office -w ~/Workplace/foo      # or pick a folder
ai-office --fake                  # free dry run, no AI calls
ai-office --plain                 # classic inline terminal UI instead of the full-screen app
```
(installed globally with `uv tool install -e .`; or `python -m ai_office ...`)

**Workspace = the AIs' `./`.** Running from `~` makes your whole home folder their playground. Even then, Claude must ask before touching your config/dotfiles (`~/.config`, `~/.zshrc`, `~/.mydotfiles`…), secrets, or deleting/moving whole top-level folders (`~/Documents`, `~/Workplace/<project>`). GPT's sandbox covers the whole workspace and can't ask first.

## In the chat
- talk to DeepSeek; when there's work it sends a brief to the team
- each team AI ends with a one-line `REPORT:` (what changed, tests, blockers) and `STATUS: CONTINUE | DONE | ASK_MANAGER`; DeepSeek reads the reports instead of full messages to save tokens
- a round ends when the reviewer says DONE, DeepSeek says done/stuck, someone asks a question, or the turn limit is hit. Then DeepSeek answers the team or reports to you
- the input box is always live: messages typed while the team works are queued for the next turn; **Esc** or **Ctrl+C** stops them instantly
- ask one AI something without starting work: "ask GPT if Postgres or SQLite fits here". DeepSeek consults it (read-only) and relays the answer
- DeepSeek can check the team's work itself (list files, read a file, git log)
- just ask DeepSeek in plain words: "how's our usage?", "switch GPT to gpt-6-luna", "make Claude the reviewer", "start a new session", "work in ~/Workplace/foo"
- or use commands: `/usage` `/model claude|gpt [name]` `/swap` `/turns N` `/new` `/forget` `/permissions` `/approvals` `/deepseek on|off` `/status` `/quit`

## Permissions
Three modes. Switch live with `/permissions full|ask|auto` or tell DeepSeek. Default: **full**.

| mode | Claude | GPT |
|---|---|---|
| `full` | runs anything (git, files, builds, installs in the project, web). Only **risky/system** stuff is asked | sandbox confined to the project, internet on |
| `ask` | every command not pre-allowed goes to DeepSeek → you | sandbox, no internet |
| `auto` | Claude Code's own auto mode decides | sandbox |

**Risky** (full mode, see `ai_office/safety.py`; tune freely): sudo/su, system packages (pacman, yay…), systemctl, reboot, disk tools, kill/pkill, hyprctl, global npm/pip installs, writes/deletes outside the project (also after `cd`), your config/dotfiles, deleting/moving whole top-level folders, secrets (`~/.ssh`, `~/.aws`, `.env`, credentials), `curl … | sh`, git push / reset --hard / clean -f / global config.

Risky requests go: your saved "always" rules → DeepSeek (`prompts/approver.md`) → you:
```
🔐 Claude (builder) wants to run: sudo pacman -S htop
   ⚠ flagged: `sudo` runs as root
   [y] allow  [a] always (Bash(sudo pacman:*))  [n] deny  · or type a reason to deny
```
- "always" rules: `state/approvals.json` (`/approvals`, `/approvals clear`). Chained/redirected commands never match a rule.
- Reviewer/consult may run commands but never edit files.
- GPT can't ask first (`codex exec` has no approval hook), so its sandbox blocks system-level stuff instead. `gpt_unsandboxed = true` removes even that (no checks at all).

## Customize
- `prompts/*.md`: every instruction each AI gets (manager, router, recap, builder, reviewer, consult…). Edits apply on the next message. See `prompts/README.md`.
- `config.toml`: turn limits, recap frequency, models list, DeepSeek model, timeouts.
- DeepSeek remembers your chat across restarts (`state/manager_history.json`); `/forget` clears it.

## Live view
- While Claude/GPT work, the line above the input shows their current action (`Claude › ran pnpm vitest`), streamed live from `claude --output-format stream-json` / `codex exec --json`, at no extra cost.
- Every `live_summary_every` seconds (default 45, `config.toml`), DeepSeek posts a one-line update (`prompts/narrator.md`).
- Claude/GPT replies start **collapsed**: a 3-line preview + their REPORT/STATUS line. Click the reply or press **Ctrl+O** (latest) to expand.
- Reviewers may write temp/test files (GPT gets a writable sandbox so tests run), but any project edit by a reviewer is flagged live and double-checked with `git status` after the turn.

## Screen
Full-screen TUI (Textual, uses your terminal background): one chat panel that starts with a session card (team, models, usage bars, manager, settings; `/status` posts a fresh one), REPORT/STATUS lines shown as a colored footer, input at the bottom, approval requests as a dialog (Allow / Always / Deny, or type a reason). Mouse scroll works; every session is also saved in `logs/`.

## Keys
| key | does |
|---|---|
| Enter | send |
| Shift+Enter · Ctrl+J · Alt+Enter · `\` at line end | new line |
| Esc / Ctrl+C | stop the AIs (working) · clear input (idle) |
| Ctrl+←/→, Alt+B/F | jump by word |
| Shift+arrows | select |
| Ctrl+W / Ctrl+Backspace | delete word |
| Ctrl+U / Ctrl+K | delete to line start / end |
| ↑/↓ | move between lines, then history |
| `/` + Tab or Enter | command popup (↑/↓ to pick) |
| Ctrl+O / click | expand or collapse a Claude/GPT reply |
| Ctrl+L | clear chat |
| mouse drag | select text → copied automatically (terminal clipboard + wl-copy); Shift+drag = kitty’s own selection |
| Ctrl+D or Ctrl+C twice | quit |


## Usage tracking
Claude's and GPT's 5-hour and weekly subscription limits are recorded after every turn (from Claude's `rate_limit_event` and Codex's local session logs), so there are no extra calls. DeepSeek sees them and warns you above ~80%. Model/role/turn changes are remembered in `state/`.

Auto-commit (`[Claude] ...` per turn) happens only in AI Office's own `workspaces/*` folders by default; in your real projects you commit yourself (`auto_commit` in `config.toml`). Chat logs go to `logs/`.

## Roles
- builder: can edit files (Claude: `acceptEdits`; Codex: `workspace-write` sandbox)
- reviewer: read-only (Claude: Read/Grep/Glob only; Codex: `read-only` sandbox)
