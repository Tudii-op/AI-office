"""Load editable prompts from AI-office/prompts/*.md (read on every use, so edits apply live)."""

from pathlib import Path
from string import Template

DIR = Path(__file__).resolve().parent.parent / "prompts"


def load(name, **values):
    return Template((DIR / f"{name}.md").read_text()).safe_substitute(values).strip()
