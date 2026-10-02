# Prompts

Every instruction the AIs get lives here. Edit freely; changes apply on the next message (no restart).
Variables like `$me` are filled in by the office (unknown ones are left as-is).

| file | who | used for | variables |
|---|---|---|---|
| `manager.md` | DeepSeek | talking to you, running the office | none |
| `router.md` | DeepSeek | picking who speaks next in the team | none |
| `recap.md` | DeepSeek | squashing old team messages | none |
| `team.md` | Claude/GPT | shared intro for team turns | `$me $other $role $boss` |
| `builder.md` | builder | team turn (edits files) | `$me $other $ask` |
| `reviewer.md` | reviewer | team turn (read-only) | `$me $other $ask` |
| `approver.md` | DeepSeek | approving commands/edits Claude asks permission for | none |
| `consult.md` | Claude/GPT | DeepSeek asks one of them a question | `$me` |
| `boss_manager.md` / `boss_human.md` | | fills `$boss` (DeepSeek on / off) | |
