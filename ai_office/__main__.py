"""Entry point: python -m ai_office [-w WORKSPACE] [--fake]"""

import argparse
import os
import tomllib
from pathlib import Path

from .adapters import make_agent
from .deepseek import DeepSeek, FakeDeepSeek
from .office import Office

ROOT = Path(__file__).resolve().parent.parent


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    ap = argparse.ArgumentParser(prog="ai-office", description="Claude + GPT as coworkers, DeepSeek as manager.")
    ap.add_argument("-w", "--workspace", default=str(ROOT / "workspaces" / "default"),
                    help="folder the AIs work in (default: workspaces/default)")
    ap.add_argument("-c", "--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--fake", action="store_true", help="fake Claude/GPT/DeepSeek: test the flow for free")
    args = ap.parse_args()

    load_env(ROOT / ".env")
    cfg = tomllib.loads(Path(args.config).read_text()) if Path(args.config).exists() else {}

    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    agents = {k: make_agent(k, cfg, fake=args.fake) for k in ("claude", "gpt")}

    deepseek = None
    ds = cfg.get("deepseek", {})
    if args.fake:
        deepseek = FakeDeepSeek()
    elif ds.get("enabled", True):
        key = os.environ.get(ds.get("api_key_env", "DEEPSEEK_API_KEY"))
        if key:
            deepseek = DeepSeek(ds.get("base_url", "https://api.deepseek.com"),
                                ds.get("model", "deepseek-chat"), key, ds.get("timeout", 120))

    sub = "fake" if args.fake else ""  # fake runs never touch real logs/state
    Office(agents, workspace, cfg, deepseek=deepseek, log_dir=ROOT / "logs" / sub,
           state_dir=ROOT / "state" / sub, workspaces_dir=ROOT / "workspaces").loop()


if __name__ == "__main__":
    main()
