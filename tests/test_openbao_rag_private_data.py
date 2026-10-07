import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("openbao_rag_private_data", ROOT / "scripts/openbao-rag-private-data.py")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class Client:
    def __init__(self):
        self.calls = []
        self.keys = {}
        self.role = None
    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        name = path.rsplit('/', 1)[1]
        if method == 'GET':
            if name not in self.keys:
                raise MODULE.ROLE.OpenBaoHTTPError(method, path, 404)
            return {'data': self.keys[name]}
        self.keys[name] = payload
        return {}
    def get_approle_mount(self): return {'type': 'approle'}
    def get_role(self, _name): return self.role
    def write_policy(self, name, policy): self.calls.append(('policy', name, policy))
    def write_role(self, name, role): self.calls.append(('role', name, role)); self.role = role
    def read_role_id(self, _name): return 'synthetic-role-id'
    def issue_secret_id(self, _name): return {'secret_id': 'synthetic-secret-id', 'secret_id_accessor': 'synthetic-accessor'}
    def revoke_secret_id(self, name, accessor): self.calls.append(('revoke', name, accessor))


class Volume:
    def __init__(self, fail=False): self.calls=[]; self.fail=fail
    def ensure_empty(self): self.calls.append(('empty',))
    def write(self, role, secret):
        self.calls.append(('write', role, secret))
        if self.fail: raise OSError('synthetic write failure')


class RagPrivateDataTests(unittest.TestCase):
    def test_dry_run_is_read_only(self):
        client=Client()
        self.assertEqual(MODULE.provision(client)['mode'], 'dry-run')
        self.assertTrue(all(call[0]=='GET' for call in client.calls))
    def test_role_cannot_read_api_keys_and_lookup_key_is_32_bytes(self):
        client=Client(); volume=Volume()
        MODULE.provision(client, volume, True)
        policy=next(call[2] for call in client.calls if call[0]=='policy')
        self.assertNotIn('port-pii-kek', policy)
        self.assertNotIn('port-private-data"', policy)
        self.assertIn('port-rag-private-data', policy)
        self.assertEqual(client.keys['port-rag-private-lookup']['key_size'],32)
        self.assertEqual(client.role['token_policies'],['rag-private-data'])
        self.assertTrue(client.role['token_no_default_policy'])
    def test_volume_failure_revokes_only_the_new_secret(self):
        client=Client()
        with self.assertRaises(MODULE.ROLE.ProvisionError): MODULE.provision(client,Volume(True),True)
        self.assertIn(('revoke','rag-private-data','synthetic-accessor'),client.calls)
