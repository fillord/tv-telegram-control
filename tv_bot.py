#!/usr/bin/env python3
"""Telegram controls for trusted Android TV devices on a local network."""

import ipaddress
import hashlib
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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tv_control import EventHistory, OperationResult, RuntimeState, StatusCache
from tv_control import failure as operation_failure
from tv_control import success as operation_success

ROOT = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("TV_BOT_CONFIG", ROOT / "config.json")).expanduser()
CONFIG_LOCK = threading.RLock()
TV_LOCKS_LOCK = threading.Lock()
TV_LOCKS = {}
ADB_RECOVERY_LOCK = threading.Lock()
ADB_LAST_RECOVERY = 0.0
ADB_RECOVERY_COOLDOWN_SECONDS = 60.0
LOG_STATE_LOCK = threading.Lock()
LOG_STATE = {}
HEARTBEAT = Path(os.environ.get("TV_BOT_HEARTBEAT", "/tmp/tv_bot_heartbeat"))
PENDING_URL = {}
PENDING_ADD_TV = {}
PENDING_SCHEDULE = {}
PENDING_EDIT_TV = {}
STATUS_CACHE = StatusCache()
SCHEDULE_RUNTIME_KEYS = (
    "last_on", "last_off",
    "last_on_event", "last_off_event",
    "last_on_attempt_event", "last_off_attempt_event",
    "last_on_attempt_at", "last_off_attempt_at",
    "last_on_attempts", "last_off_attempts",
    "last_on_result", "last_off_result",
    "last_on_success", "last_off_success",
)
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


def state_path():
    return Path(os.environ.get("TV_BOT_STATE", CONFIG.with_name("state.json"))).expanduser()


def history_path():
    return Path(
        os.environ.get("TV_BOT_HISTORY", CONFIG.with_name("history.jsonl"))
    ).expanduser()


def runtime_state():
    return RuntimeState(state_path())


def event_history(cfg=None):
    maximum = (cfg or {}).get("history_max_events", 1000)
    return EventHistory(history_path(), max_events=maximum)


def hydrate_runtime_state(televisions, state=None):
    state = state if state is not None else runtime_state().load()
    for tv in televisions:
        runtime_tv = state.get("tvs", {}).get(tv.get("id"), {})
        if "manual_sleep" in runtime_tv:
            tv["manual_sleep"] = bool(runtime_tv["manual_sleep"])
        schedule_state = runtime_tv.get("schedule", {})
        if isinstance(tv.get("schedule"), dict) and isinstance(schedule_state, dict):
            tv["schedule"].update(schedule_state)
    return televisions


def token_from_file():
    token_file = os.environ.get("TV_BOT_TOKEN_FILE")
    if not token_file:
        return ""
    try:
        return Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"Не удалось прочитать TV_BOT_TOKEN_FILE: {exc}") from exc


def record_event(cfg, *, event, message, success=True, tv=None, action=None, source="bot"):
    try:
        return event_history(cfg).append(
            event=event,
            message=message,
            success=success,
            tv=tv,
            action=action,
            source=source,
        )
    except OSError as exc:
        log_throttled_warning("event_history", f"Не удалось записать историю: {exc}")
        return None


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


def update_heartbeat():
    """Record a successful Telegram polling cycle for Docker health checks."""
    HEARTBEAT.touch()


def runtime_is_healthy(max_age_seconds=120):
    try:
        return time.time() - HEARTBEAT.stat().st_mtime <= max_age_seconds
    except OSError:
        return False


def log_throttled_warning(key, message, repeat_seconds=900):
    """Log a repeated fault once, when it changes, or after the repeat window."""
    now = time.monotonic()
    with LOG_STATE_LOCK:
        previous = LOG_STATE.get(key)
        should_log = (
            previous is None
            or previous[0] != message
            or now - previous[1] >= repeat_seconds
        )
        if should_log:
            LOG_STATE[key] = (message, now)
    if should_log:
        logging.warning("%s", message)


def log_recovery(key, message):
    with LOG_STATE_LOCK:
        previous = LOG_STATE.pop(key, None)
    if previous is not None:
        logging.info("%s", message)


def load_config():
    with CONFIG_LOCK:
        with CONFIG.open(encoding="utf-8") as file:
            stored = json.load(file)
        os.chmod(CONFIG, 0o600)
        cfg = json.loads(json.dumps(stored))
    external_token = token_from_file() or os.environ.get("TV_BOT_TOKEN", "").strip()
    token = external_token or str(cfg.get("telegram_token", "")).strip()
    if not token or token.startswith("PASTE_"):
        raise ValueError(
            "Укажите токен через TV_BOT_TOKEN_FILE, TV_BOT_TOKEN "
            "или legacy-поле telegram_token"
        )
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
    cfg["wake_timeout_seconds"] = min(
        120, max(5, int(cfg.get("wake_timeout_seconds", 45)))
    )
    cfg["wake_verify_timeout_seconds"] = min(
        30, max(0, int(cfg.get("wake_verify_timeout_seconds", 10)))
    )
    cfg["schedule_retry_attempts"] = min(
        10, max(1, int(cfg.get("schedule_retry_attempts", 3)))
    )
    cfg["schedule_retry_delay_seconds"] = min(
        3600, max(15, int(cfg.get("schedule_retry_delay_seconds", 60)))
    )
    cfg["log_repeat_interval_seconds"] = max(
        60, int(cfg.get("log_repeat_interval_seconds", 900))
    )
    cfg["status_cache_seconds"] = min(
        120, max(3, int(cfg.get("status_cache_seconds", 15)))
    )
    cfg["status_poll_interval_seconds"] = min(
        120, max(5, int(cfg.get("status_poll_interval_seconds", 10)))
    )
    cfg["history_max_events"] = min(
        10000, max(100, int(cfg.get("history_max_events", 1000)))
    )
    auto_refresh = cfg.get("auto_refresh", False)
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
    state = runtime_state().load()
    state_changed = False
    for tv_index, tv in enumerate(cfg["tvs"]):
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
            stored["tvs"][tv_index]["id"] = tv_id
            config_changed = True
        endpoint = (tv["ip"], tv["port"])
        if tv_id in seen_ids:
            raise ValueError(f'Повторяющийся ID телевизора: {tv_id}')
        if endpoint in seen_endpoints:
            raise ValueError(f'Повторяющийся адрес телевизора: {tv["ip"]}:{tv["port"]}')
        seen_ids.add(tv_id)
        seen_endpoints.add(endpoint)
        runtime_tv = state["tvs"].setdefault(tv_id, {})
        if "manual_sleep" in tv:
            runtime_tv["manual_sleep"] = bool(tv.pop("manual_sleep"))
            state_changed = True
            config_changed = True
        schedule = tv.get("schedule")
        if isinstance(schedule, dict):
            runtime_schedule = runtime_tv.setdefault("schedule", {})
            for key in SCHEDULE_RUNTIME_KEYS:
                if key in schedule:
                    runtime_schedule[key] = schedule.pop(key)
                    state_changed = True
                    config_changed = True
            schedule.update(runtime_schedule)
        if "manual_sleep" in runtime_tv:
            tv["manual_sleep"] = bool(runtime_tv["manual_sleep"])
        group = str(tv.get("group", "")).strip()
        if group:
            tv["group"] = group[:40]
        else:
            tv.pop("group", None)
    if state_changed:
        runtime_state().save(state)
    if config_changed:
        with CONFIG_LOCK:
            clean = json.loads(json.dumps(stored))
            # The deployment finalizer removes the legacy token only after the
            # new container is healthy, preserving rollback compatibility.
            if "telegram_token" in stored:
                clean["telegram_token"] = stored["telegram_token"]
            for clean_tv in clean.get("tvs", []):
                clean_tv.pop("manual_sleep", None)
                if isinstance(clean_tv.get("schedule"), dict):
                    for key in SCHEDULE_RUNTIME_KEYS:
                        clean_tv["schedule"].pop(key, None)
            atomic_write_config(clean)
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
    except RuntimeError as exc:
        if "message is not modified" in str(exc).lower():
            return
        send(cfg, chat_id, message, markup=markup)
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


def group_key(name):
    digest = hashlib.sha1(name.strip().casefold().encode("utf-8")).hexdigest()[:10]
    return f"g_{digest}"


def tv_groups(cfg):
    groups = {}
    for tv in cfg.get("tvs", []):
        name = str(tv.get("group", "")).strip()
        if name:
            groups.setdefault(name, []).append(tv)
    return groups


def resolve_target(cfg, target):
    if target == "all":
        return list(cfg.get("tvs", []))
    if target.startswith("g_"):
        for name, televisions in tv_groups(cfg).items():
            if group_key(name) == target:
                return list(televisions)
        return []
    return [tv for tv in cfg.get("tvs", []) if tv.get("id") == target]


def matching_tvs(tvs, target):
    if target == "all":
        return list(tvs)
    if target.startswith("g_"):
        return [tv for tv in tvs if tv.get("group") and group_key(tv["group"]) == target]
    return [tv for tv in tvs if tv.get("id") == target]


def menu(cfg, statuses=None):
    if statuses is None:
        statuses = get_all_tv_statuses(cfg)
    rows = []
    groups = tv_groups(cfg)
    grouped_ids = {tv["id"] for televisions in groups.values() for tv in televisions}
    for group_name, televisions in groups.items():
        icons = [statuses.get(tv["id"], ("unknown", "...", "⚪"))[2] for tv in televisions]
        summary_icon = "🟢" if icons and all(icon == "🟢" for icon in icons) else "📁"
        rows.append([{
            "text": f"{summary_icon} {group_name} · {len(televisions)} ТВ",
            "callback_data": f"select:{group_key(group_name)}",
        }])
        for tv in televisions:
            _, _, icon = statuses.get(tv["id"], ("offline", "...", "⚪"))
            rows.append([{
                "text": f"   {icon} {tv['name']}",
                "callback_data": f"select:{tv['id']}",
            }])
    for tv in cfg["tvs"]:
        if tv["id"] in grouped_ids:
            continue
        _, _, icon = statuses.get(tv["id"], ("offline", "...", "⚪"))
        rows.append([{"text": f"{icon} {tv['name']}", "callback_data": f"select:{tv['id']}"}])
    if len(cfg["tvs"]) > 1:
        rows.append([
            {"text": "🔄 Обновить", "callback_data": "refresh_menu"},
            {"text": "⚡ Управлять всеми", "callback_data": "select:all"},
        ])
    else:
        rows.append([{"text": "🔄 Обновить", "callback_data": "refresh_menu"}])
    rows.append([
        {"text": "🕒 Расписание", "callback_data": "schedule_menu"},
        {"text": "＋ Добавить ТВ", "callback_data": "addtv_start"},
    ])
    rows.append([
        {"text": "🧾 История", "callback_data": "history:all"},
        {"text": "⚙️ Общие настройки", "callback_data": "global_settings"},
    ])
    return {"inline_keyboard": rows}


def main_screen_text(statuses, notice=None):
    counts = {"on": 0, "sleep": 0, "offline": 0, "unauthorized": 0, "unknown": 0}
    for code, _, _ in statuses.values():
        counts[code] = counts.get(code, 0) + 1
    summary = [
        f"🟢 {counts['on']}",
        f"💤 {counts['sleep']}",
        f"🔴 {counts['offline']}",
    ]
    if counts["unauthorized"]:
        summary.append(f"🟡 {counts['unauthorized']}")
    if counts["unknown"]:
        summary.append(f"⚪ {counts['unknown']}")
    lines = ["📺 Телевизоры", " · ".join(summary), f"Обновлено: {time.strftime('%H:%M')}"]
    if notice:
        lines.extend(("", notice))
    return "\n".join(lines)


def tv_screen_text(tv, status, notice=None):
    _, label, icon = status
    host = urllib.parse.urlsplit(tv.get("url", "")).hostname or "не задан"
    lines = [
        f"📺 {tv['name']}",
        "",
        f"{icon} {label}",
        f"🌐 {host}",
        f"🕒 {format_schedule(tv.get('schedule'))}",
    ]
    if notice:
        lines.extend(("", notice))
    return "\n".join(lines)


def target_label(tvs, target=None):
    if target == "all":
        return "Все телевизоры"
    if target and target.startswith("g_"):
        return str(tvs[0].get("group", "Группа")) if tvs else "Группа"
    return tvs[0]["name"] if len(tvs) == 1 else "Выбранные телевизоры"


def target_screen_text(tvs, title, notice=None, target=None):
    name = target_label(tvs, target)
    lines = [f"{title} — {name}"]
    if notice:
        lines.extend(("", notice))
    return "\n".join(lines)


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


def actions(cfg, target, tv=None):
    rows = [
        [{"text": "▶ Включить", "callback_data": f"on:{target}"},
         {"text": "⏸ Сон", "callback_data": f"off:{target}"}],
        [{"text": "▶🌐 Включить и открыть", "callback_data": f"both:{target}"}],
        [{"text": "🌐 Открыть страницу", "callback_data": f"web:{target}"}],
        [{"text": "🎮 Пульт", "callback_data": f"remote:{target}"},
         {"text": "🔊 Звук", "callback_data": f"sound:{target}"}],
        [{"text": "📸 Скриншот", "callback_data": f"screenshot:{target}"},
         {"text": "🕒 Расписание", "callback_data": f"schedule:{target}"}],
        [{"text": "⚙️ Настройки", "callback_data": f"settings:{target}"}],
        [{"text": "‹ К телевизорам", "callback_data": "menu"}],
    ]
    return {"inline_keyboard": rows}


def remote_controls(target):
    return {"inline_keyboard": [
        [{"text": "▲", "callback_data": f"up:{target}"}],
        [{"text": "◀", "callback_data": f"left:{target}"},
         {"text": "OK", "callback_data": f"enter:{target}"},
         {"text": "▶", "callback_data": f"right:{target}"}],
        [{"text": "▼", "callback_data": f"down:{target}"}],
        [{"text": "↩ Назад", "callback_data": f"back:{target}"},
         {"text": "⌂ Домой", "callback_data": f"home:{target}"}],
        [{"text": "‹ К управлению", "callback_data": f"select:{target}"}],
    ]}


def sound_controls(target):
    return {"inline_keyboard": [
        [{"text": "− Тише", "callback_data": f"voldown:{target}"},
         {"text": "Mute", "callback_data": f"mute:{target}"},
         {"text": "+ Громче", "callback_data": f"volup:{target}"}],
        [{"text": "‹ К управлению", "callback_data": f"select:{target}"}],
    ]}


def settings_controls(cfg, target, tv=None):
    rows = [
        [{"text": "🖥 Не засыпать", "callback_data": f"screen:{target}"}],
        [{"text": "🔗 Изменить сайт", "callback_data": f"seturl:{target}"}],
        [{"text": "🔄 Перезагрузить", "callback_data": f"rebootask:{target}"}],
    ]
    if target != "all":
        if tv is not None:
            refresh_enabled = tv.get("auto_refresh", cfg.get("auto_refresh", False))
            rows.insert(0, [{
                "text": f"🔁 Автообновление: {'ВКЛ' if refresh_enabled else 'ВЫКЛ'}",
                "callback_data": f"tvrefresh:{target}",
            }])
            rows.append([
                {"text": "✏️ Данные ТВ", "callback_data": f"edit:{target}"},
                {"text": "🩺 Диагностика", "callback_data": f"diag:{target}"},
            ])
            rows.append([
                {"text": "ℹ️ Информация", "callback_data": f"info:{target}"},
                {"text": "🧾 История", "callback_data": f"history:{target}"},
            ])
            rows.append([{"text": "🗑 Удалить телевизор", "callback_data": f"deleteask:{target}"}])
    rows.append([{"text": "‹ К управлению", "callback_data": f"select:{target}"}])
    return {"inline_keyboard": rows}


def global_settings(cfg):
    enabled = "ВКЛ" if cfg.get("auto_refresh", False) else "ВЫКЛ"
    return {"inline_keyboard": [
        [{"text": f"🔁 Автообновление страниц: {enabled}", "callback_data": "toggle_auto_refresh"}],
        [{"text": "‹ К телевизорам", "callback_data": "menu"}],
    ]}


def settings_screen_text(cfg, target, tv=None, notice=None):
    name = "Все телевизоры" if target == "all" else (tv["name"] if tv else "Группа")
    lines = [f"⚙️ Настройки — {name}"]
    if target != "all" and tv is not None:
        enabled = tv.get("auto_refresh", cfg.get("auto_refresh", False))
        lines.extend(("", f"🔁 Автообновление: {'включено' if enabled else 'выключено'}"))
    if notice:
        lines.extend(("", notice))
    return "\n".join(lines)


def info_screen_text(cfg, tv, status):
    _, label, icon = status
    enabled = tv.get("auto_refresh", cfg.get("auto_refresh", False))
    return (
        f"ℹ️ {tv['name']}\n\n"
        f"{icon} {label}\n"
        f"ADB: {tv['ip']}:{tv.get('port', 5555)}\n"
        f"MAC: {'настроен' if tv.get('mac') else 'не указан'}\n"
        f"Сайт: {tv['url']}\n"
        f"Автообновление: {'включено' if enabled else 'выключено'}\n"
        f"Расписание: {format_schedule(tv.get('schedule'))}"
    )


def edit_tv_controls(cfg, tv):
    tv_id = tv["id"]
    index = next(
        (position for position, item in enumerate(cfg.get("tvs", []))
         if item.get("id") == tv_id),
        0,
    )
    rows = [
        [{"text": "Название", "callback_data": f"editname:{tv_id}"},
         {"text": "IP и порт", "callback_data": f"editaddr:{tv_id}"}],
        [{"text": "MAC", "callback_data": f"editmac:{tv_id}"},
         {"text": "Группа", "callback_data": f"editgroup:{tv_id}"}],
    ]
    move_row = []
    if index > 0:
        move_row.append({"text": "↑ Выше", "callback_data": f"moveup:{tv_id}"})
    if index < len(cfg.get("tvs", [])) - 1:
        move_row.append({"text": "↓ Ниже", "callback_data": f"movedown:{tv_id}"})
    if move_row:
        rows.append(move_row)
    rows.append([{"text": "‹ К настройкам", "callback_data": f"settings:{tv_id}"}])
    return {"inline_keyboard": rows}


def edit_tv_screen_text(tv, notice=None):
    lines = [
        f"✏️ Данные — {tv['name']}",
        "",
        f"Адрес: {tv['ip']}:{tv.get('port', 5555)}",
        f"MAC: {tv.get('mac', 'не указан')}",
        f"Группа: {tv.get('group', 'без группы')}",
        "",
        "Порядок телевизоров меняется кнопками ↑ и ↓.",
    ]
    if notice:
        lines.extend(("", notice))
    return "\n".join(lines)


def diagnostics_controls(tv_id):
    return {"inline_keyboard": [
        [{"text": "🔄 Проверить снова", "callback_data": f"diag:{tv_id}"}],
        [{"text": "🔌 Переподключить ADB", "callback_data": f"reconnect:{tv_id}"},
         {"text": "🌐 Проверить сайт", "callback_data": f"testsite:{tv_id}"}],
        [{"text": "‹ К настройкам", "callback_data": f"settings:{tv_id}"}],
    ]}


def format_history(cfg, tv_id=None, limit=15):
    entries = event_history(cfg).recent(limit=limit, tv_id=tv_id)
    if not entries:
        return "Событий пока нет."
    timezone = ZoneInfo(cfg.get("timezone", "Asia/Almaty"))
    lines = []
    for entry in entries:
        try:
            timestamp = datetime.fromisoformat(entry["at"])
            at = timestamp.astimezone(timezone).strftime("%d.%m %H:%M")
        except (KeyError, ValueError):
            at = "—"
        icon = "✅" if entry.get("success", False) else "❌"
        name = entry.get("tv_name")
        prefix = f"{name}: " if name else ""
        lines.append(f"{icon} {at} · {prefix}{entry.get('message', 'Событие')}")
    return "\n".join(lines)


def reboot_confirmation(target):
    return {"inline_keyboard": [
        [{"text": "✅ Да, перезагрузить", "callback_data": f"reboot:{target}"}],
        [{"text": "❌ Отмена", "callback_data": f"settings:{target}"}],
    ]}


def delete_confirmation(target):
    return {"inline_keyboard": [
        [{"text": "⚠️ Да, удалить телевизор", "callback_data": f"delete:{target}"}],
        [{"text": "❌ Отмена", "callback_data": f"settings:{target}"}],
    ]}


def schedule_target_menu(cfg):
    rows = [
        [{"text": f"📺 {tv['name']}", "callback_data": f"schedule:{tv['id']}"}]
        for tv in cfg["tvs"]
    ]
    for name in tv_groups(cfg):
        rows.append([{
            "text": f"📁 Группа: {name}",
            "callback_data": f"schedule:{group_key(name)}",
        }])
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


def adb_requires_server_restart(output):
    """Return true only for failures that can originate in the local ADB daemon."""
    text = output.lower()
    return any(
        marker in text
        for marker in (
            "cannot connect to daemon",
            "daemon not running",
            "server version",
            "protocol fault",
            "no route to host",
            "transport error",
        )
    )


def adb_state_error(output):
    text = output.lower()
    if "unauthorized" in text:
        return "ADB не авторизован на ТВ (подтвердите запрос на экране)"
    if "offline" in text:
        return "ТВ в режиме ADB offline (перезапустите сетевую отладку на ТВ)"
    if "timeout" in text or "timed out" in text:
        return "Истекло время ожидания ответа ADB"
    return output or "ADB не авторизован на ТВ"


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


def wait_for_tv_port(tv, timeout):
    """Wait for Android ADB after Wake-on-LAN instead of requiring a second click."""
    port = int(tv.get("port", 5555))
    deadline = time.monotonic() + timeout
    while True:
        if is_device_reachable(tv["ip"], port, timeout=1.0):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(2, remaining))


def connect(cfg, tv, wake=False):
    port = int(tv.get("port", 5555))
    address = f'{tv["ip"]}:{port}'
    if wake and tv.get("mac"):
        try:
            wake_on_lan(tv["mac"], tv.get("broadcast", "255.255.255.255"), tv_ip=tv["ip"])
        except (ValueError, OSError) as exc:
            logging.warning("Wake-on-LAN для %s: %s", tv["name"], exc)

    # Быстрая сокет-проверка, исключающая долгое 10-секундное зависание при выключенном ТВ
    if wake and tv.get("mac"):
        timeout = cfg.get("wake_timeout_seconds", 45)
        reachable = wait_for_tv_port(tv, timeout)
    else:
        reachable = is_device_reachable(tv["ip"], port, timeout=1.0)
    if not reachable:
        adb(cfg, "disconnect", address, timeout=2)
        if wake and tv.get("mac"):
            return None, f"ТВ не открыл порт {port} за {timeout} сек. после Wake-on-LAN"
        return None, f"ТВ недоступен по сети (порт {port} закрыт или ТВ выключен)"

    ok, output = adb(cfg, "connect", address, timeout=8)
    if not ok or adb_connect_failed(output):
        adb(cfg, "disconnect", address, timeout=2)
        # First retry only this device. Restarting the shared ADB daemon affects
        # every TV and is reserved for a repeated local-daemon failure.
        if is_device_reachable(tv["ip"], port, timeout=1.0):
            ok, output = adb(cfg, "connect", address, timeout=8)
        if (
            (not ok or adb_connect_failed(output))
            and adb_requires_server_restart(output)
            and is_device_reachable(tv["ip"], port, timeout=1.0)
        ):
            recover_adb_server(cfg)
            ok, output = adb(cfg, "connect", address, timeout=8)
        if not ok or adb_connect_failed(output):
            adb(cfg, "disconnect", address, timeout=2)
            return None, output or "Нет соединения по ADB"

    ok, output = adb(cfg, "-s", address, "get-state", timeout=5)
    if not ok or output.strip() != "device":
        adb(cfg, "disconnect", address, timeout=2)
        if "offline" in output.lower() and is_device_reachable(tv["ip"], port, timeout=1.0):
            retry_ok, retry_output = adb(cfg, "connect", address, timeout=6)
            if retry_ok and not adb_connect_failed(retry_output):
                ok, output = adb(cfg, "-s", address, "get-state", timeout=5)
                if ok and output.strip() == "device":
                    return address, ""
            adb(cfg, "disconnect", address, timeout=2)
        return None, adb_state_error(output)

    return address, ""


def apply_keep_awake_settings(cfg, address):
    for command in KEEP_AWAKE_COMMANDS:
        ok, output = adb(cfg, "-s", address, "shell", *command, timeout=6)
        if not ok:
            return False, output or f"Не выполнена команда: {' '.join(command)}"
    return True, ""


def detect_power_state(power_output, window_output=""):
    for line in power_output.splitlines():
        line_str = line.strip()
        if "mWakefulness=" in line_str:
            value = line_str.split("mWakefulness=", 1)[1].split()[0].lower()
            if value == "awake":
                return "on"
            if value in {"asleep", "dozing", "dreaming"}:
                return "sleep"
        if "Display Power: state=" in line_str:
            value = line_str.split("Display Power: state=", 1)[1].split()[0].upper()
            if value == "ON":
                return "on"
            if value == "OFF":
                return "sleep"
    if "mHoldingDisplaySuspendBlocker=true" in power_output:
        return "on"
    if "screenState=SCREEN_STATE_ON" in window_output or "mScreenOnEarly=true" in window_output:
        return "on"
    if "screenState=SCREEN_STATE_OFF" in window_output or "mScreenOnEarly=false" in window_output:
        return "sleep"
    return "unknown"


def read_power_state(cfg, address):
    ok, power = adb(cfg, "-s", address, "shell", "dumpsys", "power", timeout=4)
    if not ok:
        return "unknown"
    state = detect_power_state(power)
    if state != "unknown":
        return state
    ok, window = adb(
        cfg, "-s", address, "shell", "dumpsys", "window", "policy", timeout=3
    )
    return detect_power_state(power, window if ok else "")


def confirm_tv_awake(cfg, address):
    timeout = cfg.get("wake_verify_timeout_seconds", 10)
    deadline = time.monotonic() + timeout
    while True:
        state = read_power_state(cfg, address)
        if state == "on":
            return True, ""
        if state == "unknown":
            # Some Android TV builds do not expose a reliable power state.
            # The accepted key event is still considered a successful command.
            return True, "Состояние экрана не удалось подтвердить"
        if time.monotonic() >= deadline:
            return False, "Экран остался в режиме сна после команды пробуждения"
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def wake_tv_with_retry(cfg, tv, address):
    """Wake a TV and recover once from a stuck ADB shell command."""
    ok, output = adb(
        cfg, "-s", address, "shell", "input", "keyevent", "224", timeout=6
    )
    if ok:
        verified, detail = confirm_tv_awake(cfg, address)
        return verified, detail or output

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
        return False, retry_output
    verified, detail = confirm_tv_awake(cfg, retry_address)
    return verified, detail or retry_output


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
                log_key = ("keep_awake", tv.get("id", tv["ip"]))
                if not ok:
                    log_throttled_warning(
                        log_key,
                        f'Контроль питания {tv["name"]}: {error}',
                        cfg.get("log_repeat_interval_seconds", 900),
                    )
                else:
                    log_recovery(log_key, f'Контроль питания {tv["name"]} восстановлен')
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
        if tv.get("manual_sleep", False):
            return "sleep", "Сон", "💤"
        return "offline", "Оффлайн", "🔴"

    address, error = connect(cfg, tv, wake=False)
    if not address:
        err_lower = error.lower()
        if "не авторизован" in err_lower or "unauthorized" in err_lower:
            return "unauthorized", "Не авторизован", "🟡"
        if "offline" in err_lower:
            return "offline", "ADB оффлайн", "🔴"
        return "offline", "Оффлайн", "🔴"

    state = read_power_state(cfg, address)
    if state == "on":
        return "on", "Включен", "🟢"
    if state == "sleep":
        return "sleep", "Сон", "💤"

    return "unknown", "Состояние неизвестно", "⚪"


def get_tv_status(cfg, tv):
    with get_tv_lock(tv):
        return _get_tv_status_unlocked(cfg, tv)


def refresh_status_cache(cfg, tvs=None):
    """Refresh selected TVs in parallel and return the resulting snapshot."""
    tvs = list(tvs if tvs is not None else cfg.get("tvs", []))
    if not tvs:
        return {}
    results = {}
    with ThreadPoolExecutor(max_workers=min(8, len(tvs)), thread_name_prefix="status") as pool:
        future_map = {pool.submit(get_tv_status, cfg, tv): tv for tv in tvs}
        for future in as_completed(future_map):
            tv = future_map[future]
            error = ""
            try:
                status = future.result()
            except Exception as exc:
                status = ("offline", "Оффлайн", "🔴")
                error = str(exc)
            STATUS_CACHE.put(tv["id"], status, error=error)
            results[tv["id"]] = status
    return results


def get_cached_tv_status(cfg, tv, force=False):
    maximum_age = cfg.get("status_cache_seconds", 15)
    cached = STATUS_CACHE.get(tv["id"], max_age=maximum_age)
    if not force and cached is not None:
        return cached["status"]
    return refresh_status_cache(cfg, [tv])[tv["id"]]


def get_all_tv_statuses(cfg, force=False):
    tvs = cfg.get("tvs", [])
    if not tvs:
        return {}
    ids = [tv["id"] for tv in tvs]
    cached = STATUS_CACHE.statuses(ids, include_stale=True)
    if force or len(cached) != len(tvs):
        return refresh_status_cache(cfg, tvs)
    return cached


def _status_loop_forever(cfg):
    while True:
        with CONFIG_LOCK:
            televisions = [dict(tv) for tv in cfg.get("tvs", [])]
            interval = cfg.get("status_poll_interval_seconds", 10)
        refresh_status_cache(cfg, televisions)
        STATUS_CACHE.prune(tv["id"] for tv in televisions)
        time.sleep(interval)


def status_loop(cfg):
    while True:
        try:
            _status_loop_forever(cfg)
        except Exception:
            logging.exception("Фоновый кэш статусов аварийно перезапускается")
            time.sleep(10)


def run_diagnostics(cfg, tv, check_website=True):
    port = int(tv.get("port", 5555))
    reachable = is_device_reachable(tv["ip"], port, timeout=1.0)
    result = {
        "reachable": reachable,
        "adb": False,
        "power": "unknown",
        "site": None,
        "site_detail": "не проверен",
        "error": "",
    }
    if reachable:
        address, error = connect(cfg, tv, wake=False)
        if address:
            result["adb"] = True
            result["power"] = read_power_state(cfg, address)
        else:
            result["error"] = error
    else:
        result["error"] = f"Порт {port} недоступен"
    if check_website:
        result["site"], result["site_detail"] = check_site(tv["url"])
    if not reachable:
        status = (
            ("sleep", "Сон", "💤") if tv.get("manual_sleep")
            else ("offline", "Оффлайн", "🔴")
        )
    elif not result["adb"]:
        status = (
            ("unauthorized", "Не авторизован", "🟡")
            if "авториз" in result["error"].casefold() or "unauthorized" in result["error"].casefold()
            else ("offline", "ADB оффлайн", "🔴")
        )
    elif result["power"] == "on":
        status = ("on", "Включен", "🟢")
    elif result["power"] == "sleep":
        status = ("sleep", "Сон", "💤")
    else:
        status = ("unknown", "Состояние неизвестно", "⚪")
    STATUS_CACHE.put(tv["id"], status, error=result["error"])
    return result


def diagnostics_screen_text(cfg, tv, result, notice=None):
    power_labels = {"on": "экран включён", "sleep": "сон", "unknown": "неизвестно"}
    cached = STATUS_CACHE.get(tv["id"])
    cache_age = f"{int(cached['age'])} сек." if cached else "нет данных"
    lines = [
        f"🩺 Диагностика — {tv['name']}",
        "",
        f"{'✅' if result['reachable'] else '❌'} ADB-порт {tv['ip']}:{tv.get('port', 5555)}",
        f"{'✅' if result['adb'] else '❌'} Авторизация ADB",
        f"🖥 Питание: {power_labels.get(result['power'], result['power'])}",
        f"{'✅' if result['site'] else '❌'} Сайт: {result['site_detail']}",
        f"🗃 Кэш статуса: {cache_age}",
    ]
    latest = event_history(cfg).recent(limit=1, tv_id=tv["id"])
    if latest:
        lines.append(f"🧾 Последнее: {latest[0].get('message', '—')}")
    if result.get("error"):
        lines.extend(("", f"Ошибка: {result['error']}"))
    if notice:
        lines.extend(("", notice))
    return "\n".join(lines)


def reconnect_tv(cfg, tv):
    port = int(tv.get("port", 5555))
    address = f"{tv['ip']}:{port}"
    if not is_device_reachable(tv["ip"], port, timeout=1.0):
        adb(cfg, "disconnect", address, timeout=2)
        result = operation_failure(
            f"Порт {port} недоступен — переподключение невозможно", "port_closed"
        )
    else:
        adb(cfg, "disconnect", address, timeout=2)
        connected, error = connect(cfg, tv, wake=False)
        result = (
            operation_success("ADB успешно переподключён", "adb_reconnected")
            if connected
            else operation_failure(f"Не удалось переподключить ADB: {error}", "adb_reconnect_failed")
        )
    STATUS_CACHE.invalidate(tv["id"])
    record_event(
        cfg, event="diagnostic", message=str(result),
        success=operation_succeeded(result), tv=tv,
        action="reconnect", source="diagnostic",
    )
    return result


def _operate_unlocked(cfg, tv, action, url_override=None):
    if action not in {"on", "off", "web", "both", "screen", "reboot"} and action not in KEY_ACTIONS:
        return operation_failure("Неизвестная команда", "unknown_action")
    if action in {"on", "both"}:
        set_manual_sleep(cfg, tv["id"], False)
    address, error = connect(cfg, tv, wake=action in {"on", "both"})
    if not address:
        return operation_failure(
            f"Не удалось связаться с ТВ: {error[:200]}", "connect_failed", error
        )
    if action in KEY_ACTIONS:
        keycode, label = KEY_ACTIONS[action]
        ok, output = adb(cfg, "-s", address, "shell", "input", "keyevent", keycode)
        if ok:
            return operation_success(f"{label} выполнено", "key_sent")
        return operation_failure(f"Ошибка: {output[:200]}", "key_failed", output)
    if action in {"on", "both"}:
        ok, output = wake_tv_with_retry(cfg, tv, address)
        if not ok:
            return operation_failure(
                f"Не удалось разбудить ТВ: {output[:200]}", "wake_failed", output
            )
    if action == "off":
        ok, output = adb(cfg, "-s", address, "shell", "input", "keyevent", "223")
        if ok:
            set_manual_sleep(cfg, tv["id"], True)
        if ok:
            return operation_success("Отправлена команда ожидания", "standby_sent")
        return operation_failure(f"Ошибка: {output[:200]}", "standby_failed", output)
    if action == "screen":
        ok, output = apply_keep_awake_settings(cfg, address)
        if not ok:
            return operation_failure(
                f"Не удалось применить настройки: {output[:200]}",
                "keep_awake_failed", output,
            )
        return operation_success("Заставка и автоматический сон отключены", "keep_awake")
    if action == "reboot":
        ok, output = adb(cfg, "-s", address, "reboot")
        if ok:
            return operation_success("Команда перезагрузки отправлена", "reboot_sent")
        return operation_failure(f"Ошибка: {output[:200]}", "reboot_failed", output)
    if action in {"web", "both"}:
        if action == "both":
            time.sleep(3)
        ok, output = adb(
            cfg, "-s", address, "shell", "am", "start", "-a",
            "android.intent.action.VIEW", "-d", url_override or tv["url"],
            timeout=20,
        )
        if not ok or "Error:" in output or "unable to resolve" in output.lower():
            return operation_failure(
                f"Не удалось открыть сайт: {output[:250]}", "web_failed", output
            )
        return operation_success(
            "Команда открытия сайта отправлена — проверьте экран ТВ", "web_opened"
        )
    return operation_success(
        "Команда пробуждения отправлена — проверьте экран ТВ", "wake_sent"
    )


def operate(cfg, tv, action, url_override=None):
    with get_tv_lock(tv):
        result = _operate_unlocked(cfg, tv, action, url_override=url_override)
    STATUS_CACHE.invalidate(tv.get("id"))
    record_event(
        cfg,
        event="command",
        message=str(result),
        success=operation_succeeded(result),
        tv=tv,
        action=action,
        source="command",
    )
    return result


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
                res = operation_failure(f"Ошибка: {exc}", "exception", str(exc))
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
                record_event(
                    cfg, event="screenshot", message="Скриншот отправлен",
                    success=True, tv=tv, action="screenshot", source="command",
                )
            except Exception as exc:
                record_event(
                    cfg, event="screenshot", message=f"Ошибка скриншота: {exc}",
                    success=False, tv=tv, action="screenshot", source="command",
                )
                send(cfg, chat_id, f'Не удалось отправить снимок экрана {tv["name"]}: {exc}')
        else:
            record_event(
                cfg, event="screenshot", message=f"Ошибка скриншота: {error}",
                success=False, tv=tv, action="screenshot", source="command",
            )
            send(cfg, chat_id, f'Ошибка скриншота с {tv["name"]}: {error}')


def validate_url(value):
    value = value.strip()
    parsed = urllib.parse.urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Нужна полная веб-ссылка без логина и пароля")
    if any(char.isspace() for char in value):
        raise ValueError("В ссылке не должно быть пробелов")
    if parsed.scheme == "https":
        return value
    allowed_http_urls = {
        item.strip()
        for item in os.environ.get("TV_BOT_HTTP_ALLOWED_URLS", "").split(",")
        if item.strip()
    }
    if parsed.scheme == "http" and value in allowed_http_urls:
        return value
    if parsed.scheme == "http":
        raise ValueError("HTTP-ссылка не включена в TV_BOT_HTTP_ALLOWED_URLS")
    raise ValueError("Ссылка должна начинаться с https://")


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
    for key in (
        "last_on", "last_off",
        "last_on_event", "last_off_event",
        "last_on_attempt_event", "last_off_attempt_event",
        "last_on_attempt_at", "last_off_attempt_at",
        "last_on_result", "last_off_result",
    ):
        if schedule.get(key):
            normalized[key] = str(schedule[key])
    for key in ("last_on_attempts", "last_off_attempts"):
        if schedule.get(key) is not None:
            normalized[key] = max(0, int(schedule[key]))
    for key in ("last_on_success", "last_off_success"):
        if schedule.get(key) is not None:
            normalized[key] = bool(schedule[key])
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
    televisions = matching_tvs(cfg["tvs"], target)
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
        matches = matching_tvs(televisions, target)
        if not matches:
            raise ValueError("Телевизор не найден")
        for tv in matches:
            tv["schedule"] = dict(normalized)
        atomic_write_config(stored)
        state = runtime_state().load()
        for tv in matches:
            state["tvs"].setdefault(tv["id"], {})["schedule"] = {}
        runtime_state().save(state)
        cfg["tvs"] = hydrate_runtime_state(stored["tvs"], state=state)


def disable_schedule(cfg, target):
    with CONFIG_LOCK:
        with CONFIG.open(encoding="utf-8") as file:
            stored = json.load(file)
        matches = matching_tvs(stored.get("tvs", []), target)
        if not matches:
            raise ValueError("Телевизор не найден")
        for tv in matches:
            current = tv.get("schedule") or {"on": "09:00", "off": "22:00", "days": list(range(7))}
            current["enabled"] = False
            tv["schedule"] = normalize_schedule(current)
        atomic_write_config(stored)
        cfg["tvs"] = hydrate_runtime_state(stored["tvs"])


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
        indices = [
            index for index, tv in enumerate(stored["tvs"])
            if tv in matching_tvs(stored["tvs"], target)
        ]
        if not indices:
            raise ValueError("Телевизор не найден")
        for index in indices:
            stored["tvs"][index]["url"] = url
        atomic_write_config(stored)
        cfg["tvs"] = hydrate_runtime_state(stored["tvs"])


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
        cfg["tvs"] = hydrate_runtime_state(stored["tvs"])
        STATUS_CACHE.invalidate(new_tv["id"])
        record_event(
            cfg, event="tv_added", message="Телевизор добавлен",
            success=True, tv=new_tv, source="settings",
        )
        return new_tv


def update_tv(cfg, tv_id, *, name=None, ip=None, port=None, mac=None, group=None):
    """Validate and update editable TV fields without changing its stable ID."""
    with CONFIG_LOCK:
        with CONFIG.open(encoding="utf-8") as file:
            stored = json.load(file)
        tv = next((item for item in stored.get("tvs", []) if item.get("id") == tv_id), None)
        if tv is None:
            raise ValueError("Телевизор не найден")
        if name is not None:
            cleaned_name = str(name).strip()
            if not cleaned_name or len(cleaned_name) > 60:
                raise ValueError("Название должно быть от 1 до 60 символов")
            tv["name"] = cleaned_name
        if ip is not None or port is not None:
            new_ip = str(ipaddress.IPv4Address(str(ip if ip is not None else tv["ip"])))
            new_port = int(port if port is not None else tv.get("port", 5555))
            if not (1 <= new_port <= 65535):
                raise ValueError("Неверный ADB-порт")
            if any(
                item.get("id") != tv_id
                and item.get("ip") == new_ip
                and int(item.get("port", 5555)) == new_port
                for item in stored.get("tvs", [])
            ):
                raise ValueError(f"Телевизор {new_ip}:{new_port} уже добавлен")
            tv["ip"], tv["port"] = new_ip, new_port
        if mac is not None:
            if str(mac).strip():
                normalized_mac = validate_mac(str(mac))
                if any(
                    item.get("id") != tv_id
                    and item.get("mac", "").upper().replace("-", ":") == normalized_mac
                    for item in stored.get("tvs", [])
                ):
                    raise ValueError(f"Телевизор с MAC {normalized_mac} уже добавлен")
                tv["mac"] = normalized_mac
            else:
                tv.pop("mac", None)
        if group is not None:
            cleaned_group = str(group).strip()
            if len(cleaned_group) > 40:
                raise ValueError("Название группы должно быть не длиннее 40 символов")
            if cleaned_group:
                tv["group"] = cleaned_group
            else:
                tv.pop("group", None)
        atomic_write_config(stored)
        cfg["tvs"] = hydrate_runtime_state(stored["tvs"])
        current = next(item for item in cfg["tvs"] if item.get("id") == tv_id)
    STATUS_CACHE.invalidate(tv_id)
    record_event(
        cfg, event="tv_updated", message="Данные телевизора изменены",
        success=True, tv=current, source="settings",
    )
    return current


def move_tv(cfg, tv_id, direction):
    with CONFIG_LOCK:
        with CONFIG.open(encoding="utf-8") as file:
            stored = json.load(file)
        index = next(
            (position for position, tv in enumerate(stored.get("tvs", []))
             if tv.get("id") == tv_id),
            None,
        )
        if index is None:
            raise ValueError("Телевизор не найден")
        new_index = index + (-1 if direction == "up" else 1)
        if 0 <= new_index < len(stored["tvs"]):
            stored["tvs"][index], stored["tvs"][new_index] = (
                stored["tvs"][new_index], stored["tvs"][index]
            )
            atomic_write_config(stored)
            cfg["tvs"] = hydrate_runtime_state(stored["tvs"])
        return next(tv for tv in cfg["tvs"] if tv.get("id") == tv_id)


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
        cfg["tvs"] = hydrate_runtime_state(stored["tvs"])
        runtime_state().remove_tv(tv_id)
        STATUS_CACHE.invalidate(tv_id)
        record_event(
            cfg, event="tv_deleted", message="Телевизор удалён",
            success=True, tv=deleted, source="settings",
        )
        return deleted


def set_manual_sleep(cfg, tv_id, enabled):
    """Persist an explicit standby request so the watchdog respects it."""
    with CONFIG_LOCK:
        current_tv = next(
            (tv for tv in cfg.get("tvs", []) if tv.get("id") == tv_id), None
        )
        if current_tv is None:
            raise ValueError("Телевизор не найден")
        runtime_state().update_tv(tv_id, {"manual_sleep": bool(enabled)})
        current_tv["manual_sleep"] = bool(enabled)


def set_auto_refresh(cfg, enabled):
    """Persist and apply periodic browser refresh without restarting the bot."""
    value = bool(enabled)
    with CONFIG_LOCK:
        with CONFIG.open(encoding="utf-8") as file:
            stored = json.load(file)
        stored["auto_refresh"] = value
        atomic_write_config(stored)
        cfg["auto_refresh"] = value


def set_tv_auto_refresh(cfg, tv_id, enabled):
    """Persist a per-TV override for periodic browser refresh."""
    value = bool(enabled)
    with CONFIG_LOCK:
        with CONFIG.open(encoding="utf-8") as file:
            stored = json.load(file)
        stored_tv = next(
            (tv for tv in stored.get("tvs", []) if tv.get("id") == tv_id), None
        )
        if stored_tv is None:
            raise ValueError("Телевизор не найден")
        stored_tv["auto_refresh"] = value
        atomic_write_config(stored)
        for current_tv in cfg.get("tvs", []):
            if current_tv.get("id") == tv_id:
                current_tv["auto_refresh"] = value
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


def schedule_event_key(action, event_at):
    return f'{event_at.strftime("%Y-%m-%dT%H:%M")}:{action}'


def most_recent_schedule_event(schedule, now):
    """Return the latest desired on/off state, including missed events."""
    events = []
    days = set(schedule.get("days", []))
    for offset in range(8):
        day = now - timedelta(days=offset)
        if day.weekday() not in days:
            continue
        for action in ("on", "off"):
            hour, minute = map(int, schedule[action].split(":"))
            event_at = now.replace(
                year=day.year,
                month=day.month,
                day=day.day,
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0,
            )
            if event_at <= now:
                events.append((event_at, action))
    if not events:
        return None
    event_at, action = max(events, key=lambda item: item[0])
    return action, event_at


def schedule_retry_ready(cfg, schedule, action, event_at, now):
    event_key = schedule_event_key(action, event_at)
    success_event = schedule.get(f"last_{action}_event")
    legacy_success = schedule.get(f"last_{action}") == event_at.strftime("%Y-%m-%d")
    if success_event == event_key or (not success_event and legacy_success):
        return False

    attempt_event = schedule.get(f"last_{action}_attempt_event")
    attempts = int(schedule.get(f"last_{action}_attempts", 0)) if attempt_event == event_key else 0
    if attempts >= cfg.get("schedule_retry_attempts", 3):
        return False

    if attempt_event == event_key and schedule.get(f"last_{action}_attempt_at"):
        try:
            attempted_at = datetime.fromisoformat(schedule[f"last_{action}_attempt_at"])
            if attempted_at.tzinfo is None:
                attempted_at = attempted_at.replace(tzinfo=now.tzinfo)
            elapsed = (now - attempted_at.astimezone(now.tzinfo)).total_seconds()
            if elapsed < cfg.get("schedule_retry_delay_seconds", 60):
                return False
        except (TypeError, ValueError):
            pass
    return True


def operation_succeeded(result):
    if isinstance(result, OperationResult):
        return result.success
    return not str(result).startswith(("Не удалось", "Ошибка"))


def record_schedule_attempts(cfg, action, events, results, attempted_at):
    result_by_id = {tv["id"]: result for tv, result in results}
    with CONFIG_LOCK:
        state = runtime_state().load()
        for tv in cfg.get("tvs", []):
            tv_id = tv.get("id")
            if tv_id not in events or not isinstance(tv.get("schedule"), dict):
                continue
            event_at = events[tv_id]
            event_key = schedule_event_key(action, event_at)
            schedule = tv["schedule"]
            runtime_schedule = state["tvs"].setdefault(tv_id, {}).setdefault(
                "schedule", {}
            )
            previous_event = schedule.get(f"last_{action}_attempt_event")
            attempts = int(schedule.get(f"last_{action}_attempts", 0)) if previous_event == event_key else 0
            result = result_by_id.get(tv_id, "Ошибка: результат операции отсутствует")
            success = operation_succeeded(result)
            updates = {
                f"last_{action}_attempt_event": event_key,
                f"last_{action}_attempt_at": attempted_at.isoformat(timespec="seconds"),
                f"last_{action}_attempts": attempts + 1,
                f"last_{action}_result": str(result)[:500],
                f"last_{action}_success": success,
            }
            if success:
                updates[f"last_{action}"] = event_at.strftime("%Y-%m-%d")
                updates[f"last_{action}_event"] = event_key
            schedule.update(updates)
            runtime_schedule.update(updates)
            record_event(
                cfg,
                event="schedule",
                message=str(result),
                success=success,
                tv=tv,
                action=action,
                source="schedule",
            )
        runtime_state().save(state)


def run_due_schedules(cfg, now=None):
    timezone = ZoneInfo(cfg.get("timezone", "Asia/Almaty"))
    if now is None:
        now = datetime.now(timezone)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone)
    else:
        now = now.astimezone(timezone)
    with CONFIG_LOCK:
        televisions = [dict(tv) for tv in cfg.get("tvs", [])]

    completed = []
    due_by_action = {"on": [], "off": []}
    events_by_action = {"on": {}, "off": {}}
    already_satisfied = []
    satisfied_events = {}
    for tv in televisions:
        schedule = tv.get("schedule")
        if not isinstance(schedule, dict) or not schedule.get("enabled", False):
            continue
        event = most_recent_schedule_event(schedule, now)
        if event is None:
            continue
        schedule_action, event_at = event
        if schedule_retry_ready(cfg, schedule, schedule_action, event_at, now):
            if schedule_action == "off" and tv.get("manual_sleep", False):
                already_satisfied.append((tv, "Режим ожидания уже установлен"))
                satisfied_events[tv["id"]] = event_at
                continue
            due_by_action[schedule_action].append(tv)
            events_by_action[schedule_action][tv["id"]] = event_at

    if already_satisfied:
        record_schedule_attempts(cfg, "off", satisfied_events, already_satisfied, now)
        completed.extend(("off", tv, result) for tv, result in already_satisfied)

    for schedule_action, tv_action in (("on", "both"), ("off", "off")):
        due = due_by_action[schedule_action]
        if not due:
            continue
        results = operate_many(cfg, due, tv_action)
        record_schedule_attempts(
            cfg, schedule_action, events_by_action[schedule_action], results, now
        )
        completed.extend((schedule_action, tv, result) for tv, result in results)
        label = "включение + сайт" if schedule_action == "on" else "ожидание"
        attempts_limit = cfg.get("schedule_retry_attempts", 3)
        current_by_id = {tv["id"]: tv for tv in cfg.get("tvs", [])}
        lines = []
        for tv, result in results:
            if operation_succeeded(result):
                status = "✅"
            else:
                schedule = current_by_id.get(tv["id"], {}).get("schedule", {})
                attempts = int(schedule.get(f"last_{schedule_action}_attempts", 1))
                status = (
                    f"❌ попытки исчерпаны ({attempts}/{attempts_limit})"
                    if attempts >= attempts_limit
                    else f"❌ будет повтор ({attempts}/{attempts_limit})"
                )
            lines.append(f"{tv['name']}: {result} ({status})")
        notify_owners(
            cfg,
            "🕒 Расписание: " + label + "\n" +
            "\n".join(lines),
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
            default_enabled = cfg.get("auto_refresh", False)
        refresh_tvs = [tv for tv in televisions if tv.get("auto_refresh", default_enabled)]
        if refresh_tvs:
            # Smart Refresh: обновляем только те ТВ, которые реально бодрствуют (статус 'on')
            # Это исключает пробуждение спящих телевизоров командой am start
            statuses = list(pool.map(lambda t: get_cached_tv_status(cfg, t)[0], refresh_tvs))
            awake_tvs = [tv for tv, st in zip(refresh_tvs, statuses) if st == "on"]
            if awake_tvs:
                jobs = {
                    pool.submit(operate, cfg, tv, "web", refresh_url(tv["url"])): tv
                    for tv in awake_tvs
                }
                for job in as_completed(jobs):
                    tv = jobs[job]
                    log_key = ("refresh", tv.get("id", tv["ip"]))
                    try:
                        result = job.result()
                    except Exception as exc:
                        result = f"Ошибка: {exc}"
                    if not operation_succeeded(result):
                        log_throttled_warning(
                            log_key,
                            f'Автообновление {tv["name"]}: {result}',
                            cfg.get("log_repeat_interval_seconds", 900),
                        )
                    else:
                        log_recovery(log_key, f'Автообновление {tv["name"]} восстановлено')
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
                record_event(
                    cfg, event="site_recovered",
                    message=f"Сайт снова доступен: {detail}", success=True,
                    source="monitor",
                )
                notify_owners(
                    cfg,
                    f"✅ Сайт снова доступен\n{url}\nТВ: {tv_names}\n{detail}",
                )
            elif transition == "down":
                record_event(
                    cfg, event="site_down",
                    message=f"Сайт недоступен: {detail}", success=False,
                    source="monitor",
                )
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
                record_event(
                    cfg, event="tv_recovered", message="Телевизор снова в сети",
                    success=True, tv=tv, source="monitor",
                )
                notify_owners(
                    cfg,
                    f"🟢 Телевизор снова в сети\nТВ: {tv['name']} ({tv['ip']})",
                )
            elif transition == "down":
                record_event(
                    cfg, event="tv_down", message="Телевизор отключился от сети",
                    success=False, tv=tv, source="monitor",
                )
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


def show_screen(cfg, chat_id, message_id, text, markup):
    if message_id:
        edit_message(cfg, chat_id, message_id, text, markup)
    else:
        send(cfg, chat_id, text, markup)


def show_main_screen(cfg, chat_id, message_id=None, notice=None, force=False):
    statuses = get_all_tv_statuses(cfg, force=force)
    show_screen(
        cfg,
        chat_id,
        message_id,
        main_screen_text(statuses, notice=notice),
        menu(cfg, statuses=statuses),
    )


def show_control_screen(cfg, chat_id, message_id, target, tvs, notice=None):
    if target == "all" or target.startswith("g_") or len(tvs) > 1:
        text = target_screen_text(tvs, "⚡ Управление", notice=notice, target=target)
        show_screen(cfg, chat_id, message_id, text, actions(cfg, target))
        return
    tv = tvs[0]
    status = get_cached_tv_status(cfg, tv)
    show_screen(
        cfg,
        chat_id,
        message_id,
        tv_screen_text(tv, status, notice=notice),
        actions(cfg, target, tv),
    )


def clear_pending(user_id):
    PENDING_URL.pop(user_id, None)
    PENDING_ADD_TV.pop(user_id, None)
    PENDING_SCHEDULE.pop(user_id, None)
    PENDING_EDIT_TV.pop(user_id, None)


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
            payload = {"callback_query_id": callback["id"]}
            callback_action = callback.get("data", "").split(":", 1)[0]
            if callback_action in KEY_ACTIONS:
                payload["text"] = KEY_ACTIONS[callback_action][1]
            telegram(cfg, "answerCallbackQuery", payload)
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
            clear_pending(user_id)
            show_main_screen(cfg, chat_id)
            return
        if text == "/cancel":
            clear_pending(user_id)
            show_main_screen(cfg, chat_id, notice="Действие отменено")
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
                show_main_screen(
                    cfg,
                    chat_id,
                    notice=f"✅ Телевизор «{new_tv['name']}» добавлен{note}",
                )
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
        edit_state = PENDING_EDIT_TV.get(user_id)
        if edit_state is not None:
            tv_id = edit_state["tv_id"]
            field = edit_state["field"]
            try:
                if field == "name":
                    updated = update_tv(cfg, tv_id, name=text)
                elif field == "address":
                    ip, port = parse_ip_port(text)
                    updated = update_tv(cfg, tv_id, ip=ip, port=port)
                elif field == "mac":
                    value = "" if text.casefold() in {"удалить", "нет", "-"} else text
                    updated = update_tv(cfg, tv_id, mac=value)
                elif field == "group":
                    value = "" if text.casefold() in {"без группы", "удалить", "нет", "-"} else text
                    updated = update_tv(cfg, tv_id, group=value)
                else:
                    raise ValueError("Неизвестное поле")
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                send(
                    cfg, chat_id,
                    f"❌ Данные не сохранены: {exc}\nПопробуйте ещё раз или /cancel.",
                )
                return
            PENDING_EDIT_TV.pop(user_id, None)
            send(
                cfg, chat_id, edit_tv_screen_text(updated, "✅ Данные сохранены"),
                edit_tv_controls(cfg, updated),
            )
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
            televisions = resolve_target(cfg, target)
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
                    "Шаг 4 из 4: Отправьте HTTPS-ссылку или разрешённый внутренний HTTP-адрес, "
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
                show_main_screen(
                    cfg, chat_id, notice=f"✅ Телевизор «{new_tv['name']}» добавлен"
                )
                return
        show_main_screen(cfg, chat_id)
        return
    data = callback.get("data", "")
    msg_id = callback.get("message", {}).get("message_id")
    if data == "addtv_start":
        PENDING_URL.pop(user_id, None)
        PENDING_SCHEDULE.pop(user_id, None)
        PENDING_ADD_TV[user_id] = {"step": "ip"}
        show_screen(
            cfg, chat_id, msg_id,
            "➕ Добавление нового телевизора\n\n"
            "Шаг 1 из 4: Отправьте IP-адрес телевизора (например: 192.168.0.120 или 192.168.0.120:5555).\n\n"
            "Для отмены отправьте /cancel.",
            {"inline_keyboard": [[{"text": "❌ Отмена", "callback_data": "addtv:cancel"}]]},
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
                show_main_screen(
                    cfg,
                    chat_id,
                    msg_id,
                    notice=f"✅ Телевизор «{new_tv['name']}» добавлен",
                )
            except Exception as exc:
                show_main_screen(cfg, chat_id, msg_id, notice=f"❌ Ошибка добавления: {exc}")
        else:
            show_main_screen(cfg, chat_id, msg_id, notice="Сессия добавления устарела")
        return
    if data == "addtv:cancel":
        PENDING_ADD_TV.pop(user_id, None)
        show_main_screen(cfg, chat_id, msg_id, notice="Добавление телевизора отменено")
        return
    if data == "refresh_menu":
        clear_pending(user_id)
        show_main_screen(cfg, chat_id, msg_id, force=True)
        return
    if data == "global_settings":
        show_screen(
            cfg,
            chat_id,
            msg_id,
            "⚙️ Общие настройки\n\n"
            f"🔁 Автообновление страниц: {'включено' if cfg.get('auto_refresh', False) else 'выключено'}",
            global_settings(cfg),
        )
        return
    if data == "toggle_auto_refresh":
        enabled = not cfg.get("auto_refresh", False)
        set_auto_refresh(cfg, enabled)
        text = (
            "⚙️ Общие настройки\n\n🔁 Автообновление страниц включено"
            if enabled else
            "⚙️ Общие настройки\n\n⏸ Автообновление страниц выключено"
        )
        show_screen(cfg, chat_id, msg_id, text, global_settings(cfg))
        return
    if data == "menu":
        clear_pending(user_id)
        show_main_screen(cfg, chat_id, msg_id)
        return
    if data == "schedule_menu":
        PENDING_URL.pop(user_id, None)
        PENDING_ADD_TV.pop(user_id, None)
        PENDING_SCHEDULE.pop(user_id, None)
        show_screen(
            cfg, chat_id, msg_id,
            "🕒 Расписание\n\nВыберите телевизор:",
            schedule_target_menu(cfg),
        )
        return
    try:
        action, target = data.split(":", 1)
        tvs = resolve_target(cfg, target)
        if not tvs:
            raise ValueError("Неверный ТВ")
    except ValueError:
        send(cfg, chat_id, "Кнопка устарела. Отправьте /start.")
        return
    if action == "select":
        PENDING_URL.pop(user_id, None)
        PENDING_SCHEDULE.pop(user_id, None)
        show_control_screen(cfg, chat_id, msg_id, target, tvs)
        return
    if action == "remote":
        show_screen(
            cfg, chat_id, msg_id,
            target_screen_text(tvs, "🎮 Пульт", target=target),
            remote_controls(target),
        )
        return
    if action == "sound":
        show_screen(
            cfg, chat_id, msg_id,
            target_screen_text(tvs, "🔊 Звук", target=target),
            sound_controls(target),
        )
        return
    if action == "settings":
        PENDING_URL.pop(user_id, None)
        PENDING_EDIT_TV.pop(user_id, None)
        tv = tvs[0] if len(tvs) == 1 and not target.startswith("g_") else None
        show_screen(
            cfg, chat_id, msg_id,
            settings_screen_text(cfg, target, tv).replace(
                "— Группа", f"— {target_label(tvs, target)}"
            ),
            settings_controls(cfg, target, tv),
        )
        return
    if action == "info" and len(tvs) == 1 and not target.startswith("g_"):
        tv = tvs[0]
        show_screen(
            cfg, chat_id, msg_id,
            info_screen_text(cfg, tv, get_cached_tv_status(cfg, tv)),
            {"inline_keyboard": [[
                {"text": "‹ К настройкам", "callback_data": f"settings:{target}"}
            ]]},
        )
        return
    if action == "history":
        title = "🧾 История — все телевизоры" if target == "all" else f"🧾 История — {target_label(tvs, target)}"
        back = "menu" if target == "all" else f"settings:{target}"
        show_screen(
            cfg, chat_id, msg_id,
            title + "\n\n" + format_history(cfg, None if target == "all" else tvs[0]["id"]),
            {"inline_keyboard": [[{"text": "🔄 Обновить", "callback_data": f"history:{target}"}],
                                  [{"text": "‹ Назад", "callback_data": back}]]},
        )
        return
    if action == "edit" and len(tvs) == 1 and not target.startswith("g_"):
        PENDING_EDIT_TV.pop(user_id, None)
        tv = tvs[0]
        show_screen(cfg, chat_id, msg_id, edit_tv_screen_text(tv), edit_tv_controls(cfg, tv))
        return
    if action in {"editname", "editaddr", "editmac", "editgroup"} and len(tvs) == 1:
        clear_pending(user_id)
        tv = tvs[0]
        field = {
            "editname": "name", "editaddr": "address",
            "editmac": "mac", "editgroup": "group",
        }[action]
        PENDING_EDIT_TV[user_id] = {"tv_id": tv["id"], "field": field}
        prompts = {
            "name": "Отправьте новое название (до 60 символов).",
            "address": "Отправьте новый адрес как IP или IP:порт.",
            "mac": "Отправьте MAC AA:BB:CC:DD:EE:FF. Для удаления отправьте «удалить».",
            "group": "Отправьте название группы. Для удаления отправьте «без группы».",
        }
        show_screen(
            cfg, chat_id, msg_id,
            f"✏️ {tv['name']}\n\n{prompts[field]}\n\nДля отмены отправьте /cancel.",
            {"inline_keyboard": [[{"text": "❌ Отмена", "callback_data": f"edit:{tv['id']}"}]]},
        )
        return
    if action in {"moveup", "movedown"} and len(tvs) == 1:
        tv = move_tv(cfg, target, "up" if action == "moveup" else "down")
        show_screen(
            cfg, chat_id, msg_id,
            edit_tv_screen_text(tv, "✅ Порядок изменён"),
            edit_tv_controls(cfg, tv),
        )
        return
    if action == "diag" and len(tvs) == 1 and not target.startswith("g_"):
        tv = tvs[0]
        diagnostic = run_diagnostics(cfg, tv)
        success = diagnostic["reachable"] and diagnostic["adb"] and bool(diagnostic["site"])
        record_event(
            cfg, event="diagnostic", message="Полная диагностика выполнена",
            success=success, tv=tv, action="diagnostics", source="diagnostic",
        )
        show_screen(
            cfg, chat_id, msg_id,
            diagnostics_screen_text(cfg, tv, diagnostic), diagnostics_controls(tv["id"]),
        )
        return
    if action == "reconnect" and len(tvs) == 1:
        tv = tvs[0]
        reconnect_result = reconnect_tv(cfg, tv)
        diagnostic = run_diagnostics(cfg, tv)
        show_screen(
            cfg, chat_id, msg_id,
            diagnostics_screen_text(cfg, tv, diagnostic, notice=str(reconnect_result)),
            diagnostics_controls(tv["id"]),
        )
        return
    if action == "testsite" and len(tvs) == 1:
        tv = tvs[0]
        available, detail = check_site(tv["url"])
        record_event(
            cfg, event="site_check", message=f"Проверка сайта: {detail}",
            success=available, tv=tv, action="testsite", source="diagnostic",
        )
        diagnostic = run_diagnostics(cfg, tv)
        notice = f"{'✅' if available else '❌'} Проверка сайта: {detail}"
        show_screen(
            cfg, chat_id, msg_id,
            diagnostics_screen_text(cfg, tv, diagnostic, notice=notice),
            diagnostics_controls(tv["id"]),
        )
        return
    if action == "tvrefresh" and len(tvs) == 1 and not target.startswith("g_"):
        tv = tvs[0]
        enabled = not tv.get("auto_refresh", cfg.get("auto_refresh", False))
        set_tv_auto_refresh(cfg, tv["id"], enabled)
        tv = next(item for item in cfg["tvs"] if item.get("id") == target)
        text = (
            "✅ Автообновление включено"
            if enabled else
            "⏸ Автообновление выключено"
        )
        show_screen(
            cfg, chat_id, msg_id,
            settings_screen_text(cfg, target, tv, notice=text),
            settings_controls(cfg, target, tv),
        )
        return
    if action == "schedule":
        PENDING_SCHEDULE.pop(user_id, None)
        show_screen(
            cfg, chat_id, msg_id,
            "🕒 Расписание\n" + schedule_summary(cfg, target),
            schedule_controls(target),
        )
        return
    if action == "schedset":
        PENDING_URL.pop(user_id, None)
        PENDING_ADD_TV.pop(user_id, None)
        PENDING_SCHEDULE[user_id] = target
        label = target_label(tvs, target)
        show_screen(
            cfg, chat_id, msg_id,
            f"Настройка расписания для {label}.\n\n"
            "Отправьте одним сообщением:\n"
            "ВРЕМЯ_ВКЛЮЧЕНИЯ ВРЕМЯ_ОЖИДАНИЯ ДНИ\n\n"
            "Примеры:\n"
            "09:00 22:00 каждый день\n"
            "08:30 18:00 будни\n"
            "10:00 20:00 пн,ср,пт\n\n"
            "Часовой пояс: " + cfg.get("timezone", "Asia/Almaty") + "\n"
            "Для отмены отправьте /cancel.",
            {"inline_keyboard": [[
                {"text": "❌ Отмена", "callback_data": f"schedule:{target}"}
            ]]},
        )
        return
    if action == "schedoff":
        try:
            disable_schedule(cfg, target)
            show_screen(
                cfg, chat_id, msg_id,
                "⏸ Расписание отключено. Ручное управление продолжает работать.\n\n" +
                schedule_summary(cfg, target),
                schedule_controls(target),
            )
        except Exception as exc:
            send(cfg, chat_id, f"Не удалось отключить расписание: {exc}")
        return
    if action == "rebootask":
        label = target_label(tvs, target)
        show_screen(
            cfg, chat_id, msg_id,
            f"⚠️ Перезагрузить {label}?",
            reboot_confirmation(target),
        )
        return
    if action == "deleteask":
        if len(tvs) == 1:
            tv = tvs[0]
            show_screen(
                cfg, chat_id, msg_id,
                f"⚠️ Удалить телевизор «{tv['name']}»?",
                delete_confirmation(target),
            )
        else:
            show_main_screen(cfg, chat_id, msg_id, notice="Телевизор не найден")
        return
    if action == "delete":
        try:
            deleted = delete_tv(cfg, target)
            show_main_screen(
                cfg, chat_id, msg_id,
                notice=f"🗑 Телевизор «{deleted['name']}» удалён",
            )
        except Exception as exc:
            show_main_screen(cfg, chat_id, msg_id, notice=f"❌ Ошибка удаления: {exc}")
        return
    if action == "seturl":
        PENDING_SCHEDULE.pop(user_id, None)
        PENDING_URL[user_id] = target
        label = target_label(tvs, target)
        show_screen(
            cfg, chat_id, msg_id,
            f"Отправьте новую HTTPS-ссылку или разрешённый HTTP-адрес для {label}.\n"
            "Для отмены отправьте /cancel.",
            {"inline_keyboard": [[
                {"text": "❌ Отмена", "callback_data": f"settings:{target}"}
            ]]},
        )
        return
    if action == "screenshot":
        handle_screenshots(cfg, chat_id, tvs)
        return
    if action in KEY_ACTIONS:
        results = operate_many(cfg, tvs, action)
        errors = [res for _, res in results if not operation_succeeded(res)]
        if errors:
            send(cfg, chat_id, f"❌ {errors[0]}")
        return
    if action not in {"on", "off", "web", "both", "screen", "reboot"}:
        return
    results = operate_many(cfg, tvs, action)
    formatted = [f'{tv["name"]}: {res}' for tv, res in results]
    notice = "\n".join(formatted)
    if action in {"screen", "reboot"}:
        tv = tvs[0] if len(tvs) == 1 and not target.startswith("g_") else None
        show_screen(
            cfg, chat_id, msg_id,
            settings_screen_text(cfg, target, tv, notice=notice).replace(
                "— Группа", f"— {target_label(tvs, target)}"
            ),
            settings_controls(cfg, target, tv),
        )
    else:
        show_control_screen(cfg, chat_id, msg_id, target, tvs, notice=notice)


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


def telegram_retry_delay(failures):
    return min(60, 3 * (2 ** min(max(failures - 1, 0), 5)))


def get_initial_update_offset(cfg):
    failures = 0
    while True:
        try:
            old = telegram(cfg, "getUpdates", {"offset": -1, "timeout": 0})
            update_heartbeat()
            log_recovery("telegram_polling", "Соединение с Telegram восстановлено")
            return old[-1]["update_id"] + 1 if old else None
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            failures += 1
            log_throttled_warning(
                "telegram_polling",
                f"Ошибка начального соединения с Telegram: {exc}",
                cfg.get("log_repeat_interval_seconds", 900),
            )
            time.sleep(telegram_retry_delay(failures))


def main():
    configure_logging()
    cfg = load_config()
    set_bot_commands(cfg)
    # Skip commands accumulated while the bot was offline.
    offset = get_initial_update_offset(cfg)
    threading.Thread(target=status_loop, args=(cfg,), daemon=True).start()
    threading.Thread(target=refresh_loop, args=(cfg,), daemon=True).start()
    if cfg["keep_awake"]:
        threading.Thread(target=keep_awake_loop, args=(cfg,), daemon=True).start()
    threading.Thread(target=schedule_loop, args=(cfg,), daemon=True).start()
    threading.Thread(target=healthcheck_loop, args=(cfg,), daemon=True).start()
    logging.info("Бот работает. Для остановки нажмите Ctrl+C.")
    dispatcher = UpdateDispatcher(max_workers=10)
    connection_failures = 0
    while True:
        try:
            payload = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                payload["offset"] = offset
            updates = telegram(cfg, "getUpdates", payload)
            update_heartbeat()
            connection_failures = 0
            log_recovery("telegram_polling", "Соединение с Telegram восстановлено")
            for update in updates:
                dispatcher.submit(cfg, update)
                offset = update["update_id"] + 1
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            connection_failures += 1
            log_throttled_warning(
                "telegram_polling",
                f"Ошибка соединения с Telegram: {exc}",
                cfg.get("log_repeat_interval_seconds", 900),
            )
            time.sleep(telegram_retry_delay(connection_failures))


def print_config_summary(cfg):
    print("Конфигурация корректна")
    print(f"Телевизоров: {len(cfg.get('tvs', []))}")
    print(f"Разрешённых пользователей: {len(cfg.get('allowed_user_ids', []))}")
    print(f"Часовой пояс: {cfg.get('timezone')}")
    print(f"ADB: {cfg.get('adb_path')}")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--healthcheck"]:
            raise SystemExit(0 if runtime_is_healthy() else 1)
        if sys.argv[1:] == ["--check-config"]:
            print_config_summary(load_config())
        elif sys.argv[1:]:
            raise SystemExit("Использование: tv_bot.py [--check-config|--healthcheck]")
        else:
            main()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        sys.exit(f"Ошибка настройки: {exc}")
    except KeyboardInterrupt:
        logging.info("Бот остановлен")
