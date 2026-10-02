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
- `ai_office/ui.py`: terminal rendering/input
- Standard library only. Python 3.11+.
