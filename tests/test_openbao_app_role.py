import importlib.util
import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "openbao-app-role.py"
SPEC = importlib.util.spec_from_file_location("openbao_app_role", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class FakeClient:
    def __init__(self, auth_mount=None, role=None):
        self.auth_mount = auth_mount
        self.role = role
        self.calls = []

    def get_approle_mount(self):
        self.calls.append(("get_approle_mount",))
        return self.auth_mount

    def write_policy(self, name, policy):
        self.calls.append(("write_policy", name, policy))

    def enable_approle(self):
        self.calls.append(("enable_approle",))
        self.auth_mount = {"type": "approle"}

    def get_role(self, name):
        self.calls.append(("get_role", name))
        return self.role

    def write_role(self, name, payload):
        self.calls.append(("write_role", name, payload))

    def read_role_id(self, name):
        self.calls.append(("read_role_id", name))
        return "role-id-for-test"

    def issue_secret_id(self, name):
        self.calls.append(("issue_secret_id", name))
        return {"secret_id": "secret-id-for-test", "secret_id_accessor": "accessor-for-test"}

    def revoke_secret_id(self, name, accessor):
        self.calls.append(("revoke_secret_id", name, accessor))


class FakeVolume:
    def __init__(self, error=None, write_error=None):
        self.error = error
        self.write_error = write_error
        self.calls = []

    def ensure_empty(self):
        self.calls.append(("ensure_empty",))
        if self.error:
            raise self.error

    def write(self, role_id, secret_id):
        self.calls.append(("write", role_id, secret_id))
        if self.write_error:
            raise self.write_error


class OpenBaoAppRoleProvisioningTests(unittest.TestCase):
    def config(self, folder, policy_file, token_file):
        return MODULE.ProvisionConfig(
            addr="https://127.0.0.1:18200",
            ca_cert=Path(folder) / "ca.crt",
            token_file=token_file,
            policy_file=policy_file,
            volume="infra_openbao_api_credentials",
            image="ghcr.io/openbao/openbao:2.6.2",
        )

    def write_inputs(self, folder):
        policy = Path(folder) / "policy.hcl"
        policy.write_text('path "secret/data/port/api/pii-envelope" { capabilities = ["read"] }\n')
        token = Path(folder) / "operator.token"
        token.write_text("operator-token-for-test\n")
        token.chmod(0o600)
        ca = Path(folder) / "ca.crt"
        ca.write_text("test-ca")
        return policy, token, ca

    def test_provision_registers_policy_role_and_persists_credentials_without_printing_them(self):
        with tempfile.TemporaryDirectory() as folder:
            policy, token, _ = self.write_inputs(folder)
            config = self.config(folder, policy, token)
            client = FakeClient()
            volume = FakeVolume()
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(io.StringIO()):
                MODULE.provision(config, client=client, volume=volume)

            self.assertEqual(volume.calls, [("ensure_empty",), ("write", "role-id-for-test", "secret-id-for-test")])
            self.assertEqual(client.calls[0], ("get_approle_mount",))
            self.assertIn(("enable_approle",), client.calls)
            self.assertIn(("write_policy", "api-pii-envelope", policy.read_text()), client.calls)
            role_call = next(call for call in client.calls if call[0] == "write_role")
            self.assertEqual(role_call[1], "api-pii-envelope")
            self.assertEqual(role_call[2]["token_policies"], ["api-pii-envelope"])
            self.assertEqual(role_call[2]["token_ttl"], "300s")
            self.assertEqual(role_call[2]["token_max_ttl"], "300s")
            self.assertEqual(role_call[2]["token_period"], "0s")
            self.assertTrue(role_call[2]["token_no_default_policy"])
            self.assertTrue(role_call[2]["bind_secret_id"])
            self.assertEqual(role_call[2]["secret_id_ttl"], "0s")
            self.assertEqual(role_call[2]["secret_id_num_uses"], 0)
            self.assertNotIn("role-id-for-test", output.getvalue())
            self.assertNotIn("secret-id-for-test", output.getvalue())

    def test_existing_auth_mount_with_wrong_type_fails_before_policy_write(self):
        with tempfile.TemporaryDirectory() as folder:
            policy, token, _ = self.write_inputs(folder)
            config = self.config(folder, policy, token)
            client = FakeClient(auth_mount={"type": "userpass"})
            volume = FakeVolume()

            with self.assertRaises(MODULE.ProvisionError):
                MODULE.provision(config, client=client, volume=volume)

            self.assertEqual(volume.calls, [("ensure_empty",)])
            self.assertEqual([call[0] for call in client.calls], ["get_approle_mount"])

    def test_nonempty_volume_fails_before_openbao_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            policy, token, _ = self.write_inputs(folder)
            config = self.config(folder, policy, token)
            client = FakeClient()
            volume = FakeVolume(MODULE.ProvisionError("credential volume is not empty"))

            with self.assertRaises(MODULE.ProvisionError):
                MODULE.provision(config, client=client, volume=volume)

            self.assertEqual(volume.calls, [("ensure_empty",)])
            self.assertEqual(client.calls, [])

    def test_existing_periodic_role_fails_before_policy_write_or_credential_issuance(self):
        with tempfile.TemporaryDirectory() as folder:
            policy, token, _ = self.write_inputs(folder)
            config = self.config(folder, policy, token)
            role = {**MODULE.desired_role_payload(), "token_period": "3600s"}
            client = FakeClient(auth_mount={"type": "approle"}, role=role)
            volume = FakeVolume()
            with self.assertRaisesRegex(MODULE.ProvisionError, "periodic tokens"):
                MODULE.provision(config, client=client, volume=volume)
            self.assertEqual(volume.calls, [("ensure_empty",)])
            self.assertNotIn("write_policy", [call[0] for call in client.calls])
            self.assertNotIn("issue_secret_id", [call[0] for call in client.calls])

    def test_existing_role_mismatch_fails_before_policy_write(self):
        with tempfile.TemporaryDirectory() as folder:
            policy, token, _ = self.write_inputs(folder)
            config = self.config(folder, policy, token)
            client = FakeClient(
                auth_mount={"type": "approle"},
                role={"token_policies": ["unexpected-policy"]},
            )
            volume = FakeVolume()

            with self.assertRaises(MODULE.ProvisionError):
                MODULE.provision(config, client=client, volume=volume)

            self.assertEqual([call[0] for call in client.calls], ["get_approle_mount", "get_role"])


    def test_failed_credential_write_revokes_new_secret_id(self):
        with tempfile.TemporaryDirectory() as folder:
            policy, token, _ = self.write_inputs(folder)
            config = self.config(folder, policy, token)
            client = FakeClient()
            volume = FakeVolume(write_error=MODULE.ProvisionError("credential write failed"))

            with self.assertRaises(MODULE.ProvisionError):
                MODULE.provision(config, client=client, volume=volume)

            self.assertIn(("revoke_secret_id", "api-pii-envelope", "accessor-for-test"), client.calls)
            self.assertNotIn("secret-id-for-test", " ".join(map(str, client.calls)))

    def test_docker_credential_write_sends_values_on_stdin_not_argv(self):
        with mock.patch.object(MODULE.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            volume = MODULE.CredentialVolume("infra_openbao_api_credentials", "ghcr.io/openbao/openbao:2.6.2")
            volume.write("role-id-for-test", "secret-id-for-test")

            command = run.call_args.args[0]
            self.assertIn("--interactive", command)
            self.assertIn("--network", command)
            self.assertIn("none", command)
            self.assertNotIn("role-id-for-test", command)
            self.assertNotIn("secret-id-for-test", command)
            self.assertEqual(run.call_args.kwargs["input"], b"role-id-for-test\nsecret-id-for-test\n")

    def test_token_file_must_be_owner_only_and_ca_must_be_explicit_https(self):
        with tempfile.TemporaryDirectory() as folder:
            policy, token, ca = self.write_inputs(folder)
            token.chmod(0o644)
            with self.assertRaises(MODULE.ProvisionError):
                MODULE.load_operator_token(token)

            with self.assertRaises(MODULE.ProvisionError):
                MODULE.parse_endpoint("http://127.0.0.1:18200", ca)

            with self.assertRaises(MODULE.ProvisionError):
                MODULE.parse_endpoint("https://127.0.0.1:18200", Path(folder) / "missing-ca.crt")


if __name__ == "__main__":
    unittest.main()
