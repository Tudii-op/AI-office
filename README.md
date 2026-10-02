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
uv run ai-office                         # works in workspaces/default
uv run ai-office -w ~/Workplace/foo      # work on a real project folder
uv run ai-office --fake                  # free dry run, no AI calls
```
(or `python -m ai_office ...`)

## In the chat
- talk to DeepSeek; when there's work it sends a brief to the team
- each team AI ends with `STATUS: CONTINUE | DONE | ASK_MANAGER`
- a round ends when the reviewer says DONE, DeepSeek says done/stuck, someone asks a question, or the turn limit is hit. Then DeepSeek answers the team or reports to you
- type a line + Enter while they work to interject; **Ctrl+C** to pause
- just ask DeepSeek in plain words: "how's our usage?", "switch GPT to gpt-6-luna", "make Claude the reviewer", "start a new session", "work in ~/Workplace/foo"
- or use commands: `/usage` `/model claude|gpt [name]` `/swap` `/turns N` `/new` `/deepseek on|off` `/status` `/quit`

## Usage tracking
Claude's and GPT's 5-hour and weekly subscription limits are recorded after every turn (from Claude's `rate_limit_event` and Codex's local session logs), so there are no extra calls. DeepSeek sees them and warns you above ~80%. Model/role/turn changes are remembered in `state/`.

Every turn that changes files is auto-committed in the workspace (`[Claude] ...`), so `git revert` undoes a bad round. Chat logs go to `logs/`.

## Roles
- builder: can edit files (Claude: `acceptEdits`; Codex: `workspace-write` sandbox)
- reviewer: read-only (Claude: Read/Grep/Glob only; Codex: `read-only` sandbox)
