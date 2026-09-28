#!/usr/bin/env python3
"""Telegram controls for trusted Android TV devices on a local network."""

import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("TV_BOT_CONFIG", ROOT / "config.json")).expanduser()
CONFIG_LOCK = threading.RLock()
TV_LOCKS_LOCK = threading.Lock()
TV_LOCKS = {}
ADB_RECOVERY_LOCK = threading.Lock()
ADB_LAST_RECOVERY = 0.0
ADB_RECOVERY_COOLDOWN_SECONDS = 10.0
PENDING_URL = {}
PENDING_ADD_TV = {}
PENDING_SCHEDULE = {}
WEEKDAY_LABELS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
KEY_ACTIONS = {
    "voldown": ("25", "🔉 Тише"),
    "volup": ("24", "🔊 Громче"),
    "mute": ("164", "🔇 Mute"),
    "up": ("19", "⬆️ Вверх"),
    "down": ("20", "⬇️ Вниз"),
    "left": ("21", "⬅️ Влево"),
    "right": ("22", "➡️ Вправо"),
    "enter": ("23", "🔘 OK"),
    "back": ("4", "↩ Назад"),
    "home": ("3", "🏠 Домой"),
}
KEEP_AWAKE_COMMANDS = (
    ("settings", "put", "global", "stay_on_while_plugged_in", "7"),
    ("settings", "put", "system", "screen_off_timeout", "2147483647"),
    ("settings", "put", "secure", "sleep_timeout", "-1"),
    ("settings", "put", "secure", "screensaver_enabled", "0"),
    ("svc", "power", "stayon", "true"),
)


def atomic_write_config(data):
    temporary = CONFIG.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, CONFIG)


def get_tv_lock(tv):
    key = tv.get("id") or f'{tv.get("ip")}:{tv.get("port", 5555)}'
    with TV_LOCKS_LOCK:
        return TV_LOCKS.setdefault(key, threading.RLock())


def validate_mac(value):
    value = value.strip().upper().replace("-", ":")
    if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", value):
        raise ValueError("MAC-адрес должен иметь вид AA:BB:CC:DD:EE:FF")
    return value


def configure_logging():
    log_dir = Path(os.environ.get("TV_BOT_LOG_DIR", ROOT / "logs")).expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "tv_bot.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    stream = logging.StreamHandler()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(threadName)s: %(message)s")
    handler.setFormatter(formatter)
    stream.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[handler, stream])


def load_config():
    with CONFIG_LOCK:
        with CONFIG.open(encoding="utf-8") as file:
            stored = json.load(file)
        os.chmod(CONFIG, 0o600)
        cfg = dict(stored)
    token = os.environ.get("TV_BOT_TOKEN", cfg.get("telegram_token", ""))
    if not token or token.startswith("PASTE_"):
        raise ValueError("Укажите telegram_token в config.json или TV_BOT_TOKEN")
    cfg["telegram_token"] = token
    allowed_ids = cfg.get("allowed_user_ids", [])
    if not isinstance(allowed_ids, list):
        raise ValueError("allowed_user_ids должен быть списком Telegram ID")
    cfg["allowed_user_ids"] = {int(x) for x in allowed_ids}
    cfg["refresh_interval_seconds"] = max(
        30, int(cfg.get("refresh_interval_seconds", 60))
    )
    cfg["healthcheck_interval_seconds"] = max(
        30, int(cfg.get("healthcheck_interval_seconds", 60))
    )
    cfg["healthcheck_failure_threshold"] = max(
        1, int(cfg.get("healthcheck_failure_threshold", 3))
    )
    cfg["healthcheck_recovery_threshold"] = max(
        1, int(cfg.get("healthcheck_recovery_threshold", 2))
    )
    cfg["keep_awake_interval_seconds"] = max(
        30, int(cfg.get("keep_awake_interval_seconds", 60))
    )
    auto_refresh = cfg.get("auto_refresh", True)
    if not isinstance(auto_refresh, bool):
        raise ValueError("auto_refresh должен быть true или false без кавычек")
    cfg["auto_refresh"] = auto_refresh
    keep_awake = cfg.get("keep_awake", True)
    if not isinstance(keep_awake, bool):
        raise ValueError("keep_awake должен быть true или false без кавычек")
    cfg["keep_awake"] = keep_awake
    timezone_name = str(cfg.get("timezone", "Asia/Almaty")).strip()
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Неизвестный часовой пояс: {timezone_name}") from exc
    cfg["timezone"] = timezone_name
    adb_path = Path(
        os.environ.get("TV_BOT_ADB_PATH", cfg.get("adb_path", ""))
    ).expanduser()
    if not adb_path.is_file() or not os.access(adb_path, os.X_OK):
        raise ValueError(f"ADB не найден или не исполняется: {adb_path}")
    cfg["adb_path"] = str(adb_path)
    if not cfg.get("tvs"):
        raise ValueError("Добавьте хотя бы один телевизор в tvs")
    seen_ids = set()
    seen_endpoints = set()
    config_changed = False
    for tv in cfg["tvs"]:
        if not tv.get("name") or not tv.get("ip"):
            raise ValueError("У каждого ТВ нужны name и ip")
        tv["name"] = tv["name"].strip()
        if len(tv["name"]) > 60:
            raise ValueError("Название ТВ не должно быть длиннее 60 символов")
        try:
            ipaddress.IPv4Address(tv["ip"])
        except ValueError as exc:
            raise ValueError(f'Некорректный IPv4-адрес ТВ «{tv["name"]}»: {tv["ip"]}') from exc
        try:
            tv["port"] = int(tv.get("port", 5555))
        except (TypeError, ValueError) as exc:
            raise ValueError("Неверный ADB-порт") from exc
        if not (1 <= tv["port"] <= 65535):
            raise ValueError("Неверный ADB-порт")
        tv["url"] = validate_url(tv.get("url", ""))
        if tv.get("mac"):
            tv["mac"] = validate_mac(tv["mac"])
        if "schedule" in tv:
            tv["schedule"] = normalize_schedule(tv["schedule"])
        tv_id = str(tv.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,32}", tv_id):
            tv_id = uuid.uuid4().hex[:12]
            tv["id"] = tv_id
            config_changed = True
        endpoint = (tv["ip"], tv["port"])
        if tv_id in seen_ids:
            raise ValueError(f'Повторяющийся ID телевизора: {tv_id}')
        if endpoint in seen_endpoints:
            raise ValueError(f'Повторяющийся адрес телевизора: {tv["ip"]}:{tv["port"]}')
        seen_ids.add(tv_id)
        seen_endpoints.add(endpoint)
    if config_changed:
        with CONFIG_LOCK:
            atomic_write_config(stored)
    return cfg


def telegram(cfg, method, payload):
    url = f'https://api.telegram.org/bot{cfg["telegram_token"]}/{method}'
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        body = json.load(response)
    if not body.get("ok"):
        raise RuntimeError(body.get("description", "Ошибка Telegram API"))
    return body["result"]


def send(cfg, chat_id, message, markup=None):
    payload = {"chat_id": chat_id, "text": message[:4000]}
    if markup is not None:
        payload["reply_markup"] = markup
    telegram(cfg, "sendMessage", payload)


def edit_message(cfg, chat_id, message_id, message, markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": message[:4000]}
    if markup is not None:
        payload["reply_markup"] = markup
    try:
        telegram(cfg, "editMessageText", payload)
    except Exception:
        send(cfg, chat_id, message, markup=markup)


def send_telegram_file(
    cfg, chat_id, method, file_field, file_bytes, filename,
    caption=None, reply_markup=None, content_type="image/png"
):
    boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
    parts = []

    def add_field(name, value):
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n'
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{value}\r\n".encode("utf-8")
        )

    add_field("chat_id", str(chat_id))
    if caption:
        add_field("caption", str(caption)[:1024])
    if reply_markup is not None:
        add_field("reply_markup", json.dumps(reply_markup, ensure_ascii=False))

    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
    )
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    body = b"".join(parts)
    url = f'https://api.telegram.org/bot{cfg["telegram_token"]}/{method}'
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Ошибка Telegram API"))
    return result["result"]


def send_photo(cfg, chat_id, photo_bytes, caption="", filename="screenshot.png"):
    try:
        return send_telegram_file(
            cfg, chat_id, "sendPhoto", "photo", photo_bytes,
            filename=filename, caption=caption
        )
    except Exception:
        return send_telegram_file(
            cfg, chat_id, "sendDocument", "document", photo_bytes,
            filename=filename, caption=caption
        )


def send_chat_action(cfg, chat_id, action="upload_photo"):
    try:
        telegram(cfg, "sendChatAction", {"chat_id": chat_id, "action": action})
    except Exception:
        pass


def set_bot_commands(cfg):
    commands = [
        {"command": "start", "description": "Главное меню телевизоров"},
        {"command": "addtv", "description": "Добавить телевизор по IP"},
        {"command": "schedule", "description": "Расписание включения и ожидания"},
        {"command": "screenshot", "description": "Сделать скриншот с экрана ТВ"},
        {"command": "mute", "description": "Включить/выключить звук"},
        {"command": "volup", "description": "Сделать громче"},
        {"command": "voldown", "description": "Сделать тише"},
        {"command": "id", "description": "Узнать свой Telegram ID"},
        {"command": "cancel", "description": "Отмена текущего действия"},
    ]
    try:
        telegram(cfg, "setMyCommands", {"commands": commands})
    except Exception:
        pass


def menu(cfg, statuses=None):
    if statuses is None:
        statuses = get_all_tv_statuses(cfg)
    rows = []
    for tv in cfg["tvs"]:
        code, label, icon = statuses.get(tv["id"], ("offline", "...", "⚪"))
        rows.append([{"text": f"{icon} {tv['name']} ({label})", "callback_data": f"select:{tv['id']}"}])
    if len(cfg["tvs"]) > 1:
        rows.append([{"text": "📺 Все телевизоры", "callback_data": "select:all"}])
    rows.append([
        {"text": "➕ Добавить ТВ", "callback_data": "addtv_start"},
        {"text": "🔄 Обновить статусы", "callback_data": "refresh_menu"}
    ])
    rows.append([{"text": "🕒 Расписание", "callback_data": "schedule_menu"}])
    return {"inline_keyboard": rows}


def screenshot_menu(cfg, statuses=None):
    if statuses is None:
        statuses = get_all_tv_statuses(cfg)
    rows = []
    for tv in cfg["tvs"]:
        code, label, icon = statuses.get(tv["id"], ("offline", "...", "⚪"))
        rows.append([{"text": f"📸 {icon} {tv['name']}", "callback_data": f"screenshot:{tv['id']}"}])
    if len(cfg["tvs"]) > 1:
        rows.append([{"text": "📸 Все телевизоры", "callback_data": "screenshot:all"}])
    rows.append([{"text": "↩ Главное меню", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def actions(target):
    rows = [
        [{"text": "▶ Включить", "callback_data": f"on:{target}"},
         {"text": "⏸ Ожидание", "callback_data": f"off:{target}"}],
        [{"text": "🌐 Открыть сайт", "callback_data": f"web:{target}"},
         {"text": "▶🌐 Включить + сайт", "callback_data": f"both:{target}"}],
        [{"text": "🖥 Не засыпать", "callback_data": f"screen:{target}"},
         {"text": "🔄 Перезагрузить", "callback_data": f"rebootask:{target}"}],
        [{"text": "📸 Скриншот", "callback_data": f"screenshot:{target}"},
         {"text": "🔗 Сменить сайт", "callback_data": f"seturl:{target}"}],
        [{"text": "🕒 Расписание", "callback_data": f"schedule:{target}"}],
        [{"text": "🔉 Тише", "callback_data": f"voldown:{target}"},
         {"text": "🔇 Mute", "callback_data": f"mute:{target}"},
         {"text": "🔊 Громче", "callback_data": f"volup:{target}"}],
        [{"text": "⬆️", "callback_data": f"up:{target}"}],
        [{"text": "⬅️", "callback_data": f"left:{target}"},
         {"text": "🔘 OK", "callback_data": f"enter:{target}"},
         {"text": "➡️", "callback_data": f"right:{target}"}],
        [{"text": "⬇️", "callback_data": f"down:{target}"}],
        [{"text": "↩ Назад", "callback_data": f"back:{target}"},
         {"text": "🏠 Домой", "callback_data": f"home:{target}"}],
    ]
    if target != "all":
        rows.append([{"text": "🗑 Удалить этот ТВ", "callback_data": f"deleteask:{target}"}])
    rows.append([{"text": "↩ Выбрать ТВ", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def reboot_confirmation(target):
    return {"inline_keyboard": [
        [{"text": "✅ Да, перезагрузить", "callback_data": f"reboot:{target}"}],
        [{"text": "❌ Отмена", "callback_data": f"select:{target}"}],
    ]}


def delete_confirmation(target):
    return {"inline_keyboard": [
        [{"text": "⚠️ Да, удалить телевизор", "callback_data": f"delete:{target}"}],
        [{"text": "❌ Отмена", "callback_data": f"select:{target}"}],
    ]}


def schedule_target_menu(cfg):
    rows = [
        [{"text": f"📺 {tv['name']}", "callback_data": f"schedule:{tv['id']}"}]
        for tv in cfg["tvs"]
    ]
    if len(cfg["tvs"]) > 1:
        rows.append([{"text": "📺 Все телевизоры", "callback_data": "schedule:all"}])
    rows.append([{"text": "↩ Главное меню", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


def schedule_controls(target):
    return {"inline_keyboard": [
        [{"text": "✏️ Настроить", "callback_data": f"schedset:{target}"}],
        [{"text": "⏸ Отключить расписание", "callback_data": f"schedoff:{target}"}],
        [{"text": "↩ Назад", "callback_data": "schedule_menu"}],
    ]}


def adb(cfg, *args, timeout=12):
    exe = cfg.get("adb_path", str(Path.home() / "Downloads/platform-tools/adb"))
    try:
        result = subprocess.run(
            [exe, *args], capture_output=True, text=True, timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    message = (result.stdout + "\n" + result.stderr).strip()
    return result.returncode == 0, message


def adb_bytes(cfg, *args, timeout=20):
    exe = cfg.get("adb_path", str(Path.home() / "Downloads/platform-tools/adb"))
    try:
        result = subprocess.run(
            [exe, *args], capture_output=True, timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, b"", str(exc)
    err = result.stderr.decode("utf-8", errors="replace").strip()
    return result.returncode == 0, result.stdout, err


def is_device_reachable(ip, port=5555, timeout=1.0):
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


def recover_adb_server(cfg):
    """Restart a stuck local ADB server, at most once per cooldown window."""
    global ADB_LAST_RECOVERY
    with ADB_RECOVERY_LOCK:
        now = time.monotonic()
        if now - ADB_LAST_RECOVERY < ADB_RECOVERY_COOLDOWN_SECONDS:
            return True
        logging.warning("ADB-сервер завис: выполняется автоматический перезапуск")
        adb(cfg, "kill-server", timeout=5)
        ok, output = adb(cfg, "start-server", timeout=8)
        if not ok:
            logging.warning("Не удалось перезапустить ADB-сервер: %s", output)
            return False
        ADB_LAST_RECOVERY = time.monotonic()
        return True


def adb_connect_failed(output):
    text = output.lower()
    return any(
        marker in text
        for marker in (
            "failed",
            "unable",
            "cannot connect",
            "no route",
            "timed out",
        )
    )


def wake_on_lan(mac, broadcast="255.255.255.255", tv_ip=None):
    hex_mac = mac.replace(":", "").replace("-", "")
    if len(hex_mac) != 12:
        raise ValueError("Неверный MAC-адрес")
    packet = b"\xff" * 6 + bytes.fromhex(hex_mac) * 16
    targets = [broadcast]
    if broadcast == "255.255.255.255" and tv_ip:
        parts = tv_ip.split(".")
        if len(parts) == 4:
            targets.append(f"{parts[0]}.{parts[1]}.{parts[2]}.255")
    sent = False
    for target in targets:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(packet, (target, 9))
            sent = True
        except OSError:
            pass
    if not sent:
        raise OSError("Не удалось отправить Wake-on-LAN пакет")


def connect(cfg, tv, wake=False):
    port = int(tv.get("port", 5555))
    address = f'{tv["ip"]}:{port}'
    if wake and tv.get("mac"):
        try:
            wake_on_lan(tv["mac"], tv.get("broadcast", "255.255.255.255"), tv_ip=tv["ip"])
        except (ValueError, OSError) as exc:
            logging.warning("Wake-on-LAN для %s: %s", tv["name"], exc)
        time.sleep(2)

    # Быстрая сокет-проверка, исключающая долгое 10-секундное зависание при выключенном ТВ
    if not is_device_reachable(tv["ip"], port, timeout=1.0):
        adb(cfg, "disconnect", address, timeout=2)
        return None, f"ТВ недоступен по сети (порт {port} закрыт или ТВ выключен)"

    ok, output = adb(cfg, "connect", address, timeout=8)
    if not ok or adb_connect_failed(output):
        adb(cfg, "disconnect", address, timeout=2)
        # ADB can keep a stale route internally even though the TV port is open.
        # Restart the local daemon once and retry instead of returning a false
        # "No route to host" error to the user.
        if is_device_reachable(tv["ip"], port, timeout=1.0):
            recover_adb_server(cfg)
            ok, output = adb(cfg, "connect", address, timeout=8)
        if not ok or adb_connect_failed(output):
            adb(cfg, "disconnect", address, timeout=2)
            return None, output or "Нет соединения по ADB"

    ok, output = adb(cfg, "-s", address, "get-state", timeout=5)
    if not ok or output.strip() != "device":
        adb(cfg, "disconnect", address, timeout=2)
        if "offline" in output.lower():
            return None, "ТВ в режиме offline (перезагрузите отладку по ADB на ТВ)"
        if "unauthorized" in output.lower():
            return None, "ADB не авторизован на ТВ (подтвердите запрос на экране)"
        return None, output or "ADB не авторизован на ТВ"

    return address, ""


def apply_keep_awake_settings(cfg, address):
    for command in KEEP_AWAKE_COMMANDS:
        ok, output = adb(cfg, "-s", address, "shell", *command, timeout=6)
        if not ok:
            return False, output or f"Не выполнена команда: {' '.join(command)}"
    return True, ""


def wake_tv_with_retry(cfg, tv, address):
    """Wake a TV and recover once from a stuck ADB shell command."""
    ok, output = adb(
        cfg, "-s", address, "shell", "input", "keyevent", "224", timeout=6
    )
    if ok:
        return True, output

    adb(cfg, "disconnect", address, timeout=2)
    logging.warning(
        'Пробуждение «%s» зависло, выполняется повторное подключение', tv["name"]
    )
    if tv.get("mac"):
        try:
            wake_on_lan(
                tv["mac"],
                tv.get("broadcast", "255.255.255.255"),
                tv_ip=tv["ip"],
            )
            time.sleep(3)
        except (ValueError, OSError) as exc:
            logging.warning('Wake-on-LAN для «%s»: %s', tv["name"], exc)

    retry_address, connect_error = connect(cfg, tv, wake=False)
    if not retry_address:
        return False, connect_error or output
    ok, retry_output = adb(
        cfg, "-s", retry_address, "shell", "input", "keyevent", "224", timeout=8
    )
    if not ok:
        adb(cfg, "disconnect", retry_address, timeout=2)
    return ok, retry_output


def _ensure_tv_awake_unlocked(cfg, tv):
    port = int(tv.get("port", 5555))
    if not is_device_reachable(tv["ip"], port, timeout=1.0) and tv.get("mac"):
        try:
            wake_on_lan(
                tv["mac"],
                tv.get("broadcast", "255.255.255.255"),
                tv_ip=tv["ip"],
            )
            time.sleep(5)
        except (ValueError, OSError) as exc:
            return False, f"Wake-on-LAN: {exc}"

    address, error = connect(cfg, tv, wake=False)
    if not address:
        return False, error

    ok, error = apply_keep_awake_settings(cfg, address)
    if not ok:
        return False, error

    ok, power = adb(cfg, "-s", address, "shell", "dumpsys", "power", timeout=5)
    asleep = ok and any(
        marker in power
        for marker in (
            "mWakefulness=Asleep",
            "mWakefulness=Dozing",
            "Display Power: state=OFF",
        )
    )
    if asleep:
        ok, output = adb(
            cfg, "-s", address, "shell", "input", "keyevent", "224", timeout=6
        )
        if not ok:
            return False, output or "Не удалось разбудить экран"
        logging.info('Телевизор «%s» автоматически разбужен', tv["name"])
    return True, ""


def ensure_tv_awake(cfg, tv):
    with get_tv_lock(tv):
        # Re-read state after taking the lock: this loop may hold an older
        # snapshot while the user explicitly sends the TV to standby.
        with CONFIG_LOCK:
            current_tv = next(
                (item for item in cfg["tvs"] if item.get("id") == tv.get("id")),
                tv,
            )
            current_tv = dict(current_tv)
        if current_tv.get("manual_sleep", False):
            return True, ""
        return _ensure_tv_awake_unlocked(cfg, current_tv)


def _keep_awake_loop_forever(cfg):
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="keep_awake")
    while True:
        with CONFIG_LOCK:
            televisions = [dict(tv) for tv in cfg["tvs"]]
            interval = cfg["keep_awake_interval_seconds"]
        if televisions:
            results = list(pool.map(lambda tv: ensure_tv_awake(cfg, tv), televisions))
            for tv, (ok, error) in zip(televisions, results):
                if not ok:
                    logging.warning('Контроль питания %s: %s', tv["name"], error)
        time.sleep(interval)


def keep_awake_loop(cfg):
    while True:
        try:
            _keep_awake_loop_forever(cfg)
        except Exception:
            logging.exception("Контроль питания аварийно перезапускается")
            time.sleep(10)


def _get_tv_status_unlocked(cfg, tv):
    """
    Определяет статус ТВ:
      ('on', 'Включен', '🟢')
      ('sleep', 'Режим ожидания', '💤')
      ('offline', 'Оффлайн', '🔴')
      ('unauthorized', 'Не авторизован', '🟡')
    """
    port = int(tv.get("port", 5555))
    if not is_device_reachable(tv["ip"], port, timeout=0.8):
        return "offline", "Оффлайн", "🔴"

    address, error = connect(cfg, tv, wake=False)
    if not address:
        err_lower = error.lower()
        if "не авторизован" in err_lower or "unauthorized" in err_lower:
            return "unauthorized", "Не авторизован", "🟡"
        if "offline" in err_lower:
            return "offline", "ADB оффлайн", "🔴"
        return "offline", "Оффлайн", "🔴"

    # 1. Проверка dumpsys power (стандарт для Android TV)
    ok, output = adb(cfg, "-s", address, "shell", "dumpsys", "power", timeout=4)
    if ok and output:
        for line in output.splitlines():
            line_str = line.strip()
            if "mWakefulness=" in line_str:
                wake_val = line_str.split("mWakefulness=", 1)[1].split()[0].lower()
                if wake_val == "awake":
                    return "on", "Включен", "🟢"
                if wake_val in {"asleep", "dozing", "dreaming"}:
                    return "sleep", "Сон", "💤"
            if "Display Power: state=" in line_str:
                p_state = line_str.split("Display Power: state=", 1)[1].split()[0].upper()
                if p_state == "ON":
                    return "on", "Включен", "🟢"
                if p_state == "OFF":
                    return "sleep", "Сон", "💤"
        if "mHoldingDisplaySuspendBlocker=true" in output:
            return "on", "Включен", "🟢"

    # 2. Резервная проверка через dumpsys window
    ok, win_out = adb(cfg, "-s", address, "shell", "dumpsys", "window", "policy", timeout=3)
    if ok and win_out:
        if "screenState=SCREEN_STATE_ON" in win_out or "mScreenOnEarly=true" in win_out:
            return "on", "Включен", "🟢"
        if "screenState=SCREEN_STATE_OFF" in win_out or "mScreenOnEarly=false" in win_out:
            return "sleep", "Сон", "💤"

    return "unknown", "Состояние неизвестно", "⚪"


def get_tv_status(cfg, tv):
    with get_tv_lock(tv):
        return _get_tv_status_unlocked(cfg, tv)


def get_all_tv_statuses(cfg):
    tvs = cfg.get("tvs", [])
    if not tvs:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(tvs)), thread_name_prefix="status") as pool:
        future_map = {pool.submit(get_tv_status, cfg, tv): tv["id"] for tv in tvs}
        results = {}
        for future in as_completed(future_map):
            ip = future_map[future]
            try:
                results[ip] = future.result()
            except Exception:
                results[ip] = ("offline", "Оффлайн", "🔴")
        return results


def _operate_unlocked(cfg, tv, action, url_override=None):
    if action not in {"on", "off", "web", "both", "screen", "reboot"} and action not in KEY_ACTIONS:
        return "Неизвестная команда"
    if action in {"on", "both"}:
        set_manual_sleep(cfg, tv["id"], False)
    address, error = connect(cfg, tv, wake=action in {"on", "both"})
    if not address:
        return f"Не удалось связаться с ТВ: {error[:200]}"
    if action in KEY_ACTIONS:
        keycode, label = KEY_ACTIONS[action]
        ok, output = adb(cfg, "-s", address, "shell", "input", "keyevent", keycode)
        return f"{label} выполнено" if ok else f"Ошибка: {output[:200]}"
    if action in {"on", "both"}:
        ok, output = wake_tv_with_retry(cfg, tv, address)
        if not ok:
            return f"Не удалось разбудить ТВ: {output[:200]}"
    if action == "off":
        ok, output = adb(cfg, "-s", address, "shell", "input", "keyevent", "223")
        if ok:
            set_manual_sleep(cfg, tv["id"], True)
        return "Отправлена команда ожидания" if ok else f"Ошибка: {output[:200]}"
    if action == "screen":
        ok, output = apply_keep_awake_settings(cfg, address)
        if not ok:
            return f"Не удалось применить настройки: {output[:200]}"
        return "Заставка и автоматический сон отключены"
    if action == "reboot":
        ok, output = adb(cfg, "-s", address, "reboot")
        return "Команда перезагрузки отправлена" if ok else f"Ошибка: {output[:200]}"
    if action in {"web", "both"}:
        if action == "both":
            time.sleep(3)
        ok, output = adb(
            cfg, "-s", address, "shell", "am", "start", "-a",
            "android.intent.action.VIEW", "-d", url_override or tv["url"],
            timeout=20,
        )
        if not ok or "Error:" in output or "unable to resolve" in output.lower():
            return f"Не удалось открыть сайт: {output[:250]}"
        return "Команда открытия сайта отправлена — проверьте экран ТВ"
    return "Команда пробуждения отправлена — проверьте экран ТВ"


def operate(cfg, tv, action, url_override=None):
    with get_tv_lock(tv):
        return _operate_unlocked(cfg, tv, action, url_override=url_override)


def operate_many(cfg, tvs, action, url_override=None):
    if len(tvs) == 1:
        return [(tvs[0], operate(cfg, tvs[0], action, url_override=url_override))]
    with ThreadPoolExecutor(max_workers=min(8, len(tvs)), thread_name_prefix="operate") as pool:
        futures = {
            pool.submit(operate, cfg, tv, action, url_override=url_override): tv
            for tv in tvs
        }
        results = []
        for future in as_completed(futures):
            tv = futures[future]
            try:
                res = future.result()
            except Exception as exc:
                res = f"Ошибка: {exc}"
            results.append((tv, res))
        tv_order = {tv["id"]: i for i, tv in enumerate(tvs)}
        results.sort(key=lambda item: tv_order.get(item[0]["id"], 0))
        return results


def _capture_screenshot_unlocked(cfg, tv):
    address, error = connect(cfg, tv, wake=False)
    if not address:
        return False, None, f"Не удалось связаться с ТВ: {error[:200]}"

    # 1. Быстрый потоковый снимок через exec-out screencap -p
    ok, data, err = adb_bytes(cfg, "-s", address, "exec-out", "screencap", "-p", timeout=15)
    if ok and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True, data, ""

    # 2. Резервный способ: сохранение во временный файл на ТВ и adb pull
    for remote_tmp in ["/data/local/tmp/screencap_tv.png", "/sdcard/screencap_tv.png"]:
        ok_cap, _ = adb(cfg, "-s", address, "shell", "screencap", "-p", remote_tmp, timeout=12)
        if not ok_cap:
            continue
        local_tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
                local_tmp = tmp_file.name
            ok_pull, _ = adb(cfg, "-s", address, "pull", remote_tmp, local_tmp, timeout=12)
            if ok_pull and os.path.exists(local_tmp) and os.path.getsize(local_tmp) > 0:
                with open(local_tmp, "rb") as file:
                    file_data = file.read()
                if file_data.startswith(b"\x89PNG\r\n\x1a\n"):
                    return True, file_data, ""
        except Exception:
            pass
        finally:
            if local_tmp and os.path.exists(local_tmp):
                try:
                    os.remove(local_tmp)
                except OSError:
                    pass
            adb(cfg, "-s", address, "shell", "rm", "-f", remote_tmp, timeout=3)

    return False, None, err or "Не удалось получить скриншот через ADB (возможно, экран выключен)"


def capture_screenshot(cfg, tv):
    with get_tv_lock(tv):
        return _capture_screenshot_unlocked(cfg, tv)


def handle_screenshots(cfg, chat_id, tvs):
    send_chat_action(cfg, chat_id, "upload_photo")
    if len(tvs) == 1:
        shots = [(tvs[0], *capture_screenshot(cfg, tvs[0]))]
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(tvs)), thread_name_prefix="screencap") as pool:
            futures = {pool.submit(capture_screenshot, cfg, tv): tv for tv in tvs}
            shots = []
            for future in as_completed(futures):
                tv = futures[future]
                try:
                    ok, data, err = future.result()
                except Exception as exc:
                    ok, data, err = False, None, str(exc)
                shots.append((tv, ok, data, err))
            tv_order = {tv["id"]: i for i, tv in enumerate(tvs)}
            shots.sort(key=lambda item: tv_order.get(item[0]["id"], 0))

    for tv, ok, data, error in shots:
        if ok and data:
            now_str = time.strftime("%H:%M:%S")
            caption = f'📸 {tv["name"]} ({now_str})'
            safe_name = tv["name"].replace(" ", "_").replace("/", "_")
            filename = f"screenshot_{safe_name}_{int(time.time())}.png"
            try:
                send_photo(cfg, chat_id, data, caption=caption, filename=filename)
            except Exception as exc:
                send(cfg, chat_id, f'Не удалось отправить снимок экрана {tv["name"]}: {exc}')
        else:
            send(cfg, chat_id, f'Ошибка скриншота с {tv["name"]}: {error}')


def validate_url(value):
    value = value.strip()
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Нужна полная HTTPS-ссылка, например https://example.com/tv")
    if any(char.isspace() for char in value):
        raise ValueError("В ссылке не должно быть пробелов")
    return value


def validate_clock(value):
    value = str(value).strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("Время должно быть в формате ЧЧ:ММ, например 09:00")
    return value


def parse_schedule_days(value):
    normalized = re.sub(r"\s+", " ", value.strip().lower().replace("ё", "е"))
    presets = {
        "каждый день": list(range(7)),
        "ежедневно": list(range(7)),
        "все дни": list(range(7)),
        "пн-вс": list(range(7)),
        "будни": list(range(5)),
        "пн-пт": list(range(5)),
        "выходные": [5, 6],
        "сб-вс": [5, 6],
    }
    if normalized in presets:
        return presets[normalized]
    aliases = {name: index for index, name in enumerate(WEEKDAY_LABELS)}
    aliases.update({
        "понедельник": 0, "вторник": 1, "среда": 2, "четверг": 3,
        "пятница": 4, "суббота": 5, "воскресенье": 6,
    })
    parts = [part.strip() for part in re.split(r"[,; ]+", normalized) if part.strip()]
    try:
        days = sorted({aliases[part] for part in parts})
    except KeyError as exc:
        raise ValueError(
            "Дни: «каждый день», «будни», «выходные» или список пн,ср,пт"
        ) from exc
    if not days:
        raise ValueError("Укажите хотя бы один день недели")
    return days


def normalize_schedule(schedule):
    if not isinstance(schedule, dict):
        raise ValueError("schedule должен быть объектом")
    enabled = schedule.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("schedule.enabled должен быть true или false")
    normalized = {
        "enabled": enabled,
        "on": validate_clock(schedule.get("on", "09:00")),
        "off": validate_clock(schedule.get("off", "22:00")),
        "days": sorted({int(day) for day in schedule.get("days", range(7))}),
    }
    if not normalized["days"] or any(day < 0 or day > 6 for day in normalized["days"]):
        raise ValueError("Дни расписания должны быть числами от 0 до 6")
    if normalized["on"] == normalized["off"]:
        raise ValueError("Время включения и ожидания не должно совпадать")
    for key in ("last_on", "last_off"):
        if schedule.get(key):
            normalized[key] = str(schedule[key])
    return normalized


def parse_schedule_text(value):
    parts = value.strip().split(maxsplit=2)
    if len(parts) < 2:
        raise ValueError("Отправьте время включения и ожидания, например 09:00 22:00")
    on_time = validate_clock(parts[0])
    off_time = validate_clock(parts[1])
    if on_time == off_time:
        raise ValueError("Время включения и ожидания не должно совпадать")
    days = parse_schedule_days(parts[2] if len(parts) == 3 else "каждый день")
    return {"enabled": True, "on": on_time, "off": off_time, "days": days}


def format_schedule(schedule):
    if not schedule or not schedule.get("enabled", False):
        return "отключено"
    days = schedule.get("days", list(range(7)))
    if days == list(range(7)):
        day_label = "каждый день"
    elif days == list(range(5)):
        day_label = "будни"
    elif days == [5, 6]:
        day_label = "выходные"
    else:
        day_label = ",".join(WEEKDAY_LABELS[day] for day in days)
    return f"включение {schedule['on']}, ожидание {schedule['off']}, {day_label}"


def schedule_summary(cfg, target):
    televisions = cfg["tvs"] if target == "all" else [
        tv for tv in cfg["tvs"] if tv.get("id") == target
    ]
    if not televisions:
        raise ValueError("Телевизор не найден")
    lines = [f"Часовой пояс: {cfg.get('timezone', 'Asia/Almaty')}"]
    lines.extend(
        f"• {tv['name']}: {format_schedule(tv.get('schedule'))}"
        for tv in televisions
    )
    return "\n".join(lines)


def save_schedule(cfg, target, schedule):
    normalized = normalize_schedule(schedule)
    with CONFIG_LOCK:
        with CONFIG.open(encoding="utf-8") as file:
            stored = json.load(file)
        televisions = stored.get("tvs", [])
        matches = televisions if target == "all" else [
            tv for tv in televisions if tv.get("id") == target
        ]
        if not matches:
            raise ValueError("Телевизор не найден")
        for tv in matches:
            tv["schedule"] = dict(normalized)
        atomic_write_config(stored)
        cfg["tvs"] = stored["tvs"]


def disable_schedule(cfg, target):
    with CONFIG_LOCK:
        with CONFIG.open(encoding="utf-8") as file:
            stored = json.load(file)
        matches = stored.get("tvs", []) if target == "all" else [
            tv for tv in stored.get("tvs", []) if tv.get("id") == target
        ]
        if not matches:
            raise ValueError("Телевизор не найден")
        for tv in matches:
            current = tv.get("schedule") or {"on": "09:00", "off": "22:00", "days": list(range(7))}
            current["enabled"] = False
            tv["schedule"] = normalize_schedule(current)
        atomic_write_config(stored)
        cfg["tvs"] = stored["tvs"]


def check_site(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "TV-Monitor/1.0",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read(1)
            status = getattr(response, "status", 200)
        if 200 <= status < 400:
            return True, f"HTTP {status}"
        return False, f"HTTP {status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, str(getattr(exc, "reason", exc))[:180]


def save_url(cfg, target, url):
    url = validate_url(url)
    with CONFIG_LOCK:
        with CONFIG.open(encoding="utf-8") as file:
            stored = json.load(file)
        if target == "all":
            indices = range(len(stored["tvs"]))
        else:
            indices = [i for i, tv in enumerate(stored["tvs"]) if tv.get("id") == target]
            if not indices:
                raise ValueError("Телевизор не найден")
        for index in indices:
            stored["tvs"][index]["url"] = url
        atomic_write_config(stored)
        cfg["tvs"] = stored["tvs"]


def parse_ip_port(value):
    value = value.strip()
    if ":" in value:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("Неверный формат. Используйте IP:порт (например, 192.168.0.108:5555)")
        ip_str, port_str = parts[0].strip(), parts[1].strip()
        if not port_str.isdigit() or not (1 <= int(port_str) <= 65535):
            raise ValueError("Порт должен быть числом от 1 до 65535")
        port = int(port_str)
    else:
        ip_str = value
        port = 5555
    try:
        ipaddress.IPv4Address(ip_str)
    except ValueError:
        raise ValueError(f"Некорректный IP-адрес: {ip_str}")
    return ip_str, port


def parse_addtv_arguments(value):
    parts = value.split()
    if not parts:
        raise ValueError("Укажите IP-адрес телевизора")
    ip, port = parse_ip_port(parts[0])
    remaining = parts[1:]
    url = None
    if remaining and remaining[-1].lower().startswith(("https://", "http://")):
        url = validate_url(remaining.pop())
    mac = None
    if remaining:
        candidate = remaining[0]
        if re.fullmatch(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", candidate):
            mac = validate_mac(remaining.pop(0))
        elif candidate.count(":") >= 2 or candidate.count("-") >= 5:
            mac = validate_mac(candidate)
    name = " ".join(remaining).strip() or f"Телевизор {ip}"
    if len(name) > 60:
        raise ValueError("Название должно быть не длиннее 60 символов")
    return ip, port, mac, name, url


def add_tv(cfg, name, ip, port=5555, url=None, mac=None):
    name = name.strip()
    if not name or len(name) > 60:
        raise ValueError("Название должно быть от 1 до 60 символов")
    ip = str(ipaddress.IPv4Address(ip.strip()))
    port = int(port)
    if not (1 <= port <= 65535):
        raise ValueError("Неверный ADB-порт")
    with CONFIG_LOCK:
        with CONFIG.open(encoding="utf-8") as file:
            stored = json.load(file)
        if any(tv.get("ip") == ip and int(tv.get("port", 5555)) == port for tv in stored.get("tvs", [])):
            raise ValueError(f"Телевизор {ip}:{port} уже добавлен")
        normalized_mac = validate_mac(mac) if mac else None
        if normalized_mac and any(
            tv.get("mac", "").upper().replace("-", ":") == normalized_mac
            for tv in stored.get("tvs", [])
        ):
            raise ValueError(f"Телевизор с MAC {normalized_mac} уже добавлен")
        default_site = stored["tvs"][0]["url"] if stored.get("tvs") else "https://example.org/tv"
        new_tv = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "ip": ip,
            "port": port,
            "url": validate_url(url or default_site),
        }
        if normalized_mac:
            new_tv["mac"] = normalized_mac
        stored.setdefault("tvs", []).append(new_tv)
        atomic_write_config(stored)
        cfg["tvs"] = stored["tvs"]
        return new_tv


def delete_tv(cfg, tv_id):
    with CONFIG_LOCK:
        with CONFIG.open(encoding="utf-8") as file:
            stored = json.load(file)
        if len(stored.get("tvs", [])) <= 1:
            raise ValueError("Нельзя удалить последний телевизор")
        index = next(
            (i for i, tv in enumerate(stored.get("tvs", [])) if tv.get("id") == tv_id),
            None,
        )
        if index is None:
            raise ValueError("Телевизор не найден")
        deleted = stored["tvs"].pop(index)
        atomic_write_config(stored)
        cfg["tvs"] = stored["tvs"]
        return deleted


def set_manual_sleep(cfg, tv_id, enabled):
    """Persist an explicit standby request so the watchdog respects it."""
    with CONFIG_LOCK:
        with CONFIG.open(encoding="utf-8") as file:
            stored = json.load(file)
        stored_tv = next(
            (tv for tv in stored.get("tvs", []) if tv.get("id") == tv_id), None
        )
        if stored_tv is None:
            raise ValueError("Телевизор не найден")
        stored_tv["manual_sleep"] = bool(enabled)
        atomic_write_config(stored)
        for current_tv in cfg.get("tvs", []):
            if current_tv.get("id") == tv_id:
                current_tv["manual_sleep"] = bool(enabled)
                break


def refresh_url(url):
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "_tv_refresh"]
    query.append(("_tv_refresh", str(int(time.time()))))
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path,
         urllib.parse.urlencode(query), parsed.fragment)
    )


def notify_owners(cfg, message):
    for user_id in list(cfg["allowed_user_ids"]):
        try:
            send(cfg, user_id, message)
        except Exception as exc:
            logging.warning("Не удалось отправить уведомление %s: %s", user_id, exc)


def mark_schedule_run(cfg, tv_ids, action, date_key):
    field = f"last_{action}"
    with CONFIG_LOCK:
        with CONFIG.open(encoding="utf-8") as file:
            stored = json.load(file)
        for tv in stored.get("tvs", []):
            if tv.get("id") in tv_ids and isinstance(tv.get("schedule"), dict):
                tv["schedule"][field] = date_key
        atomic_write_config(stored)
        cfg["tvs"] = stored["tvs"]


def run_due_schedules(cfg, now=None):
    timezone = ZoneInfo(cfg.get("timezone", "Asia/Almaty"))
    if now is None:
        now = datetime.now(timezone)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone)
    else:
        now = now.astimezone(timezone)
    minute = now.strftime("%H:%M")
    date_key = now.strftime("%Y-%m-%d")
    weekday = now.weekday()
    with CONFIG_LOCK:
        televisions = [dict(tv) for tv in cfg.get("tvs", [])]

    completed = []
    for schedule_action, tv_action in (("on", "both"), ("off", "off")):
        due = []
        for tv in televisions:
            schedule = tv.get("schedule")
            if not isinstance(schedule, dict) or not schedule.get("enabled", False):
                continue
            if weekday not in schedule.get("days", []) or schedule.get(schedule_action) != minute:
                continue
            if schedule.get(f"last_{schedule_action}") == date_key:
                continue
            due.append(tv)
        if not due:
            continue
        results = operate_many(cfg, due, tv_action)
        mark_schedule_run(cfg, {tv["id"] for tv in due}, schedule_action, date_key)
        completed.extend((schedule_action, tv, result) for tv, result in results)
        label = "включение + сайт" if schedule_action == "on" else "ожидание"
        notify_owners(
            cfg,
            "🕒 Расписание: " + label + "\n" +
            "\n".join(f"{tv['name']}: {result}" for tv, result in results),
        )
    return completed


def _schedule_loop_forever(cfg):
    while True:
        run_due_schedules(cfg)
        time.sleep(15)


def schedule_loop(cfg):
    while True:
        try:
            _schedule_loop_forever(cfg)
        except Exception:
            logging.exception("Планировщик аварийно перезапускается")
            time.sleep(10)


def _refresh_loop_forever(cfg):
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="refresh")
    while True:
        with CONFIG_LOCK:
            televisions = [dict(tv) for tv in cfg["tvs"]]
            interval = cfg["refresh_interval_seconds"]
        if televisions:
            # Smart Refresh: обновляем только те ТВ, которые реально бодрствуют (статус 'on')
            # Это исключает пробуждение спящих телевизоров командой am start
            statuses = list(pool.map(lambda t: get_tv_status(cfg, t)[0], televisions))
            awake_tvs = [tv for tv, st in zip(televisions, statuses) if st == "on"]
            if awake_tvs:
                jobs = {
                    pool.submit(operate, cfg, tv, "web", refresh_url(tv["url"])): tv
                    for tv in awake_tvs
                }
                for job in as_completed(jobs):
                    tv = jobs[job]
                    try:
                        result = job.result()
                    except Exception as exc:
                        result = f"Ошибка: {exc}"
                    if result.startswith("Не удалось") or result.startswith("Ошибка"):
                        logging.warning('Автообновление %s: %s', tv["name"], result)
        time.sleep(interval)


def refresh_loop(cfg):
    while True:
        try:
            _refresh_loop_forever(cfg)
        except Exception:
            logging.exception("Фоновое автообновление аварийно перезапускается")
            time.sleep(10)


def availability_transition(state, available, failure_threshold, recovery_threshold):
    if available:
        state["successes"] += 1
        state["failures"] = 0
        if state["online"] is False and state["successes"] >= recovery_threshold:
            state["online"] = True
            return "up"
        if state["online"] is None:
            state["online"] = True
    else:
        state["failures"] += 1
        state["successes"] = 0
        if state["online"] is not False and state["failures"] >= failure_threshold:
            state["online"] = False
            return "down"
    return None


def _healthcheck_loop_forever(cfg):
    previous_sites = {}
    previous_tvs = {}
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="healthcheck")
    while True:
        with CONFIG_LOCK:
            sites = {}
            televisions = [dict(tv) for tv in cfg["tvs"]]
            for tv in televisions:
                sites.setdefault(tv["url"], []).append(tv["name"])
            interval = cfg["healthcheck_interval_seconds"]
            failure_threshold = cfg["healthcheck_failure_threshold"]
            recovery_threshold = cfg["healthcheck_recovery_threshold"]

        checks = {}
        if sites:
            site_urls = list(sites.keys())
            site_results = list(pool.map(check_site, site_urls))
            checks = dict(zip(site_urls, site_results))

        tv_checks = {}
        if televisions:
            active_tvs = [tv for tv in televisions if not tv.get("manual_sleep", False)]
            tv_results = list(pool.map(
                lambda t: is_device_reachable(t["ip"], t.get("port", 5555), timeout=1.0),
                active_tvs
            ))
            tv_checks = {tv["id"]: ok for tv, ok in zip(active_tvs, tv_results)}
            # Intentional standby is not an outage, even if the TV closes ADB port 5555.
            tv_checks.update({tv["id"]: True for tv in televisions if tv.get("manual_sleep", False)})

        for url, names in sites.items():
            available, detail = checks.get(url, (False, "Не проверено"))
            state = previous_sites.setdefault(
                url, {"online": None, "failures": 0, "successes": 0}
            )
            tv_names = ", ".join(names)
            transition = availability_transition(
                state, available, failure_threshold, recovery_threshold
            )
            if transition == "up":
                notify_owners(
                    cfg,
                    f"✅ Сайт снова доступен\n{url}\nТВ: {tv_names}\n{detail}",
                )
            elif transition == "down":
                notify_owners(
                    cfg,
                    f"🚨 Сайт недоступен\n{url}\nТВ: {tv_names}\nПричина: {detail}",
                )
        for old_url in set(previous_sites) - set(sites):
            previous_sites.pop(old_url, None)

        for tv in televisions:
            tv_id = tv["id"]
            online = tv_checks.get(tv_id, False)
            state = previous_tvs.setdefault(
                tv_id, {"online": None, "failures": 0, "successes": 0}
            )
            transition = availability_transition(
                state, online, failure_threshold, recovery_threshold
            )
            if transition == "up":
                notify_owners(
                    cfg,
                    f"🟢 Телевизор снова в сети\nТВ: {tv['name']} ({tv['ip']})",
                )
            elif transition == "down":
                notify_owners(
                    cfg,
                    f"⚠️ Телевизор отключился от сети\nТВ: {tv['name']} ({tv['ip']})",
                )
        for old_id in set(previous_tvs) - {t["id"] for t in televisions}:
            previous_tvs.pop(old_id, None)

        time.sleep(interval)


def healthcheck_loop(cfg):
    while True:
        try:
            _healthcheck_loop_forever(cfg)
        except Exception:
            logging.exception("Фоновая проверка аварийно перезапускается")
            time.sleep(10)


def process(cfg, update):
    message = update.get("message")
    callback = update.get("callback_query")
    if not message and not callback:
        return
    sender = (callback or message).get("from", {})
    chat = (callback.get("message", {}) if callback else message).get("chat", {})
    chat_id = chat.get("id")
    if callback:
        try:
            telegram(cfg, "answerCallbackQuery", {"callback_query_id": callback["id"]})
        except Exception:
            pass
    if not chat_id or chat.get("type") != "private":
        return
    user_id = sender.get("id")
    if message and message.get("text", "").split(" ")[0] == "/id":
        send(cfg, chat_id, f"Ваш Telegram ID: {user_id}")
        return
    if user_id not in cfg["allowed_user_ids"]:
        send(cfg, chat_id, "Нет доступа. Отправьте /id и добавьте свой ID в config.json.")
        return
    if message:
        text = message.get("text", "").strip()
        if text.startswith("/start") or text == "/menu":
            PENDING_URL.pop(user_id, None)
            PENDING_ADD_TV.pop(user_id, None)
            PENDING_SCHEDULE.pop(user_id, None)
            send(cfg, chat_id, "Выберите телевизор:", menu(cfg))
            return
        if text == "/cancel":
            PENDING_URL.pop(user_id, None)
            PENDING_ADD_TV.pop(user_id, None)
            PENDING_SCHEDULE.pop(user_id, None)
            send(cfg, chat_id, "Действие отменено.", menu(cfg))
            return
        cmd_parts = text.split()
        cmd = cmd_parts[0].lower().split("@")[0] if cmd_parts else ""
        if cmd == "/addtv":
            PENDING_URL.pop(user_id, None)
            PENDING_SCHEDULE.pop(user_id, None)
            arg = text[len(cmd_parts[0]):].strip()
            if arg:
                try:
                    ip, port, mac, name, url = parse_addtv_arguments(arg)
                except ValueError as exc:
                    send(cfg, chat_id, f"❌ Ошибка: {exc}")
                    return
                try:
                    new_tv = add_tv(cfg, name, ip, port=port, url=url, mac=mac)
                except Exception as exc:
                    send(cfg, chat_id, f"Не удалось добавить ТВ: {exc}")
                    return
                is_on = is_device_reachable(ip, port, timeout=0.8)
                note = "" if is_on else "\n(⚠️ ТВ сейчас не в сети — настройки сохранены)"
                send(cfg, chat_id, f"🎉 Телевизор «{new_tv['name']}» ({ip}:{port}) добавлен!{note}", menu(cfg))
                return
            else:
                PENDING_ADD_TV[user_id] = {"step": "ip"}
                send(
                    cfg, chat_id,
                    "➕ Добавление нового телевизора\n\n"
                    "Шаг 1 из 4: Отправьте IP-адрес телевизора (например: 192.168.0.120 или 192.168.0.120:5555).\n\n"
                    "Для отмены отправьте /cancel."
                )
                return
        if cmd == "/schedule":
            PENDING_URL.pop(user_id, None)
            PENDING_ADD_TV.pop(user_id, None)
            PENDING_SCHEDULE.pop(user_id, None)
            if len(cfg["tvs"]) == 1:
                target = cfg["tvs"][0]["id"]
                send(
                    cfg, chat_id,
                    "🕒 Расписание\n" + schedule_summary(cfg, target),
                    schedule_controls(target),
                )
            else:
                send(cfg, chat_id, "Выберите телевизор для расписания:", schedule_target_menu(cfg))
            return
        if cmd in {"/screenshot", "/shot", "/screencap"}:
            PENDING_URL.pop(user_id, None)
            PENDING_ADD_TV.pop(user_id, None)
            PENDING_SCHEDULE.pop(user_id, None)
            arg = text[len(cmd_parts[0]):].strip()
            if not arg:
                if len(cfg["tvs"]) == 1:
                    handle_screenshots(cfg, chat_id, cfg["tvs"])
                else:
                    send(cfg, chat_id, "Выберите ТВ для снятия скриншота:", screenshot_menu(cfg))
                return
            if arg.lower() in {"all", "все", "*"}:
                handle_screenshots(cfg, chat_id, cfg["tvs"])
                return
            if arg.isdigit():
                idx = int(arg)
                if 1 <= idx <= len(cfg["tvs"]):
                    handle_screenshots(cfg, chat_id, [cfg["tvs"][idx - 1]])
                elif idx == 0 and len(cfg["tvs"]) > 0:
                    handle_screenshots(cfg, chat_id, [cfg["tvs"][0]])
                else:
                    send(
                        cfg,
                        chat_id,
                        f"Телевизор №{arg} не найден. Всего ТВ: {len(cfg['tvs'])}.",
                        screenshot_menu(cfg),
                    )
                return
            matched = [
                tv for tv in cfg["tvs"]
                if arg.lower() in tv["name"].lower() or arg in tv["ip"]
            ]
            if len(matched) == 1:
                handle_screenshots(cfg, chat_id, matched)
                return
            if len(matched) > 1:
                names = ", ".join(tv["name"] for tv in matched)
                send(
                    cfg,
                    chat_id,
                    f"Найдено несколько ТВ: {names}.\nУточните номер или выберите из меню:",
                    screenshot_menu(cfg),
                )
                return
            send(
                cfg,
                chat_id,
                f"Телевизор «{arg}» не найден.",
                screenshot_menu(cfg),
            )
            return
        cmd_action = cmd.lstrip("/")
        if cmd_action in KEY_ACTIONS or cmd_action in {"ok", "vol+", "vol-"}:
            PENDING_URL.pop(user_id, None)
            PENDING_SCHEDULE.pop(user_id, None)
            act = "enter" if cmd_action == "ok" else ("volup" if cmd_action == "vol+" else ("voldown" if cmd_action == "vol-" else cmd_action))
            arg = text[len(cmd_parts[0]):].strip()
            if arg:
                if arg.lower() in {"all", "все", "*"}:
                    target_tvs = cfg["tvs"]
                elif arg.isdigit():
                    idx = int(arg)
                    if 1 <= idx <= len(cfg["tvs"]):
                        target_tvs = [cfg["tvs"][idx - 1]]
                    elif idx == 0 and len(cfg["tvs"]) > 0:
                        target_tvs = [cfg["tvs"][0]]
                    else:
                        send(cfg, chat_id, f"Телевизор №{arg} не найден. Всего ТВ: {len(cfg['tvs'])}.")
                        return
                else:
                    matched = [
                        tv for tv in cfg["tvs"]
                        if arg.lower() in tv["name"].lower() or arg in tv["ip"]
                    ]
                    if matched:
                        target_tvs = matched
                    else:
                        send(cfg, chat_id, f"Телевизор «{arg}» не найден.")
                        return
            else:
                target_tvs = cfg["tvs"]
            results = operate_many(cfg, target_tvs, act)
            formatted = [f'{tv["name"]}: {res}' for tv, res in results]
            send(cfg, chat_id, "\n".join(formatted))
            return
        schedule_target = PENDING_SCHEDULE.get(user_id)
        if schedule_target is not None:
            try:
                schedule = parse_schedule_text(text)
                save_schedule(cfg, schedule_target, schedule)
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                send(
                    cfg, chat_id,
                    f"❌ Расписание не сохранено: {exc}\n\n"
                    "Пример: 09:00 22:00 каждый день\n"
                    "Или: 08:30 18:00 будни\n"
                    "Для отмены отправьте /cancel.",
                )
                return
            PENDING_SCHEDULE.pop(user_id, None)
            send(
                cfg, chat_id,
                "✅ Расписание сохранено. В запланированное время включения "
                "телевизор автоматически откроет заданный сайт.\n\n" +
                schedule_summary(cfg, schedule_target),
                schedule_controls(schedule_target),
            )
            return
        target = PENDING_URL.get(user_id)
        if target is not None:
            try:
                url = validate_url(text)
            except ValueError as exc:
                send(cfg, chat_id, f"Ссылка не принята: {exc}\nОтправьте другую или /cancel.")
                return
            available, detail = check_site(url)
            if not available:
                send(
                    cfg, chat_id,
                    f"Сайт сейчас недоступен ({detail}). Адрес не сохранён. "
                    "Отправьте другую ссылку или /cancel.",
                )
                return
            try:
                save_url(cfg, target, url)
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                send(cfg, chat_id, f"Не удалось сохранить адрес: {exc}")
                return
            PENDING_URL.pop(user_id, None)
            televisions = (
                list(cfg["tvs"])
                if target == "all"
                else [tv for tv in cfg["tvs"] if tv.get("id") == target]
            )
            if not televisions:
                send(cfg, chat_id, "Кнопка устарела. Откройте /start заново.")
                return
            results = operate_many(cfg, televisions, "web")
            formatted = [f'{tv["name"]}: {res}' for tv, res in results]
            send(cfg, chat_id, "Новый сайт сохранён.\n" + "\n".join(formatted))
            return
        add_state = PENDING_ADD_TV.get(user_id)
        if add_state is not None:
            step = add_state.get("step")
            if step == "ip":
                try:
                    ip, port = parse_ip_port(text)
                except ValueError as exc:
                    send(cfg, chat_id, f"❌ Ошибка: {exc}\nПопробуйте снова (например, 192.168.0.120) или /cancel.")
                    return
                duplicate = any(
                    t["ip"] == ip and int(t.get("port", 5555)) == port
                    for t in cfg["tvs"]
                )
                if duplicate:
                    send(cfg, chat_id, f"❌ Телевизор {ip}:{port} уже добавлен. Отправьте другой адрес или /cancel.")
                    return
                reachable = is_device_reachable(ip, port, timeout=0.8)
                reach_note = " 🟢 (В сети)" if reachable else " 🔴 (Сейчас не в сети — настройки сохранятся)"
                add_state["ip"] = ip
                add_state["port"] = port
                add_state["step"] = "mac"
                send(
                    cfg, chat_id,
                    f"✅ IP принят: {ip}:{port}{reach_note}\n\n"
                    "Шаг 2 из 4: Отправьте MAC-адрес телевизора "
                    "(например: AA:BB:CC:DD:EE:FF):\n\n"
                    "Для отмены отправьте /cancel."
                )
                return
            elif step == "mac":
                try:
                    mac = validate_mac(text)
                except ValueError as exc:
                    send(cfg, chat_id, f"❌ Ошибка: {exc}\nОтправьте MAC ещё раз или /cancel.")
                    return
                if any(
                    tv.get("mac", "").upper().replace("-", ":") == mac
                    for tv in cfg["tvs"]
                ):
                    send(cfg, chat_id, f"❌ Телевизор с MAC {mac} уже добавлен. Отправьте другой MAC или /cancel.")
                    return
                add_state["mac"] = mac
                add_state["step"] = "name"
                send(
                    cfg, chat_id,
                    f"✅ MAC принят: {mac}\n\n"
                    "Шаг 3 из 4: Отправьте понятное название для этого ТВ "
                    "(например: TCL Конференц-зал или Кухня):\n\n"
                    "Для отмены отправьте /cancel."
                )
                return
            elif step == "name":
                name = text.strip()
                if not name or len(name) > 60:
                    send(cfg, chat_id, "Название должно быть от 1 до 60 символов. Отправьте название или /cancel.")
                    return
                add_state["name"] = name
                add_state["step"] = "url"
                def_url = cfg["tvs"][0]["url"] if cfg.get("tvs") else "https://example.org/tv"
                markup = {
                    "inline_keyboard": [
                        [{"text": f"✅ По умолчанию ({def_url[:28]}...)", "callback_data": "addtv:defurl"}],
                        [{"text": "❌ Отмена", "callback_data": "addtv:cancel"}],
                    ]
                }
                send(
                    cfg, chat_id,
                    f"✅ Название принято: «{name}»\n\n"
                    "Шаг 4 из 4: Отправьте HTTPS-ссылку сайта для ТВ (начинающуюся с https://), "
                    f"либо нажмите кнопку ниже для ссылки по умолчанию:\n{def_url}\n\n"
                    "Для отмены отправьте /cancel.",
                    markup=markup
                )
                return
            elif step == "url":
                try:
                    url = validate_url(text)
                except ValueError as exc:
                    send(cfg, chat_id, f"Ссылка не принята: {exc}\nОтправьте корректную ссылку или /cancel.")
                    return
                available, detail = check_site(url)
                if not available:
                    send(
                        cfg, chat_id,
                        f"Сайт сейчас недоступен ({detail}). Адрес не сохранён. "
                        "Отправьте другую ссылку или /cancel.",
                    )
                    return
                data = PENDING_ADD_TV.pop(user_id)
                try:
                    new_tv = add_tv(
                        cfg, data["name"], data["ip"], port=data["port"],
                        url=url, mac=data.get("mac")
                    )
                except Exception as exc:
                    send(cfg, chat_id, f"Не удалось добавить ТВ: {exc}")
                    return
                send(
                    cfg, chat_id,
                    f"🎉 Телевизор «{new_tv['name']}» ({new_tv['ip']}:{new_tv['port']}) успешно добавлен!",
                    menu(cfg)
                )
                return
        send(cfg, chat_id, "Выберите телевизор:", menu(cfg))
        return
    data = callback.get("data", "")
    if data == "addtv_start":
        PENDING_URL.pop(user_id, None)
        PENDING_SCHEDULE.pop(user_id, None)
        PENDING_ADD_TV[user_id] = {"step": "ip"}
        send(
            cfg, chat_id,
            "➕ Добавление нового телевизора\n\n"
            "Шаг 1 из 4: Отправьте IP-адрес телевизора (например: 192.168.0.120 или 192.168.0.120:5555).\n\n"
            "Для отмены отправьте /cancel."
        )
        return
    if data == "addtv:defurl":
        add_state = PENDING_ADD_TV.pop(user_id, None)
        if add_state and add_state.get("name") and add_state.get("ip"):
            try:
                new_tv = add_tv(
                    cfg, add_state["name"], add_state["ip"],
                    port=add_state.get("port", 5555), mac=add_state.get("mac")
                )
                send(
                    cfg, chat_id,
                    f"🎉 Телевизор «{new_tv['name']}» ({new_tv['ip']}:{new_tv['port']}) успешно добавлен!",
                    menu(cfg)
                )
            except Exception as exc:
                send(cfg, chat_id, f"Ошибка добавления ТВ: {exc}", menu(cfg))
        else:
            send(cfg, chat_id, "Сессия добавления устарела.", menu(cfg))
        return
    if data == "addtv:cancel":
        PENDING_ADD_TV.pop(user_id, None)
        send(cfg, chat_id, "Добавление телевизора отменено.", menu(cfg))
        return
    if data == "refresh_menu":
        PENDING_URL.pop(user_id, None)
        PENDING_ADD_TV.pop(user_id, None)
        PENDING_SCHEDULE.pop(user_id, None)
        msg_id = callback.get("message", {}).get("message_id")
        statuses = get_all_tv_statuses(cfg)
        now_str = time.strftime("%H:%M:%S")
        text = f"Телевизоры (обновлено в {now_str}):"
        if msg_id:
            edit_message(cfg, chat_id, msg_id, text, menu(cfg, statuses=statuses))
        else:
            send(cfg, chat_id, text, menu(cfg, statuses=statuses))
        return
    if data == "menu":
        PENDING_URL.pop(user_id, None)
        PENDING_ADD_TV.pop(user_id, None)
        PENDING_SCHEDULE.pop(user_id, None)
        msg_id = callback.get("message", {}).get("message_id")
        statuses = get_all_tv_statuses(cfg)
        if msg_id:
            edit_message(cfg, chat_id, msg_id, "Выберите телевизор:", menu(cfg, statuses=statuses))
        else:
            send(cfg, chat_id, "Выберите телевизор:", menu(cfg, statuses=statuses))
        return
    if data == "schedule_menu":
        PENDING_URL.pop(user_id, None)
        PENDING_ADD_TV.pop(user_id, None)
        PENDING_SCHEDULE.pop(user_id, None)
        send(cfg, chat_id, "Выберите телевизор для расписания:", schedule_target_menu(cfg))
        return
    try:
        action, target = data.split(":", 1)
        if target == "all":
            tvs = list(cfg["tvs"])
        else:
            tvs = [tv for tv in cfg["tvs"] if tv.get("id") == target]
        if not tvs:
            raise ValueError("Неверный ТВ")
    except ValueError:
        send(cfg, chat_id, "Кнопка устарела. Отправьте /start.")
        return
    if action == "select":
        if target == "all":
            send(cfg, chat_id, "📺 Управление всеми телевизорами:", actions(target))
        else:
            tv = tvs[0]
            code, label, icon = get_tv_status(cfg, tv)
            header = (
                f"{icon} {tv['name']}\n"
                f"Статус: {label}\n"
                f"Адрес: {tv['ip']}:{tv.get('port', 5555)}\n"
                f"Сайт: {tv['url']}\n\n"
                "Выберите действие:"
            )
            send(cfg, chat_id, header, actions(target))
        return
    if action == "schedule":
        send(
            cfg, chat_id,
            "🕒 Расписание\n" + schedule_summary(cfg, target),
            schedule_controls(target),
        )
        return
    if action == "schedset":
        PENDING_URL.pop(user_id, None)
        PENDING_ADD_TV.pop(user_id, None)
        PENDING_SCHEDULE[user_id] = target
        label = "всех телевизоров" if target == "all" else tvs[0]["name"]
        send(
            cfg, chat_id,
            f"Настройка расписания для {label}.\n\n"
            "Отправьте одним сообщением:\n"
            "ВРЕМЯ_ВКЛЮЧЕНИЯ ВРЕМЯ_ОЖИДАНИЯ ДНИ\n\n"
            "Примеры:\n"
            "09:00 22:00 каждый день\n"
            "08:30 18:00 будни\n"
            "10:00 20:00 пн,ср,пт\n\n"
            "Часовой пояс: " + cfg.get("timezone", "Asia/Almaty") + "\n"
            "Для отмены отправьте /cancel.",
        )
        return
    if action == "schedoff":
        try:
            disable_schedule(cfg, target)
            send(
                cfg, chat_id,
                "⏸ Расписание отключено. Ручное управление продолжает работать.\n\n" +
                schedule_summary(cfg, target),
                schedule_controls(target),
            )
        except Exception as exc:
            send(cfg, chat_id, f"Не удалось отключить расписание: {exc}")
        return
    if action == "rebootask":
        label = "все телевизоры" if target == "all" else tvs[0]["name"]
        send(cfg, chat_id, f"Перезагрузить: {label}?", reboot_confirmation(target))
        return
    if action == "deleteask":
        if len(tvs) == 1:
            tv = tvs[0]
            send(cfg, chat_id, f"Удалить телевизор «{tv['name']}» ({tv['ip']}) из списка?", delete_confirmation(target))
        else:
            send(cfg, chat_id, "Телевизор не найден.", menu(cfg))
        return
    if action == "delete":
        try:
            deleted = delete_tv(cfg, target)
            send(cfg, chat_id, f"🗑 Телевизор «{deleted['name']}» удален из списка.", menu(cfg))
        except Exception as exc:
            send(cfg, chat_id, f"Ошибка удаления: {exc}", menu(cfg))
        return
    if action == "seturl":
        PENDING_SCHEDULE.pop(user_id, None)
        PENDING_URL[user_id] = target
        label = "всех телевизоров" if target == "all" else tvs[0]["name"]
        send(
            cfg, chat_id,
            f"Отправьте новую HTTPS-ссылку для {label} одним сообщением.\n"
            "Для отмены отправьте /cancel.",
        )
        return
    if action == "screenshot":
        handle_screenshots(cfg, chat_id, tvs)
        return
    if action in KEY_ACTIONS:
        results = operate_many(cfg, tvs, action)
        errors = [res for _, res in results if res.startswith("Не удалось") or res.startswith("Ошибка")]
        if errors:
            send(cfg, chat_id, f"❌ {errors[0]}")
        return
    if action not in {"on", "off", "web", "both", "screen", "reboot"}:
        return
    results = operate_many(cfg, tvs, action)
    formatted = [f'{tv["name"]}: {res}' for tv, res in results]
    send(cfg, chat_id, "\n".join(formatted))


def safe_process(cfg, update):
    try:
        process(cfg, update)
    except Exception as exc:
        logging.exception("Ошибка обработки update_id=%s: %s", update.get("update_id"), exc)


class UpdateDispatcher:
    """Preserve update order per user without blocking Telegram long polling."""

    def __init__(self, max_workers=10):
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="bot_update"
        )
        self._lock = threading.Lock()
        self._queues = {}

    @staticmethod
    def _key(update):
        event = update.get("callback_query") or update.get("message") or {}
        sender_id = event.get("from", {}).get("id")
        return sender_id if sender_id is not None else f'update:{update.get("update_id")}'

    def submit(self, cfg, update):
        key = self._key(update)
        with self._lock:
            pending = self._queues.setdefault(key, deque())
            pending.append((cfg, update))
            if len(pending) == 1:
                self._pool.submit(self._drain, key)

    def _drain(self, key):
        while True:
            with self._lock:
                pending = self._queues.get(key)
                if not pending:
                    self._queues.pop(key, None)
                    return
                cfg, update = pending[0]
            safe_process(cfg, update)
            with self._lock:
                pending = self._queues.get(key)
                if pending:
                    pending.popleft()
                    if not pending:
                        self._queues.pop(key, None)
                        return

    def shutdown(self, wait=True):
        self._pool.shutdown(wait=wait)


def main():
    configure_logging()
    cfg = load_config()
    set_bot_commands(cfg)
    # Skip commands accumulated while the bot was offline.
    old = telegram(cfg, "getUpdates", {"offset": -1, "timeout": 0})
    offset = old[-1]["update_id"] + 1 if old else None
    if cfg["auto_refresh"]:
        threading.Thread(target=refresh_loop, args=(cfg,), daemon=True).start()
    if cfg["keep_awake"]:
        threading.Thread(target=keep_awake_loop, args=(cfg,), daemon=True).start()
    threading.Thread(target=schedule_loop, args=(cfg,), daemon=True).start()
    threading.Thread(target=healthcheck_loop, args=(cfg,), daemon=True).start()
    logging.info("Бот работает. Для остановки нажмите Ctrl+C.")
    dispatcher = UpdateDispatcher(max_workers=10)
    while True:
        try:
            payload = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                payload["offset"] = offset
            updates = telegram(cfg, "getUpdates", payload)
            for update in updates:
                dispatcher.submit(cfg, update)
                offset = update["update_id"] + 1
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            logging.warning("Ошибка соединения: %s", exc)
            time.sleep(3)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        sys.exit(f"Ошибка настройки: {exc}")
    except KeyboardInterrupt:
        logging.info("Бот остановлен")
