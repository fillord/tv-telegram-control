import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tv_bot


class TelegramAdbIntegrationTests(unittest.TestCase):
    def test_callback_flows_through_telegram_adb_state_and_history(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.json"
            tv = {
                "id": "tv000001",
                "name": "Зал",
                "ip": "192.168.0.10",
                "port": 5555,
                "url": "https://example.com/tv",
            }
            config_path.write_text(
                json.dumps({
                    "telegram_token": "token",
                    "allowed_user_ids": [123],
                    "adb_path": "/bin/echo",
                    "tvs": [tv],
                }),
                encoding="utf-8",
            )
            cfg = {
                "telegram_token": "token",
                "allowed_user_ids": {123},
                "adb_path": "/bin/echo",
                "auto_refresh": False,
                "timezone": "Asia/Almaty",
                "history_max_events": 100,
                "tvs": [dict(tv)],
            }
            calls = []

            def fake_telegram(_cfg, method, payload):
                calls.append((method, payload))
                return True

            def fake_adb(_cfg, *args, **_kwargs):
                if args[0] == "connect":
                    return True, "connected"
                if args[0] == "-s" and args[2] == "get-state":
                    return True, "device"
                return True, ""

            update = {
                "update_id": 7,
                "callback_query": {
                    "id": "cb-7",
                    "data": "off:tv000001",
                    "from": {"id": 123},
                    "message": {
                        "message_id": 5,
                        "chat": {"id": 123, "type": "private"},
                    },
                },
            }
            with (
                mock.patch.object(tv_bot, "CONFIG", config_path),
                mock.patch.object(tv_bot, "telegram", side_effect=fake_telegram),
                mock.patch.object(tv_bot, "is_device_reachable", return_value=True),
                mock.patch.object(tv_bot, "adb", side_effect=fake_adb),
                mock.patch.object(
                    tv_bot, "get_cached_tv_status",
                    return_value=("sleep", "Сон", "💤"),
                ),
            ):
                tv_bot.process(cfg, update)

            state = json.loads(config_path.with_name("state.json").read_text(encoding="utf-8"))
            self.assertTrue(state["tvs"]["tv000001"]["manual_sleep"])
            history = config_path.with_name("history.jsonl").read_text(encoding="utf-8")
            self.assertIn('"action": "off"', history)
            self.assertEqual(calls[0][0], "answerCallbackQuery")
            self.assertTrue(any(method == "editMessageText" for method, _ in calls))


if __name__ == "__main__":
    unittest.main()
