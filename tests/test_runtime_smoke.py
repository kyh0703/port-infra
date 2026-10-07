import importlib.util
import json
import sys
import threading
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from types import SimpleNamespace
from unittest.mock import patch

from test_runtime_fleet import fleet, registry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('runtime_smoke', ROOT / 'scripts/runtime_smoke.py')
smoke = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smoke
spec.loader.exec_module(smoke)


class HonestLocalSmokeTests(unittest.TestCase):
    def test_actual_unproven_api_gate_is_observed_but_missing_route_or_accepted_enable_is_not(self):
        for response_status in (503, 404, 200):
            with self.subTest(response_status=response_status):
                snapshot = registry()
                pool = snapshot['pools'][0]
                for name in ('firstCutoverEvidenceId', 'firstCutoverEvidenceHash', 'firstCutoverAuthorizedAt'):
                    pool[name] = None
                writes = []

                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        return

                    def do_GET(self):
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(json.dumps({'statusCode': 200, 'message': 'OK', 'data': snapshot}).encode())

                    def do_PUT(self):
                        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                        writes.append(body)
                        if response_status == 200:
                            pool.update({key: body[key] for key in ('initialAccepting', 'recoveryAccepting', 'retiring')})
                            pool['revision'] += 1
                        self.send_response(response_status)
                        self.end_headers()
                        self.wfile.write(json.dumps({'statusCode': response_status, 'message': 'OK', 'data': pool}).encode())

                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    client = fleet.FleetClient('http://127.0.0.1:' + str(server.server_port) + '/api/v1', 'unit-control-key', allow_loopback=True)
                    if response_status == 503:
                        smoke.require_unproven_enabling_blocked(client, 'a')
                        self.assertFalse(pool['initialAccepting'])
                        self.assertFalse(pool['recoveryAccepting'])
                    else:
                        with self.assertRaises(fleet.FleetError):
                            smoke.require_unproven_enabling_blocked(client, 'a')
                    self.assertEqual(len(writes), 1, 'exercise API enforcement, not only CLI precheck')
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()


class SealedKeyBootstrapObservationTests(unittest.TestCase):
    def run_key_bootstrap(self, *, fallback_after_gap=False, restored_healthy=True, changed_cluster=False):
        observation = {'sealed': False, 'restored': False, 'sealedSamples': 0,
            'restoredSamples': 0, 'restoredHealthy': 0, 'operations': []}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                if self.path != '/api/v1/health':
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b'{}')
                    return
                if observation['sealed']:
                    observation['sealedSamples'] += 1
                    if observation['sealedSamples'] == 1:
                        # A real restart transport gap is not a bootstrap verdict.
                        self.close_connection = True
                        return
                    status = 200 if fallback_after_gap else 503
                elif observation['restored']:
                    observation['restoredSamples'] += 1
                    status = 200 if restored_healthy else 503
                    if status == 200:
                        observation['restoredHealthy'] += 1
                else:
                    status = 200
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b'{}')

        with tempfile.TemporaryDirectory(prefix='port-ha-key-observation-unit-') as temporary:
            state = Path(temporary).resolve()
            owner = 'isolated-smoke-unit-owner'
            manifest = {'schemaVersion': 1, 'fixtureOnly': True, 'project': 'port-ha-fixture-smoke-unit',
                'owner': owner, 'stateDir': str(state.resolve())}
            (state / 'tls').mkdir(mode=0o700)
            for name, content in (
                ('owner.json', json.dumps(manifest).encode()),
                ('compose.env', ("RUNTIME_HA_OWNER='" + owner + "'\nRUNTIME_HA_STATE_DIR='" + str(state.resolve()) +
                    "'\nRUNTIME_HA_CONTROL_KEY='isolated-unit-control'\n").encode()),
                ('runtime.env', b"RUNTIME_HA_POOL_REPLICAS='2'\n"),
                ('bao-root.token', b'isolated-unit-bao-token'),
                ('seal.key', b'unchanged-isolated-fixture-key!!!'),
                ('tls/server.key', b'isolated-private-TLS-key-bytes-NOT-a-certificate'),
            ):
                path = state / name
                path.write_bytes(content)
                path.chmod(0o600)
            instance = smoke.Fixtures(state, runtime=True)
            original_key = (state / 'seal.key').read_bytes()
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def compose(*args, **kwargs):
                observation['operations'].append(args)
                if args == ('restart', 'openbao'):
                    observation['sealed'] = False
                    observation['restored'] = True
                return b''

            def bao(method, path, *args, **kwargs):
                if method == 'PUT' and path == '/sys/seal':
                    observation['sealed'] = True
                    return {}
                if method == 'GET' and path == '/sys/seal-status':
                    return {'initialized': True, 'sealed': observation['sealed']}
                if method == 'GET' and path == '/sys/health':
                    cluster = 'changed-unit-cluster' if changed_cluster and observation['restored'] else 'original-unit-cluster'
                    return {'cluster_id': cluster, 'initialized': True, 'sealed': observation['sealed']}
                raise AssertionError('unexpected isolated Bao boundary operation')

            real_monotonic, real_sleep = time.monotonic, time.sleep
            clock = SimpleNamespace(monotonic=lambda: real_monotonic() * 100,
                sleep=lambda seconds: real_sleep(seconds / 100))
            try:
                # This unit scope is key-bootstrap observation only. Bypass the
                # unrelated registry/admission phases without supplying fake ACKs;
                # retain real loopback API transport and the complete seal/restore
                # orchestration, with Docker/Bao isolated at their boundaries.
                with patch.object(instance, 'assert_owned_resources'), \
                        patch.object(instance, 'connections', return_value={'apiPorts': [server.server_port], 'poolAPorts': [], 'poolBPorts': []}), \
                        patch.object(instance, 'port', return_value=[server.server_port]), \
                        patch.object(instance, 'compose', side_effect=compose), \
                        patch.object(instance, 'bao', side_effect=bao), \
                        patch.object(instance, 'bootstrap_bao'), \
                        patch.object(smoke, 'load_json', return_value=None), \
                        patch.object(smoke, 'await_fleet'), \
                        patch.object(smoke, 'restore_fleet_registration'), \
                        patch.object(smoke, 'await_outage'), \
                        patch.object(smoke, 'require_unproven_enabling_blocked'), \
                        patch.object(smoke, 'set_admission'), \
                        patch.object(smoke, 'time', clock):
                    result = smoke.smoke(instance, 2, include_signal=False)
                self.assertEqual((state / 'seal.key').read_bytes(), original_key)
                return result, observation
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_late_healthy_api_after_restart_gap_is_rejected_while_bao_is_sealed(self):
        with self.assertRaises(smoke.FixtureError):
            self.run_key_bootstrap(fallback_after_gap=True)

    def test_blocked_cold_start_is_observed_for_window_then_same_key_api_recovery_is_positive(self):
        result, observation = self.run_key_bootstrap()
        self.assertGreaterEqual(observation['sealedSamples'], 2, 'do not stop on the first unavailable health sample')
        self.assertGreaterEqual(observation['restoredHealthy'], 1, 'positively observe API recovery after original Bao restoration')
        by_scenario = {item['scenario']: item for item in result}
        self.assertFalse(by_scenario['sealed-key-bootstrap']['plaintextFallback'])
        self.assertEqual(by_scenario['same-static-seal-original-fixture-data-restart']['productionSealAvailability'], 'unproven')

    def test_original_bao_restoration_without_healthy_api_does_not_report_recovery(self):
        with self.assertRaises(smoke.FixtureError):
            self.run_key_bootstrap(restored_healthy=False)

    def test_changed_original_bao_cluster_remains_rejected(self):
        with self.assertRaises(smoke.FixtureError):
            self.run_key_bootstrap(changed_cluster=True)


if __name__ == '__main__':
    unittest.main()
