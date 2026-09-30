"""Thread-safe persistence for mutable state, history, and status cache."""

import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS = {}


def _path_lock(path):
    key = str(Path(path).resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class RuntimeState:
    """Mutable TV state stored separately from operator configuration."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = _path_lock(self.path)

    def load(self):
        with self._lock:
            try:
                with self.path.open(encoding="utf-8") as file:
                    data = json.load(file)
            except FileNotFoundError:
                data = {"version": 1, "tvs": {}}
            if not isinstance(data, dict) or not isinstance(data.get("tvs", {}), dict):
                raise ValueError("state.json повреждён: ожидается объект tvs")
            data.setdefault("version", 1)
            data.setdefault("tvs", {})
            return data

    def save(self, data):
        with self._lock:
            _atomic_write_json(self.path, data)

    def update_tv(self, tv_id, values=None, *, section=None, remove=False):
        with self._lock:
            data = self.load()
            if remove:
                data["tvs"].pop(tv_id, None)
            else:
                target = data["tvs"].setdefault(tv_id, {})
                if section:
                    target = target.setdefault(section, {})
                target.update(values or {})
            self.save(data)
            return data

    def replace_tv_section(self, tv_id, section, values):
        with self._lock:
            data = self.load()
            data["tvs"].setdefault(tv_id, {})[section] = dict(values)
            self.save(data)

    def remove_tv(self, tv_id):
        self.update_tv(tv_id, remove=True)


class EventHistory:
    """Bounded JSON-lines audit trail for commands and monitoring events."""

    def __init__(self, path, max_events=1000):
        self.path = Path(path)
        self.max_events = max(50, int(max_events))
        self._lock = _path_lock(self.path)

    def append(self, *, event, message, success=True, tv=None, action=None, source="bot"):
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": str(event),
            "success": bool(success),
            "message": str(message)[:500],
            "source": str(source),
        }
        if tv:
            entry["tv_id"] = tv.get("id")
            entry["tv_name"] = tv.get("name")
        if action:
            entry["action"] = str(action)
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as file:
                file.write(line + "\n")
            os.chmod(self.path, 0o600)
            self._trim_if_needed()
        return entry

    def _trim_if_needed(self):
        try:
            with self.path.open(encoding="utf-8") as file:
                lines = deque(file, maxlen=self.max_events)
        except FileNotFoundError:
            return
        if len(lines) < self.max_events:
            return
        temporary = self.path.with_name(self.path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as file:
            file.writelines(lines)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def recent(self, limit=15, tv_id=None):
        limit = max(1, min(int(limit), 100))
        with self._lock:
            try:
                with self.path.open(encoding="utf-8") as file:
                    lines = deque(file, maxlen=self.max_events)
            except FileNotFoundError:
                return []
        result = []
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if tv_id and entry.get("tv_id") != tv_id:
                continue
            result.append(entry)
            if len(result) >= limit:
                break
        return result


class StatusCache:
    """Thread-safe TTL cache that keeps menu rendering independent from ADB."""

    def __init__(self):
        self._lock = threading.RLock()
        self._items = {}

    def put(self, tv_id, status, *, error="", checked_at=None):
        item = {
            "status": tuple(status),
            "checked_at": float(checked_at if checked_at is not None else time.time()),
            "error": str(error or ""),
        }
        with self._lock:
            self._items[tv_id] = item
        return item

    def get(self, tv_id, max_age=None):
        with self._lock:
            item = self._items.get(tv_id)
            if item is None:
                return None
            item = dict(item)
        item["age"] = max(0.0, time.time() - item["checked_at"])
        item["fresh"] = max_age is None or item["age"] <= max_age
        return item

    def statuses(self, tv_ids, max_age=None, include_stale=True):
        result = {}
        for tv_id in tv_ids:
            item = self.get(tv_id, max_age=max_age)
            if item and (include_stale or item["fresh"]):
                result[tv_id] = item["status"]
        return result

    def invalidate(self, tv_id=None):
        with self._lock:
            if tv_id is None:
                self._items.clear()
            else:
                self._items.pop(tv_id, None)

    def prune(self, tv_ids):
        valid = set(tv_ids)
        with self._lock:
            for tv_id in set(self._items) - valid:
                self._items.pop(tv_id, None)
