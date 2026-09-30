import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TokenMigrationTests(unittest.TestCase):
    def test_token_moves_to_secret_only_after_finalize(self):
        script = Path(__file__).parents[1] / "scripts" / "migrate_token.py"
        with tempfile.TemporaryDirectory() as tempdir:
            config = Path(tempdir) / "config.json"
            secret = Path(tempdir) / "secrets" / "telegram_token"
            config.write_text(
                json.dumps({"telegram_token": "private-token", "tvs": []}),
                encoding="utf-8",
            )
            subprocess.run(
                [sys.executable, str(script), "prepare", str(config), str(secret)],
                check=True,
            )
            self.assertEqual(secret.read_text(encoding="utf-8").strip(), "private-token")
            self.assertIn("telegram_token", json.loads(config.read_text(encoding="utf-8")))
            subprocess.run(
                [sys.executable, str(script), "finalize", str(config), str(secret)],
                check=True,
            )
            self.assertNotIn("telegram_token", json.loads(config.read_text(encoding="utf-8")))
            self.assertEqual(secret.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
