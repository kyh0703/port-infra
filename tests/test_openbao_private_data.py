import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("openbao_private_data", ROOT / "scripts/openbao-private-data.py")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class Client:
    def __init__(self, unsafe=False):
        self.calls = []
        self.keys = {}
        if unsafe:
            self.keys["port-private-data"] = {"type": "chacha20-poly1305", "exportable": True, "allow_plaintext_backup": False}

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        name = path.rsplit("/", 1)[1]
        if method == "GET":
            if name not in self.keys:
                raise MODULE.ROLE.OpenBaoHTTPError(method, path, 404)
            return {"data": self.keys[name]}
        self.keys[name] = payload
        return {}

    def get_role(self, _name):
        return {"token_policies": ["api-pii-envelope"]}

    def write_policy(self, name, policy):
        self.calls.append(("policy", name, policy))


class PrivateDataTests(unittest.TestCase):
    def test_dry_run_does_not_change_keys_or_acl(self):
        client = Client()
        result = MODULE.provision(client)
        self.assertEqual(result["mode"], "dry-run")
        self.assertTrue(all(call[0] == "GET" for call in client.calls))

    def test_apply_is_idempotent_and_does_not_export_keys(self):
        client = Client()
        MODULE.provision(client, True)
        MODULE.provision(client, True)
        self.assertEqual(sum(call[0] == "POST" for call in client.calls), 2)
        self.assertIn("transit/encrypt/port-private-data", client.calls[-1][2])
        self.assertNotIn('path "transit/*"', client.calls[-1][2])

    def test_rejects_unsafe_existing_key_without_writing(self):
        client = Client(unsafe=True)
        with self.assertRaises(MODULE.ROLE.ProvisionError):
            MODULE.provision(client, True)
        self.assertTrue(all(call[0] == "GET" for call in client.calls))
