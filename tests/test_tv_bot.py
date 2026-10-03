import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import tv_bot


def sample_tv(tv_id="tv000001", name="Зал", ip="192.168.0.10"):
    return {
        "id": tv_id,
        "name": name,
        "ip": ip,
        "port": 5555,
        "url": "https://example.com/tv",
    }


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.config = Path(self.tempdir.name) / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "telegram_token": "valid-token",
                    "allowed_user_ids": [123],
                    "adb_path": "/bin/echo",
                    "tvs": [
                        {
                            "name": "Зал",
                            "ip": "192.168.0.10",
                            "url": "https://example.com/tv",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.config_patch = mock.patch.object(tv_bot, "CONFIG", self.config)
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.tempdir.cleanup()

    def test_load_config_migrates_stable_id_and_secure_permissions(self):
        cfg = tv_bot.load_config()
        stored = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertRegex(cfg["tvs"][0]["id"], r"^[a-f0-9]{12}$")
        self.assertEqual(stored["tvs"][0]["id"], cfg["tvs"][0]["id"])
        self.assertEqual(os.stat(self.config).st_mode & 0o777, 0o600)
        self.assertEqual(cfg["healthcheck_interval_seconds"], 60)
        self.assertFalse(cfg["auto_refresh"])
        self.assertTrue(cfg["keep_awake"])
        self.assertEqual(cfg["keep_awake_interval_seconds"], 60)
        self.assertEqual(cfg["wake_timeout_seconds"], 45)
        self.assertEqual(cfg["wake_verify_timeout_seconds"], 10)
        self.assertEqual(cfg["schedule_retry_attempts"], 3)
        self.assertEqual(cfg["schedule_retry_delay_seconds"], 60)

    def test_duplicate_endpoint_is_rejected(self):
        cfg = tv_bot.load_config()
        with self.assertRaisesRegex(ValueError, "уже добавлен"):
            tv_bot.add_tv(cfg, "Дубликат", "192.168.0.10", 5555)

    def test_last_tv_cannot_be_deleted(self):
        cfg = tv_bot.load_config()
        with self.assertRaisesRegex(ValueError, "последний"):
            tv_bot.delete_tv(cfg, cfg["tvs"][0]["id"])

    def test_adb_path_can_be_overridden_for_container(self):
        with mock.patch.dict(os.environ, {"TV_BOT_ADB_PATH": "/bin/echo"}):
            cfg = tv_bot.load_config()
        self.assertEqual(cfg["adb_path"], "/bin/echo")

    def test_environment_adb_override_is_not_written_to_config(self):
        with mock.patch.dict(os.environ, {"TV_BOT_ADB_PATH": "/usr/bin/env"}):
            cfg = tv_bot.load_config()
        stored = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(cfg["adb_path"], "/usr/bin/env")
        self.assertEqual(stored["adb_path"], "/bin/echo")

    def test_mutable_runtime_fields_are_migrated_out_of_config(self):
        stored = json.loads(self.config.read_text(encoding="utf-8"))
        stored["tvs"][0]["manual_sleep"] = True
        stored["tvs"][0]["schedule"] = {
            "enabled": True, "on": "09:00", "off": "22:00",
            "days": list(range(7)), "last_on": "2026-09-29",
        }
        self.config.write_text(json.dumps(stored), encoding="utf-8")
        cfg = tv_bot.load_config()
        clean = json.loads(self.config.read_text(encoding="utf-8"))
        state = json.loads(self.config.with_name("state.json").read_text(encoding="utf-8"))
        tv_id = cfg["tvs"][0]["id"]
        self.assertNotIn("manual_sleep", clean["tvs"][0])
        self.assertNotIn("last_on", clean["tvs"][0]["schedule"])
        self.assertTrue(state["tvs"][tv_id]["manual_sleep"])
        self.assertEqual(state["tvs"][tv_id]["schedule"]["last_on"], "2026-09-29")

    def test_tv_can_be_edited_grouped_and_reordered(self):
        cfg = tv_bot.load_config()
        first = cfg["tvs"][0]
        second = tv_bot.add_tv(cfg, "Второй", "192.168.0.11")
        updated = tv_bot.update_tv(
            cfg, second["id"], name="Ресепшен", ip="192.168.0.12",
            port=5566, mac="AA-BB-CC-DD-EE-FF", group="Первый этаж",
        )
        self.assertEqual(updated["name"], "Ресепшен")
        self.assertEqual(updated["group"], "Первый этаж")
        self.assertEqual(updated["mac"], "AA:BB:CC:DD:EE:FF")
        tv_bot.move_tv(cfg, second["id"], "up")
        self.assertEqual(cfg["tvs"][0]["id"], second["id"])
        self.assertEqual(cfg["tvs"][1]["id"], first["id"])


class ParsingTests(unittest.TestCase):
    def test_allowlisted_http_url_is_allowed(self):
        url = "http://192.168.8.249:3000/a"
        with mock.patch.dict(os.environ, {"TV_BOT_HTTP_ALLOWED_URLS": url}):
            self.assertEqual(tv_bot.validate_url(url), url)

    def test_unlisted_http_url_is_rejected(self):
        with mock.patch.dict(os.environ, {"TV_BOT_HTTP_ALLOWED_URLS": ""}):
            with self.assertRaisesRegex(ValueError, "TV_BOT_HTTP_ALLOWED_URLS"):
                tv_bot.validate_url("http://192.168.8.249:3000/a")

    def test_http_allowlist_requires_exact_url(self):
        allowed = "http://192.168.8.249:3000/a"
        with mock.patch.dict(os.environ, {"TV_BOT_HTTP_ALLOWED_URLS": allowed}):
            with self.assertRaises(ValueError):
                tv_bot.validate_url("http://192.168.8.249:3000/other")

    def test_quick_add_supports_multiword_name(self):
        ip, port, mac, name, url = tv_bot.parse_addtv_arguments(
            "192.168.0.120 TCL Конференц зал"
        )
        self.assertEqual((ip, port), ("192.168.0.120", 5555))
        self.assertIsNone(mac)
        self.assertEqual(name, "TCL Конференц зал")
        self.assertIsNone(url)

    def test_quick_add_supports_url_as_last_argument(self):
        ip, port, mac, name, url = tv_bot.parse_addtv_arguments(
            "192.168.0.120:5566 TCL Зал https://example.com/tv"
        )
        self.assertEqual((ip, port), ("192.168.0.120", 5566))
        self.assertIsNone(mac)
        self.assertEqual(name, "TCL Зал")
        self.assertEqual(url, "https://example.com/tv")

    def test_quick_add_supports_mac_name_and_url(self):
        ip, port, mac, name, url = tv_bot.parse_addtv_arguments(
            "192.168.0.120 02-00-00-00-00-01 TCL Вход https://example.com/tv"
        )
        self.assertEqual((ip, port), ("192.168.0.120", 5555))
        self.assertEqual(mac, "02:00:00:00:00:01")
        self.assertEqual(name, "TCL Вход")
        self.assertEqual(url, "https://example.com/tv")

    def test_ipv6_is_rejected_because_adb_addresses_are_ipv4(self):
        with self.assertRaises(ValueError):
            tv_bot.parse_ip_port("2001:db8::1")


class CallbackTests(unittest.TestCase):
    def setUp(self):
        tv_bot.PENDING_URL.clear()
        tv_bot.PENDING_ADD_TV.clear()
        tv_bot.PENDING_SCHEDULE.clear()
        self.cfg = {
            "telegram_token": "token",
            "allowed_user_ids": {123},
            "auto_refresh": False,
            "tvs": [sample_tv()],
        }

    @staticmethod
    def callback(data):
        return {
            "update_id": 1,
            "callback_query": {
                "id": "callback-1",
                "data": data,
                "from": {"id": 123},
                "message": {"message_id": 5, "chat": {"id": 123, "type": "private"}},
            },
        }

    def test_old_numeric_button_is_rejected_without_operation(self):
        with (
            mock.patch.object(tv_bot, "telegram"),
            mock.patch.object(tv_bot, "send") as send,
            mock.patch.object(tv_bot, "operate_many") as operate,
        ):
            tv_bot.process(self.cfg, self.callback("off:0"))
        operate.assert_not_called()
        self.assertIn("устарела", send.call_args.args[2])

    def test_stable_id_still_targets_same_tv_after_list_changes(self):
        order = []

        def telegram(*_args, **_kwargs):
            order.append("ack")

        def operate(_cfg, tvs, action):
            order.append("operate")
            self.assertEqual(tvs[0]["id"], "tv000001")
            self.assertEqual(action, "off")
            return [(tvs[0], "Отправлена команда ожидания")]

        with (
            mock.patch.object(tv_bot, "telegram", side_effect=telegram),
            mock.patch.object(tv_bot, "send"),
            mock.patch.object(tv_bot, "edit_message"),
            mock.patch.object(tv_bot, "get_tv_status", return_value=("sleep", "Сон", "💤")),
            mock.patch.object(tv_bot, "operate_many", side_effect=operate),
        ):
            tv_bot.process(self.cfg, self.callback("off:tv000001"))
        self.assertEqual(order, ["ack", "operate"])

    def test_auto_refresh_can_be_toggled_from_menu(self):
        def enable(cfg, value):
            cfg["auto_refresh"] = value

        with (
            mock.patch.object(tv_bot, "telegram"),
            mock.patch.object(tv_bot, "set_auto_refresh", side_effect=enable) as setter,
            mock.patch.object(tv_bot, "get_all_tv_statuses", return_value={}),
            mock.patch.object(tv_bot, "edit_message") as edit,
        ):
            tv_bot.process(self.cfg, self.callback("toggle_auto_refresh"))
        setter.assert_called_once_with(self.cfg, True)
        self.assertTrue(self.cfg["auto_refresh"])
        keyboard = edit.call_args.args[4]["inline_keyboard"]
        self.assertTrue(any("Автообновление страниц: ВКЛ" in button["text"]
                            for row in keyboard for button in row))

    def test_auto_refresh_can_be_enabled_for_one_tv(self):
        def enable(cfg, tv_id, value):
            self.assertEqual(tv_id, "tv000001")
            cfg["tvs"][0]["auto_refresh"] = value

        with (
            mock.patch.object(tv_bot, "telegram"),
            mock.patch.object(tv_bot, "set_tv_auto_refresh", side_effect=enable) as setter,
            mock.patch.object(tv_bot, "edit_message") as edit,
        ):
            tv_bot.process(self.cfg, self.callback("tvrefresh:tv000001"))
        setter.assert_called_once_with(self.cfg, "tv000001", True)
        keyboard = edit.call_args.args[4]["inline_keyboard"]
        self.assertTrue(any("Автообновление: ВКЛ" in button["text"]
                            for row in keyboard for button in row))

    def test_remote_opens_compact_submenu_without_device_operation(self):
        with (
            mock.patch.object(tv_bot, "telegram"),
            mock.patch.object(tv_bot, "edit_message") as edit,
            mock.patch.object(tv_bot, "operate_many") as operate,
        ):
            tv_bot.process(self.cfg, self.callback("remote:tv000001"))
        operate.assert_not_called()
        self.assertIn("Пульт", edit.call_args.args[3])
        callbacks = [
            button["callback_data"]
            for row in edit.call_args.args[4]["inline_keyboard"]
            for button in row
        ]
        self.assertIn("up:tv000001", callbacks)
        self.assertIn("select:tv000001", callbacks)

    def test_remote_key_acknowledges_with_popup_text(self):
        with (
            mock.patch.object(tv_bot, "telegram") as telegram,
            mock.patch.object(
                tv_bot, "operate_many",
                return_value=[(self.cfg["tvs"][0], "⬆️ Вверх выполнено")],
            ),
        ):
            tv_bot.process(self.cfg, self.callback("up:tv000001"))
        payload = telegram.call_args_list[0].args[2]
        self.assertEqual(payload["text"], "⬆️ Вверх")


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.tv = sample_tv()
        self.cfg = {"auto_refresh": False, "tvs": [self.tv]}

    def test_main_menu_uses_short_tv_labels_and_summary(self):
        statuses = {self.tv["id"]: ("on", "Включен", "🟢")}
        keyboard = tv_bot.menu(self.cfg, statuses=statuses)["inline_keyboard"]
        self.assertEqual(keyboard[0][0]["text"], "🟢 Зал")
        self.assertNotIn("(Включен)", keyboard[0][0]["text"])
        self.assertIn("🟢 1", tv_bot.main_screen_text(statuses))

    def test_tv_actions_are_split_into_submenus(self):
        keyboard = tv_bot.actions(self.cfg, self.tv["id"], self.tv)["inline_keyboard"]
        callbacks = [button["callback_data"] for row in keyboard for button in row]
        self.assertIn("remote:tv000001", callbacks)
        self.assertIn("sound:tv000001", callbacks)
        self.assertIn("settings:tv000001", callbacks)
        self.assertNotIn("up:tv000001", callbacks)
        self.assertNotIn("volup:tv000001", callbacks)

    def test_unchanged_screen_does_not_create_duplicate_message(self):
        with (
            mock.patch.object(
                tv_bot, "telegram", side_effect=RuntimeError("message is not modified")
            ),
            mock.patch.object(tv_bot, "send") as send,
        ):
            tv_bot.edit_message({}, 123, 5, "same")
        send.assert_not_called()

    def test_group_button_targets_all_tvs_in_group(self):
        second = sample_tv("tv000002", "Вход", "192.168.0.11")
        self.tv["group"] = "Первый этаж"
        second["group"] = "Первый этаж"
        cfg = {"auto_refresh": False, "tvs": [self.tv, second]}
        keyboard = tv_bot.menu(cfg, statuses={})["inline_keyboard"]
        target = tv_bot.group_key("Первый этаж")
        callbacks = [button["callback_data"] for row in keyboard for button in row]
        self.assertIn(f"select:{target}", callbacks)
        self.assertEqual(len(tv_bot.resolve_target(cfg, target)), 2)


class RuntimeComponentTests(unittest.TestCase):
    def test_structured_result_remains_string_compatible(self):
        result = tv_bot.operation_failure("Ошибка: тест", "test_error", "detail")
        self.assertIsInstance(result, str)
        self.assertFalse(result.success)
        self.assertEqual(result.code, "test_error")
        self.assertFalse(tv_bot.operation_succeeded(result))

    def test_status_cache_avoids_repeated_adb_probe(self):
        tv_bot.STATUS_CACHE.invalidate()
        tv = sample_tv()
        cfg = {"tvs": [tv], "status_cache_seconds": 15}
        with mock.patch.object(
            tv_bot, "get_tv_status", return_value=("on", "Включен", "🟢")
        ) as status:
            first = tv_bot.get_cached_tv_status(cfg, tv)
            second = tv_bot.get_cached_tv_status(cfg, tv)
        self.assertEqual(first, second)
        status.assert_called_once()


class DispatcherTests(unittest.TestCase):
    def test_updates_are_ordered_per_user(self):
        completed = []
        guard = threading.Lock()

        def process(_cfg, update):
            if update["update_id"] == 1:
                time.sleep(0.03)
            with guard:
                completed.append(update["update_id"])

        def update(update_id, user_id):
            return {
                "update_id": update_id,
                "message": {
                    "from": {"id": user_id},
                    "chat": {"id": user_id, "type": "private"},
                    "text": "/start",
                },
            }

        with mock.patch.object(tv_bot, "safe_process", side_effect=process):
            dispatcher = tv_bot.UpdateDispatcher(max_workers=3)
            dispatcher.submit({}, update(1, 100))
            dispatcher.submit({}, update(2, 100))
            dispatcher.submit({}, update(3, 200))
            dispatcher.shutdown()
        self.assertLess(completed.index(1), completed.index(2))


class DeviceSafetyTests(unittest.TestCase):
    def setUp(self):
        tv_bot.ADB_LAST_RECOVERY = 0.0

    def test_unknown_power_state_is_not_treated_as_awake(self):
        tv = sample_tv()
        with (
            mock.patch.object(tv_bot, "is_device_reachable", return_value=True),
            mock.patch.object(tv_bot, "connect", return_value=("192.168.0.10:5555", "")),
            mock.patch.object(tv_bot, "adb", return_value=(True, "unrecognized output")),
        ):
            status = tv_bot.get_tv_status({}, tv)
        self.assertEqual(status[0], "unknown")

    def test_same_tv_gets_same_lock(self):
        tv = sample_tv()
        self.assertIs(tv_bot.get_tv_lock(tv), tv_bot.get_tv_lock(dict(tv)))
        self.assertIsInstance(tv_bot.get_tv_lock(tv), type(threading.RLock()))

    def test_operations_for_same_tv_are_serialized(self):
        tv = sample_tv()
        active = 0
        maximum_active = 0
        guard = threading.Lock()

        def operation(*_args, **_kwargs):
            nonlocal active, maximum_active
            with guard:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.02)
            with guard:
                active -= 1
            return "ok"

        with (
            mock.patch.object(tv_bot, "_operate_unlocked", side_effect=operation),
            mock.patch.object(tv_bot, "record_event"),
        ):
            threads = [
                threading.Thread(target=tv_bot.operate, args=({}, tv, "off"))
                for _ in range(3)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(maximum_active, 1)

    def test_stuck_adb_server_is_restarted_and_connect_retried(self):
        tv = sample_tv()
        responses = iter([
            (True, "failed to connect: No route to host"),
            (True, "disconnected"),
            (True, "failed to connect: No route to host"),
            (True, ""),
            (True, "daemon started"),
            (True, "connected"),
            (True, "device"),
        ])
        with (
            mock.patch.object(tv_bot, "is_device_reachable", return_value=True),
            mock.patch.object(tv_bot, "adb", side_effect=lambda *_a, **_k: next(responses)) as adb,
        ):
            address, error = tv_bot.connect({}, tv)
        self.assertEqual(address, "192.168.0.10:5555")
        self.assertEqual(error, "")
        commands = [call.args[1] for call in adb.call_args_list]
        self.assertEqual(
            commands,
            ["connect", "disconnect", "connect", "kill-server", "start-server", "connect", "-s"],
        )

    def test_device_reconnect_avoids_global_adb_restart(self):
        tv = sample_tv()
        responses = iter([
            (True, "failed to connect: No route to host"),
            (True, "disconnected"),
            (True, "connected"),
            (True, "device"),
        ])
        with (
            mock.patch.object(tv_bot, "is_device_reachable", return_value=True),
            mock.patch.object(tv_bot, "adb", side_effect=lambda *_a, **_k: next(responses)) as adb,
            mock.patch.object(tv_bot, "recover_adb_server") as recover,
        ):
            address, error = tv_bot.connect({}, tv)
        self.assertEqual((address, error), ("192.168.0.10:5555", ""))
        recover.assert_not_called()
        self.assertEqual([call.args[1] for call in adb.call_args_list], ["connect", "disconnect", "connect", "-s"])

    def test_unreachable_tv_does_not_restart_adb_server(self):
        tv = sample_tv()
        with (
            mock.patch.object(tv_bot, "is_device_reachable", return_value=False),
            mock.patch.object(tv_bot, "adb", return_value=(True, "")) as adb,
        ):
            address, error = tv_bot.connect({}, tv)
        self.assertIsNone(address)
        self.assertIn("недоступен по сети", error)
        self.assertNotIn("kill-server", [call.args[1] for call in adb.call_args_list])

    def test_first_on_waits_until_adb_port_opens_after_wol(self):
        tv = sample_tv()
        tv["mac"] = "AA:BB:CC:DD:EE:FF"
        with (
            mock.patch.object(tv_bot, "wake_on_lan") as wol,
            mock.patch.object(tv_bot, "is_device_reachable", side_effect=[False, False, True]) as reachable,
            mock.patch.object(tv_bot.time, "sleep"),
            mock.patch.object(tv_bot, "adb", side_effect=[(True, "connected"), (True, "device")]),
        ):
            address, error = tv_bot.connect({"wake_timeout_seconds": 45}, tv, wake=True)
        self.assertEqual(address, "192.168.0.10:5555")
        self.assertEqual(error, "")
        wol.assert_called_once()
        self.assertEqual(reachable.call_count, 3)

    def test_intentional_standby_is_reported_as_sleep_when_port_is_closed(self):
        tv = sample_tv()
        tv["manual_sleep"] = True
        with mock.patch.object(tv_bot, "is_device_reachable", return_value=False):
            self.assertEqual(tv_bot.get_tv_status({}, tv), ("sleep", "Сон", "💤"))

    def test_keep_awake_applies_settings_and_wakes_sleeping_tv(self):
        tv = sample_tv()
        adb_results = [
            *((True, "") for _ in tv_bot.KEEP_AWAKE_COMMANDS),
            (True, "mWakefulness=Asleep\nDisplay Power: state=OFF"),
            (True, ""),
        ]
        with (
            mock.patch.object(tv_bot, "is_device_reachable", return_value=True),
            mock.patch.object(tv_bot, "connect", return_value=("192.168.0.10:5555", "")),
            mock.patch.object(tv_bot, "adb", side_effect=adb_results) as adb,
        ):
            ok, error = tv_bot.ensure_tv_awake({"tvs": [tv]}, tv)
        self.assertTrue(ok)
        self.assertEqual(error, "")
        self.assertEqual(adb.call_count, len(tv_bot.KEEP_AWAKE_COMMANDS) + 2)
        self.assertEqual(adb.call_args_list[-1].args[-2:], ("keyevent", "224"))

    def test_watchdog_skips_intentional_manual_sleep(self):
        tv = sample_tv()
        tv["manual_sleep"] = True
        cfg = {"tvs": [tv]}
        with mock.patch.object(tv_bot, "_ensure_tv_awake_unlocked") as ensure:
            ok, error = tv_bot.ensure_tv_awake(cfg, dict(tv))
        self.assertTrue(ok)
        self.assertEqual(error, "")
        ensure.assert_not_called()

    def test_off_marks_manual_sleep_only_after_success(self):
        tv = sample_tv()
        with (
            mock.patch.object(tv_bot, "connect", return_value=("192.168.0.10:5555", "")),
            mock.patch.object(tv_bot, "adb", return_value=(True, "")),
            mock.patch.object(tv_bot, "set_manual_sleep") as set_sleep,
        ):
            result = tv_bot._operate_unlocked({}, tv, "off")
        self.assertEqual(result, "Отправлена команда ожидания")
        set_sleep.assert_called_once_with({}, tv["id"], True)

    def test_on_resumes_automatic_keep_awake(self):
        tv = sample_tv()
        with (
            mock.patch.object(tv_bot, "set_manual_sleep") as set_sleep,
            mock.patch.object(tv_bot, "connect", return_value=("192.168.0.10:5555", "")),
            mock.patch.object(tv_bot, "adb", return_value=(True, "")),
        ):
            tv_bot._operate_unlocked({}, tv, "on")
        set_sleep.assert_called_once_with({}, tv["id"], False)

    def test_wakeup_reconnects_and_retries_after_timeout(self):
        tv = sample_tv()
        tv["mac"] = "AA:BB:CC:DD:EE:FF"
        adb_results = [
            (False, "timed out"),
            (True, "disconnected"),
            (True, ""),
        ]
        with (
            mock.patch.object(tv_bot, "adb", side_effect=adb_results) as adb,
            mock.patch.object(tv_bot, "wake_on_lan") as wol,
            mock.patch.object(tv_bot.time, "sleep"),
            mock.patch.object(tv_bot, "connect", return_value=("192.168.0.10:5555", "")),
            mock.patch.object(tv_bot, "confirm_tv_awake", return_value=(True, "")),
        ):
            ok, output = tv_bot.wake_tv_with_retry(
                {}, tv, "192.168.0.10:5555"
            )
        self.assertTrue(ok)
        self.assertEqual(output, "")
        wol.assert_called_once()
        self.assertEqual(adb.call_count, 3)

    def test_wakeup_fails_when_screen_stays_asleep(self):
        tv = sample_tv()
        with (
            mock.patch.object(tv_bot, "adb", return_value=(True, "")),
            mock.patch.object(
                tv_bot, "confirm_tv_awake",
                return_value=(False, "Экран остался в режиме сна"),
            ),
        ):
            ok, output = tv_bot.wake_tv_with_retry({}, tv, "192.168.0.10:5555")
        self.assertFalse(ok)
        self.assertIn("режиме сна", output)


class MonitoringTests(unittest.TestCase):
    def test_monitor_debounces_failure_and_recovery(self):
        state = {"online": None, "failures": 0, "successes": 0}
        self.assertIsNone(tv_bot.availability_transition(state, False, 3, 2))
        self.assertIsNone(tv_bot.availability_transition(state, False, 3, 2))
        self.assertEqual(tv_bot.availability_transition(state, False, 3, 2), "down")
        self.assertIsNone(tv_bot.availability_transition(state, True, 3, 2))
        self.assertEqual(tv_bot.availability_transition(state, True, 3, 2), "up")

    def test_repeated_warning_is_throttled_and_recovery_is_logged(self):
        tv_bot.LOG_STATE.clear()
        with (
            mock.patch.object(tv_bot.time, "monotonic", side_effect=[0, 10]),
            mock.patch.object(tv_bot.logging, "warning") as warning,
            mock.patch.object(tv_bot.logging, "info") as info,
        ):
            tv_bot.log_throttled_warning("tv", "offline", repeat_seconds=60)
            tv_bot.log_throttled_warning("tv", "offline", repeat_seconds=60)
            tv_bot.log_recovery("tv", "online")
        warning.assert_called_once()
        info.assert_called_once_with("%s", "online")

    def test_runtime_health_uses_recent_heartbeat(self):
        with tempfile.TemporaryDirectory() as tempdir:
            heartbeat = Path(tempdir) / "heartbeat"
            with mock.patch.object(tv_bot, "HEARTBEAT", heartbeat):
                tv_bot.update_heartbeat()
                self.assertTrue(tv_bot.runtime_is_healthy(max_age_seconds=60))
                old = time.time() - 120
                os.utime(heartbeat, (old, old))
                self.assertFalse(tv_bot.runtime_is_healthy(max_age_seconds=60))


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.config = Path(self.tempdir.name) / "config.json"
        self.tv = sample_tv()
        self.tv["schedule"] = {
            "enabled": True,
            "on": "09:00",
            "off": "22:00",
            "days": [0, 1, 2, 3, 4],
        }
        self.stored = {
            "telegram_token": "token",
            "allowed_user_ids": [123],
            "timezone": "Asia/Almaty",
            "tvs": [self.tv],
        }
        self.config.write_text(json.dumps(self.stored), encoding="utf-8")
        self.config_patch = mock.patch.object(tv_bot, "CONFIG", self.config)
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.tempdir.cleanup()

    def test_parse_schedule_supports_presets_and_custom_days(self):
        weekdays = tv_bot.parse_schedule_text("08:30 18:00 будни")
        self.assertEqual(weekdays["days"], [0, 1, 2, 3, 4])
        custom = tv_bot.parse_schedule_text("10:00 20:00 пн,ср,пт")
        self.assertEqual(custom["days"], [0, 2, 4])

    def test_scheduled_on_opens_configured_site_once(self):
        cfg = dict(self.stored)
        cfg["allowed_user_ids"] = {123}
        monday = datetime(2026, 9, 28, 9, 0)
        with (
            mock.patch.object(
                tv_bot,
                "operate_many",
                return_value=[(self.tv, "Команда открытия сайта отправлена")],
            ) as operate,
            mock.patch.object(tv_bot, "notify_owners"),
        ):
            first = tv_bot.run_due_schedules(cfg, monday)
            second = tv_bot.run_due_schedules(cfg, monday)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        operate.assert_called_once()
        self.assertEqual(operate.call_args.args[2], "both")
        stored = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertNotIn("last_on", stored["tvs"][0]["schedule"])
        state = json.loads(self.config.with_name("state.json").read_text(encoding="utf-8"))
        self.assertEqual(
            state["tvs"][self.tv["id"]]["schedule"]["last_on"], "2026-09-28"
        )

    def test_scheduled_off_uses_standby_action(self):
        cfg = dict(self.stored)
        cfg["allowed_user_ids"] = {123}
        monday = datetime(2026, 9, 28, 22, 0)
        with (
            mock.patch.object(
                tv_bot,
                "operate_many",
                return_value=[(self.tv, "Отправлена команда ожидания")],
            ) as operate,
            mock.patch.object(tv_bot, "notify_owners"),
        ):
            tv_bot.run_due_schedules(cfg, monday)
        self.assertEqual(operate.call_args.args[2], "off")

    def test_scheduled_off_accepts_existing_manual_sleep(self):
        self.tv["manual_sleep"] = True
        self.config.write_text(json.dumps(self.stored), encoding="utf-8")
        cfg = dict(self.stored)
        cfg["allowed_user_ids"] = {123}
        monday = datetime(2026, 9, 28, 22, 0)
        with (
            mock.patch.object(tv_bot, "operate_many") as operate,
            mock.patch.object(tv_bot, "notify_owners"),
        ):
            completed = tv_bot.run_due_schedules(cfg, monday)
        operate.assert_not_called()
        self.assertEqual(completed[0][2], "Режим ожидания уже установлен")
        state = json.loads(self.config.with_name("state.json").read_text(encoding="utf-8"))
        self.assertEqual(
            state["tvs"][self.tv["id"]]["schedule"]["last_off"], "2026-09-28"
        )

    def test_missed_schedule_is_reconciled_after_startup(self):
        cfg = dict(self.stored)
        cfg["allowed_user_ids"] = {123}
        cfg["schedule_retry_attempts"] = 3
        cfg["schedule_retry_delay_seconds"] = 60
        monday_late = datetime(2026, 9, 28, 10, 15)
        with (
            mock.patch.object(
                tv_bot,
                "operate_many",
                return_value=[(self.tv, "Команда открытия сайта отправлена")],
            ) as operate,
            mock.patch.object(tv_bot, "notify_owners"),
        ):
            completed = tv_bot.run_due_schedules(cfg, monday_late)
        self.assertEqual(len(completed), 1)
        self.assertEqual(operate.call_args.args[2], "both")

    def test_failed_schedule_retries_and_marks_only_success(self):
        cfg = dict(self.stored)
        cfg["allowed_user_ids"] = {123}
        cfg["schedule_retry_attempts"] = 3
        cfg["schedule_retry_delay_seconds"] = 60
        first_at = datetime(2026, 9, 28, 9, 0)
        second_at = datetime(2026, 9, 28, 9, 1)
        with (
            mock.patch.object(
                tv_bot,
                "operate_many",
                side_effect=[
                    [(self.tv, "Не удалось открыть сайт")],
                    [(self.tv, "Команда открытия сайта отправлена")],
                ],
            ) as operate,
            mock.patch.object(tv_bot, "notify_owners"),
        ):
            first = tv_bot.run_due_schedules(cfg, first_at)
            state_after_failure = json.loads(
                self.config.with_name("state.json").read_text(encoding="utf-8")
            )
            second = tv_bot.run_due_schedules(cfg, second_at)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(operate.call_count, 2)
        failed_schedule = state_after_failure["tvs"][self.tv["id"]]["schedule"]
        self.assertNotIn("last_on", failed_schedule)
        self.assertFalse(failed_schedule["last_on_success"])
        final_schedule = json.loads(
            self.config.with_name("state.json").read_text(encoding="utf-8")
        )["tvs"][self.tv["id"]]["schedule"]
        self.assertEqual(final_schedule["last_on"], "2026-09-28")
        self.assertTrue(final_schedule["last_on_success"])

    def test_schedule_respects_retry_delay(self):
        cfg = dict(self.stored)
        cfg["allowed_user_ids"] = {123}
        cfg["schedule_retry_attempts"] = 3
        cfg["schedule_retry_delay_seconds"] = 60
        now = datetime(2026, 9, 28, 9, 0)
        with (
            mock.patch.object(
                tv_bot,
                "operate_many",
                return_value=[(self.tv, "Не удалось открыть сайт")],
            ) as operate,
            mock.patch.object(tv_bot, "notify_owners"),
        ):
            tv_bot.run_due_schedules(cfg, now)
            immediate = tv_bot.run_due_schedules(cfg, now)
        self.assertEqual(immediate, [])
        self.assertEqual(operate.call_count, 1)


if __name__ == "__main__":
    unittest.main()
