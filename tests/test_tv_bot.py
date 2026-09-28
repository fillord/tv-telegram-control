import json
import os
import tempfile
import threading
import time
import unittest
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
        self.assertTrue(cfg["keep_awake"])
        self.assertEqual(cfg["keep_awake_interval_seconds"], 60)

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


class ParsingTests(unittest.TestCase):
    def test_quick_add_supports_multiword_name(self):
        ip, port, name, url = tv_bot.parse_addtv_arguments(
            "192.168.0.120 TCL Конференц зал"
        )
        self.assertEqual((ip, port), ("192.168.0.120", 5555))
        self.assertEqual(name, "TCL Конференц зал")
        self.assertIsNone(url)

    def test_quick_add_supports_url_as_last_argument(self):
        ip, port, name, url = tv_bot.parse_addtv_arguments(
            "192.168.0.120:5566 TCL Зал https://example.com/tv"
        )
        self.assertEqual((ip, port), ("192.168.0.120", 5566))
        self.assertEqual(name, "TCL Зал")
        self.assertEqual(url, "https://example.com/tv")

    def test_ipv6_is_rejected_because_adb_addresses_are_ipv4(self):
        with self.assertRaises(ValueError):
            tv_bot.parse_ip_port("2001:db8::1")


class CallbackTests(unittest.TestCase):
    def setUp(self):
        tv_bot.PENDING_URL.clear()
        tv_bot.PENDING_ADD_TV.clear()
        self.cfg = {
            "telegram_token": "token",
            "allowed_user_ids": {123},
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
            mock.patch.object(tv_bot, "operate_many", side_effect=operate),
        ):
            tv_bot.process(self.cfg, self.callback("off:tv000001"))
        self.assertEqual(order, ["ack", "operate"])


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

        with mock.patch.object(tv_bot, "_operate_unlocked", side_effect=operation):
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
            ["connect", "disconnect", "kill-server", "start-server", "connect", "-s"],
        )

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
        ):
            ok, output = tv_bot.wake_tv_with_retry(
                {}, tv, "192.168.0.10:5555"
            )
        self.assertTrue(ok)
        self.assertEqual(output, "")
        wol.assert_called_once()
        self.assertEqual(adb.call_count, 3)


class MonitoringTests(unittest.TestCase):
    def test_monitor_debounces_failure_and_recovery(self):
        state = {"online": None, "failures": 0, "successes": 0}
        self.assertIsNone(tv_bot.availability_transition(state, False, 3, 2))
        self.assertIsNone(tv_bot.availability_transition(state, False, 3, 2))
        self.assertEqual(tv_bot.availability_transition(state, False, 3, 2), "down")
        self.assertIsNone(tv_bot.availability_transition(state, True, 3, 2))
        self.assertEqual(tv_bot.availability_transition(state, True, 3, 2), "up")


if __name__ == "__main__":
    unittest.main()
