import platform
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path


class OpenBaoKeychainHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if platform.system() != "Darwin":
            raise unittest.SkipTest("macOS Keychain is only available on Darwin")
        if shutil.which("swiftc") is None or shutil.which("codesign") is None:
            raise unittest.SkipTest("swiftc and codesign are required")

    def setUp(self):
        self.service = f"com.port.infra.openbao.test.{uuid.uuid4().hex}"
        self.account = "port-macos-keychain-test-v1"
        self.temp_dir = tempfile.TemporaryDirectory(prefix="openbao-keychain-helper-")
        self.binary = Path(self.temp_dir.name) / "keychain-helper"
        source = Path(__file__).resolve().parents[1] / "openbao" / "macos" / "keychain-helper.swift"
        compile_result = subprocess.run(
            [
                "swiftc",
                "-O",
                str(source),
                "-framework",
                "Security",
                "-framework",
                "Foundation",
                "-o",
                str(self.binary),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if compile_result.returncode != 0:
            self.temp_dir.cleanup()
            self.fail(f"Swift helper compilation failed: {compile_result.stderr}")

        sign_result = subprocess.run(
            [
                "codesign",
                "--force",
                "--sign",
                "-",
                "--identifier",
                "com.port.infra.openbao.test.keychain-helper",
                str(self.binary),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if sign_result.returncode != 0:
            self._cleanup_item()
            self.temp_dir.cleanup()
            self.fail(f"Swift helper signing failed: {sign_result.stderr}")

    def tearDown(self):
        self._cleanup_item()
        self.temp_dir.cleanup()

    def _cleanup_item(self):
        security = shutil.which("security")
        if security is None:
            return
        subprocess.run(
            [security, "delete-generic-password", "-s", self.service, "-a", self.account],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )

    def _run(self, command):
        return subprocess.run(
            [str(self.binary), command, "--service", self.service, "--account", self.account],
            check=False,
            capture_output=True,
            timeout=10,
        )

    def _initialize_or_skip_locked_keychain(self):
        initialize = self._run("init")
        if initialize.returncode == 3 and initialize.stderr in {b"3:-25308\n", b"3:-25291\n"}:
            self.skipTest("login Keychain is locked or requires interaction; helper failed closed")
        self.assertEqual(initialize.returncode, 0, initialize.stderr.decode(errors="replace"))
        self.assertEqual(initialize.stdout, b"0\n")
        self.assertEqual(initialize.stderr, b"")

    def test_init_is_idempotent_and_check_does_not_emit_secret(self):
        self._initialize_or_skip_locked_keychain()

        first_key = self._run("emit")
        self.assertEqual(first_key.returncode, 0)
        self.assertEqual(len(first_key.stdout), 32)
        self.assertEqual(first_key.stderr, b"")

        second = self._run("init")
        self.assertEqual(second.returncode, 0, second.stderr.decode(errors="replace"))
        self.assertEqual(second.stdout, b"0\n")
        self.assertEqual(second.stderr, b"")

        second_key = self._run("emit")
        self.assertEqual(second_key.returncode, 0)
        self.assertEqual(second_key.stdout, first_key.stdout)
        self.assertEqual(len(second_key.stdout), 32)

        check = self._run("check")
        self.assertEqual(check.returncode, 0)
        self.assertEqual(check.stdout, b"0\n")
        self.assertEqual(check.stderr, b"")

    def test_emit_is_pipe_binary_only_and_no_status_suffix(self):
        self._initialize_or_skip_locked_keychain()

        emitted = self._run("emit")
        self.assertEqual(emitted.returncode, 0)
        self.assertEqual(len(emitted.stdout), 32)
        self.assertEqual(emitted.stderr, b"")

    def test_invalid_arguments_only_return_numeric_status(self):
        result = subprocess.run(
            [str(self.binary), "emit", "--service"],
            check=False,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"2\n")


if __name__ == "__main__":
    unittest.main()
