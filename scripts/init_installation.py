#!/usr/bin/env python3
"""Create a customer-specific runtime configuration without exposing secrets."""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def positive_user_id(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Telegram ID должен быть целым числом") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("Telegram ID должен быть положительным")
    return result


def valid_port(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("ADB-порт должен быть числом") from exc
    if not 1 <= result <= 65535:
        raise argparse.ArgumentTypeError("ADB-порт должен быть от 1 до 65535")
    return result


def valid_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise argparse.ArgumentTypeError(f"Неизвестный часовой пояс: {value}") from exc
    return value


def valid_ip(value: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as exc:
        raise argparse.ArgumentTypeError("Нужен корректный IPv4-адрес телевизора") from exc


def valid_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(char.isspace() for char in value)
    ):
        raise argparse.ArgumentTypeError("Нужна полная HTTPS-ссылка без логина и пароля")
    return value


def valid_mac(value: str) -> str:
    normalized = value.strip().upper().replace("-", ":")
    if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", normalized):
        raise argparse.ArgumentTypeError("MAC должен иметь вид AA:BB:CC:DD:EE:FF")
    return normalized


def detect_timezone() -> str:
    timezone_file = Path("/etc/timezone")
    if timezone_file.is_file():
        candidate = timezone_file.read_text(encoding="utf-8").strip()
        try:
            return valid_timezone(candidate)
        except argparse.ArgumentTypeError:
            pass
    return "UTC"


def write_private(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def obtain_token(secret_path: Path) -> None:
    if secret_path.is_file() and secret_path.read_text(encoding="utf-8").strip():
        secret_path.chmod(0o600)
        return
    if not sys.stdin.isatty():
        raise SystemExit(
            "Нет secrets/telegram_token. Запустите команду в терминале, чтобы ввести токен скрыто."
        )
    token = getpass.getpass("Токен нового Telegram-бота (ввод скрыт): ").strip()
    if len(token) < 20 or ":" not in token:
        raise SystemExit("Токен выглядит некорректно; конфигурация не создана")
    write_private(secret_path, token + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Подготовить отдельную установку TV Telegram Control для заказчика"
    )
    parser.add_argument("--telegram-user-id", action="append", required=True, type=positive_user_id)
    parser.add_argument("--tv-name", required=True)
    parser.add_argument("--tv-ip", required=True, type=valid_ip)
    parser.add_argument("--tv-port", default=5555, type=valid_port)
    parser.add_argument("--tv-url", required=True, type=valid_url)
    parser.add_argument("--tv-mac", type=valid_mac)
    parser.add_argument("--timezone", default=detect_timezone(), type=valid_timezone)
    parser.add_argument("--container-name", default="tv-telegram-control")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    config_path = root / "data" / "config.json"
    secret_path = root / "secrets" / "telegram_token"
    env_path = root / ".env"

    tv_name = args.tv_name.strip()
    if not tv_name or len(tv_name) > 60:
        raise SystemExit("Название телевизора должно содержать от 1 до 60 символов")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", args.container_name):
        raise SystemExit("Некорректное имя контейнера")

    if config_path.exists() or env_path.exists():
        raise SystemExit(
            "Установка уже инициализирована: data/config.json или .env существует. "
            "Существующие данные не изменены."
        )

    for directory in (root / "data", root / "logs", root / "adb", root / "secrets"):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)

    obtain_token(secret_path)

    template = json.loads((PROJECT_ROOT / "config.example.json").read_text(encoding="utf-8"))
    television = {
        "id": "tv_" + uuid.uuid4().hex[:12],
        "name": tv_name,
        "ip": args.tv_ip,
        "port": args.tv_port,
        "url": args.tv_url,
        "auto_refresh": False,
        "group": "Основная группа",
        "schedule": {
            "enabled": False,
            "on": "09:00",
            "off": "22:00",
            "days": list(range(7)),
        },
    }
    if args.tv_mac:
        television["mac"] = args.tv_mac

    template["allowed_user_ids"] = list(dict.fromkeys(args.telegram_user_id))
    template["timezone"] = args.timezone
    template["tvs"] = [television]
    write_private(config_path, json.dumps(template, ensure_ascii=False, indent=2) + "\n")

    env_content = (
        "COMPOSE_PROJECT_NAME=tv-telegram-control\n"
        f"TV_BOT_CONTAINER_NAME={args.container_name}\n"
        f"TV_BOT_UID={os.getuid()}\n"
        f"TV_BOT_GID={os.getgid()}\n"
        f"TV_BOT_TIMEZONE={args.timezone}\n"
        "TV_BOT_HTTP_ALLOWED_URLS=\n"
    )
    write_private(env_path, env_content)

    print("Установка подготовлена.")
    print(f"Конфигурация: {config_path}")
    print("Токен сохранён отдельно и не выводился.")
    print("Следующий шаг: docker compose build && docker compose run --rm tv-telegram-control python3 /app/tv_bot.py --check-config")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
