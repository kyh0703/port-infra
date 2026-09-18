import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.yml"
ENTRYPOINT = ROOT / "openbao" / "entrypoint.sh"
STATIC_CONFIG = ROOT / "openbao" / "seal-static.hcl"


class OpenBaoStaticSealContractTests(unittest.TestCase):
    def compose_config(self, seal_mode=None):
        if shutil.which("docker") is None:
            self.skipTest("docker is unavailable")
        environment = os.environ.copy()
        environment["INTERNAL_SERVER_KEY"] = "compose-test-only-internal-key-0123456789"
        if seal_mode is None:
            environment.pop("OPENBAO_SEAL_MODE", None)
        else:
            environment["OPENBAO_SEAL_MODE"] = seal_mode
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                ".env.example",
                "-f",
                str(COMPOSE),
                "config",
                "--format",
                "json",
            ],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_compose_defaults_to_shamir_and_can_select_static(self):
        default = self.compose_config()
        static = self.compose_config("static")
        self.assertEqual(default["services"]["openbao"]["environment"]["OPENBAO_SEAL_MODE"], "shamir")
        self.assertEqual(static["services"]["openbao"]["environment"]["OPENBAO_SEAL_MODE"], "static")

    def test_compose_mounts_static_config_and_openbao_only_seal_tmpfs(self):
        config = self.compose_config("static")
        openbao = config["services"]["openbao"]
        api = config["services"]["api"]
        mounts = openbao["volumes"]
        self.assertTrue(
            any(
                mount["target"] == "/bao/config/seal-static.hcl"
                and mount["read_only"]
                for mount in mounts
            )
        )
        self.assertIn("/bao/seal-runtime", openbao["tmpfs"])
        self.assertFalse(any(mount["target"] == "/bao/seal-runtime" for mount in api["volumes"]))

    def test_invalid_mode_is_not_a_compose_contract(self):
        config = self.compose_config("invalid")
        self.assertEqual(config["services"]["openbao"]["environment"]["OPENBAO_SEAL_MODE"], "invalid")
        self.assertIn('invalid OPENBAO_SEAL_MODE', ENTRYPOINT.read_text())

    def test_static_config_contains_only_file_reference_and_no_secret_input(self):
        content = STATIC_CONFIG.read_text()
        self.assertIn('seal "static"', content)
        self.assertIn('current_key_id = "port-macos-keychain-v1"', content)
        self.assertIn('current_key    = "file:///bao/seal-runtime/current.key"', content)
        self.assertNotRegex(content, r"(?i)(OPENBAO_SEAL|key\s*=\s*[^\"\n]*[A-Za-z0-9+/]{20,})")
        self.assertNotIn("USER_PII", content)

    def test_entrypoint_is_posix_shell_and_enforces_static_key_contract(self):
        syntax = subprocess.run(["sh", "-n", str(ENTRYPOINT)], text=True, capture_output=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        content = ENTRYPOINT.read_text()
        for required in (
            'seal_mode="${OPENBAO_SEAL_MODE:-shamir}"',
            "shamir|static)",
            'seal_key="${seal_runtime_dir}/current.key"',
            'while [ ! -e "${seal_key}" ]; do',
            '[ -L "${seal_key}" ] || [ ! -f "${seal_key}" ]',
            'wc -c < "${seal_key}"',
            'chown openbao:openbao "${seal_key}"',
            'chmod 0400 "${seal_key}"',
            'set -- "$@" "-config=${static_config}"',
            'exec su-exec openbao:openbao bao "$@"',
        ):
            self.assertIn(required, content)

    def test_entrypoint_does_not_embed_seal_key_or_pass_it_as_an_argument(self):
        content = ENTRYPOINT.read_text()
        self.assertNotRegex(content, r"(?i)(OPENBAO_[A-Z_]*(KEY|TOKEN)|current_key\s*=\s*['\"][^f])")
        self.assertNotIn("cat \"${seal_key}\"", content)

    def run_entrypoint_in_openbao(self, mode, key_length=0):
        if shutil.which("docker") is None:
            self.skipTest("docker is unavailable")
        image = "ghcr.io/openbao/openbao:2.6.2"
        image_check = subprocess.run(
            ["docker", "image", "inspect", image],
            text=True,
            capture_output=True,
        )
        if image_check.returncode != 0:
            self.skipTest("OpenBao image is unavailable")
        wrapper = """\
set -eu
mkdir -p /bao/data /bao/audit /bao/tls
: > /bao/tls/ca.crt
: > /bao/tls/server.crt
: > /bao/tls/server.key
if [ "${TEST_KEY_LENGTH}" -gt 0 ]; then
  dd if=/dev/zero of=/bao/seal-runtime/current.key bs="${TEST_KEY_LENGTH}" count=1 status=none
fi
printf '%s\\n' '#!/bin/sh' \\
  'printf "key=%s\\n" "$(stat -c "%U %a" /bao/seal-runtime/current.key 2>/dev/null || printf absent)"' \\
  'printf "args="; printf "%s " "$@"; printf "\\n"' > /tmp/bao
chmod 755 /tmp/bao
PATH=/tmp:${PATH} /usr/local/bin/openbao-entrypoint server -config=/bao/config/openbao.hcl
"""
        return subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                "0:0",
                "--entrypoint",
                "sh",
                "--env",
                f"OPENBAO_SEAL_MODE={mode}",
                "--env",
                f"TEST_KEY_LENGTH={key_length}",
                "--tmpfs",
                "/bao/seal-runtime",
                "--volume",
                f"{ENTRYPOINT}:/usr/local/bin/openbao-entrypoint:ro",
                "--volume",
                f"{STATIC_CONFIG}:/bao/config/seal-static.hcl:ro",
                image,
                "-ec",
                wrapper,
            ],
            text=True,
            capture_output=True,
        )

    def test_runtime_rejects_wrong_length_sets_permissions_and_preserves_shamir(self):
        wrong_length = self.run_entrypoint_in_openbao("static", key_length=31)
        self.assertNotEqual(wrong_length.returncode, 0)
        self.assertIn("static seal key must be exactly 32 bytes", wrong_length.stderr)

        static = self.run_entrypoint_in_openbao("static", key_length=32)
        self.assertEqual(static.returncode, 0, static.stderr)
        self.assertIn("key=openbao 400", static.stdout)
        self.assertIn("-config=/bao/config/seal-static.hcl", static.stdout)

        shamir = self.run_entrypoint_in_openbao("shamir")
        self.assertEqual(shamir.returncode, 0, shamir.stderr)
        self.assertIn("key=absent", shamir.stdout)
        self.assertNotIn("seal-static.hcl", shamir.stdout)

    def test_runtime_rejects_unknown_mode_before_starting_openbao(self):
        result = self.run_entrypoint_in_openbao("invalid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid OPENBAO_SEAL_MODE", result.stderr)
        self.assertNotIn("args=", result.stdout)


if __name__ == "__main__":
    unittest.main()
