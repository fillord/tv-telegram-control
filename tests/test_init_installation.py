import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "init_installation.py"


class InstallationInitializerTests(unittest.TestCase):
    def test_creates_isolated_customer_runtime_without_exposing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            secret = target / "secrets" / "telegram_token"
            secret.parent.mkdir()
            secret.write_text("123456:customer-secret-token-value\n", encoding="utf-8")

            command = [
                sys.executable,
                str(SCRIPT),
                "--root",
                str(target),
                "--telegram-user-id",
                "123456789",
                "--telegram-user-id",
                "987654321",
                "--tv-name",
                "Экран в зале",
                "--tv-ip",
                "192.168.10.50",
                "--tv-port",
                "5555",
                "--tv-url",
                "https://example.org/tv",
                "--tv-mac",
                "aa-bb-cc-dd-ee-ff",
                "--timezone",
                "UTC",
            ]
            result = subprocess.run(command, text=True, capture_output=True, check=False)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("customer-secret", result.stdout + result.stderr)
            config_path = target / "data" / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["allowed_user_ids"], [123456789, 987654321])
            self.assertEqual(config["timezone"], "UTC")
            self.assertEqual(config["tvs"][0]["ip"], "192.168.10.50")
            self.assertEqual(config["tvs"][0]["mac"], "AA:BB:CC:DD:EE:FF")
            self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
            self.assertEqual((target / ".env").stat().st_mode & 0o777, 0o600)

            original = config_path.read_bytes()
            repeated = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertNotEqual(repeated.returncode, 0)
            self.assertEqual(config_path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
