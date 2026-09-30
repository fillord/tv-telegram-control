#!/usr/bin/env python3
"""Move the Telegram token from config.json to a protected secret file."""

import json
import os
import sys
from pathlib import Path


def atomic_json(path, data):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def main():
    if len(sys.argv) != 4 or sys.argv[1] not in {"prepare", "finalize"}:
        raise SystemExit("usage: migrate_token.py prepare|finalize CONFIG SECRET")
    mode, config_name, secret_name = sys.argv[1:]
    config_path = Path(config_name)
    secret_path = Path(secret_name)
    with config_path.open(encoding="utf-8") as file:
        config = json.load(file)
    token = str(config.get("telegram_token", "")).strip()
    if mode == "prepare":
        if not token and secret_path.exists():
            token = secret_path.read_text(encoding="utf-8").strip()
        if not token or token.startswith("PASTE_"):
            raise SystemExit("Telegram token is missing")
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = secret_path.with_name(secret_path.name + ".tmp")
        temporary.write_text(token + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, secret_path)
        return
    if not secret_path.exists() or not secret_path.read_text(encoding="utf-8").strip():
        raise SystemExit("Telegram secret is missing; refusing to modify config")
    if "telegram_token" in config:
        config.pop("telegram_token")
        atomic_json(config_path, config)


if __name__ == "__main__":
    main()
