import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/init-internal-key.py"


class InternalKeySetupTests(unittest.TestCase):
    def run_setup(self, path):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--env-file", str(path)],
            text=True, capture_output=True,
        )

    def test_creates_private_key_without_printing_it_and_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            path.write_text("API_ENV_FILE=./custom.env\nINTERNAL_SERVER_KEY=\n")
            result = self.run_setup(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            content = path.read_text()
            key = re.search(r"^INTERNAL_SERVER_KEY=(.+)$", content, re.M).group(1)
            self.assertRegex(key, r"^[a-f0-9]{64}$")
            self.assertIn("API_ENV_FILE=./custom.env", content)
            self.assertNotIn(key, result.stdout + result.stderr)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            again = self.run_setup(path)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertEqual(path.read_text(), content)

    def test_rejects_invalid_or_duplicate_existing_keys_without_replacing_them(self):
        for content in ["INTERNAL_SERVER_KEY=short\n", "INTERNAL_SERVER_KEY=\nINTERNAL_SERVER_KEY=\n"]:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / ".env"
                path.write_text(content)
                result = self.run_setup(path)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(path.read_text(), content)


if __name__ == "__main__":
    unittest.main()
