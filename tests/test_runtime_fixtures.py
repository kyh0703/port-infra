import importlib.util
import contextlib
import io
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('runtime_fixtures', ROOT / 'scripts/runtime_fixtures.py')
fixtures = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixtures
spec.loader.exec_module(fixtures)


@contextlib.contextmanager
def unsafe_private_file(path, corruption):
    """Replace an owned test file; foreign UID is doubled at the OS metadata boundary."""
    original = path.with_name(path.name + '.unit-original')
    path.rename(original)
    try:
        if corruption == 'symlink':
            path.symlink_to(original)
        elif corruption == 'directory':
            path.mkdir(mode=0o700)
        elif corruption == 'fifo':
            os.mkfifo(path, mode=0o600)
        elif corruption != 'missing':
            content = b'x' * 65537 if corruption == 'oversized' else original.read_bytes()
            fixtures.new_file(path, content)
            if corruption == 'public-mode':
                path.chmod(0o644)
        with contextlib.ExitStack() as stack:
            if corruption == 'foreign-owner':
                target = path.stat()
                actual_fstat = os.fstat

                def foreign_fstat(descriptor):
                    info = actual_fstat(descriptor)
                    if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                        fields = list(info)
                        fields[4] = info.st_uid + 1
                        return os.stat_result(fields)
                    return info

                stack.enter_context(patch.object(fixtures.os, 'fstat', side_effect=foreign_fstat))
            yield
    finally:
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        else:
            path.unlink(missing_ok=True)
        original.rename(path)


class FixtureOwnershipTests(unittest.TestCase):
    def test_production_compose_names_and_repository_state_are_rejected(self):
        for project in ('infra', 'port-ha-verify-parent', 'runtime-ha'):
            with self.subTest(project=project), self.assertRaises(fixtures.FixtureError):
                fixtures.validate_project(project)
        with self.assertRaises(fixtures.FixtureError):
            fixtures.validate_state_path(ROOT / 'data' / 'runtime-fixture')
        fixtures.validate_project('port-ha-fixture-unittest')

    def test_missing_or_foreign_ownership_labels_never_authorize_deletion(self):
        for labels in ({}, {'io.port.runtime-ha.fixture': 'true'}, {'io.port.runtime-ha.fixture': 'true', 'io.port.runtime-ha.owner': 'someone-else'}):
            with self.subTest(labels=labels), self.assertRaises(fixtures.FixtureError):
                fixtures.require_owned_labels(labels, 'this-session')
        fixtures.require_owned_labels({'io.port.runtime-ha.fixture': 'true', 'io.port.runtime-ha.owner': 'this-session'}, 'this-session')

    def test_prepare_refuses_existing_state_without_replacing_any_bytes(self):
        with tempfile.TemporaryDirectory(prefix='port-ha-state-unittest-') as folder:
            state = Path(folder)
            sentinel = state / 'seal.key'
            sentinel.write_bytes(b'original-unrelated-file')
            with self.assertRaises(fixtures.FixtureError):
                fixtures.prepare(state, 'port-ha-fixture-unittest')
            self.assertEqual(sentinel.read_bytes(), b'original-unrelated-file')


class FixtureMutationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='port-ha-mutation-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.state = (Path(self.temporary.name) / 'state').resolve()
        self.state.mkdir(mode=0o700)
        self.owner = 'isolated-unit-owner'
        self.project = 'port-ha-fixture-mutation-unit'
        self.manifest = {'schemaVersion': 1, 'fixtureOnly': True, 'project': self.project,
            'owner': self.owner, 'stateDir': str(self.state)}
        fixtures.new_file(self.state / 'owner.json', json.dumps(self.manifest).encode())
        fixtures.new_file(self.state / 'compose.env', (
            "RUNTIME_HA_OWNER='" + self.owner + "'\nRUNTIME_HA_STATE_DIR='" + str(self.state) + "'\n"
        ).encode())
        fixtures.new_file(self.state / 'platform.env', b"RUNTIME_HA_EXPECTED_CLUSTER_ID='unit-original-cluster'\n")
        fixtures.new_file(self.state / 'runtime.env', b"RUNTIME_HA_POOL_REPLICAS='2'\n")
        fixtures.new_file(self.state / 'seal.key', bytes(range(32)), 0o400)
        (self.state / 'tls').mkdir(mode=0o700)
        fixtures.new_file(self.state / 'tls/server.key', b'isolated-unit-private-tls-key', 0o600)
        fixtures.new_file(self.state / 'bao-root.token', b'isolated-unit-token-never-log', 0o600)
        self.bao_calls = []
        environment = patch.dict(os.environ, {'DOCKER_HOST': '', 'DOCKER_CONTEXT': 'isolated-unit-local'})
        environment.start()
        self.addCleanup(environment.stop)
        self.instance = fixtures.Fixtures(self.state, runtime=True)
        self.commands = []
        self.mutations = []
        self.labels = {fixtures.LABEL: 'true', fixtures.OWNER_LABEL: self.owner}
        self.endpoint = 'unix:///isolated-unit-docker.sock'
        self.runner = patch.object(fixtures, 'run', side_effect=self.docker_boundary)
        self.runner.start()
        self.addCleanup(self.runner.stop)

    def docker_boundary(self, command, **kwargs):
        self.commands.append(command)
        if command[1:3] == ['context', 'inspect']:
            return json.dumps([{'Endpoints': {'docker': {'Host': self.endpoint}}}]).encode()
        if command[1] == 'ps':
            return b'owned-unit-container\n'
        if command[1] == 'inspect':
            return json.dumps([{'Config': {'Labels': self.labels}}]).encode()
        if command[1:3] in (['volume', 'ls'], ['network', 'ls']):
            return b''
        if command[1] == 'compose' and 'ps' in command:
            return b'owned-unit-container\n'
        self.mutations.append(command)
        return b''

    def assert_restart_blocked(self):
        with self.assertRaises(fixtures.FixtureError):
            self.instance.compose('restart', 'api')
        self.assertEqual(self.mutations, [], 'unsafe state must fail before Docker mutation')

    def test_only_nonserving_docker_states_may_omit_binding_during_seal_observation(self):
        # Docker boundary double; native sealed API exits were observed separately.
        states = (
            ({'Running': False, 'Restarting': False, 'Pid': 0}, True),
            ({'Running': True, 'Restarting': True, 'Pid': 0}, True),
            ({'Running': True, 'Restarting': True, 'Pid': 42}, False),
            ({'Running': True, 'Restarting': False, 'Pid': 0}, False),
            ({'Running': True, 'Restarting': True, 'Pid': False}, False),
            ({}, False),
        )
        for state, nonserving in states:
            inspected = [{'State': state, 'NetworkSettings': {'Ports': {}}}]
            with self.subTest(state=state), \
                    patch.object(self.instance, 'containers', return_value=['owned-unit-container']), \
                    patch.object(self.instance, 'docker', return_value=json.dumps(inspected).encode()):
                with self.assertRaises(fixtures.FixtureError):
                    self.instance.port('api', 8000)
                if nonserving:
                    self.assertEqual(self.instance.port('api', 8000, allow_stopped=True), [])
                else:
                    with self.assertRaises(fixtures.FixtureError):
                        self.instance.port('api', 8000, allow_stopped=True)

    def test_restart_rechecks_state_directory_privacy_after_construction(self):
        self.state.chmod(0o770)
        self.assert_restart_blocked()

    def test_restart_rejects_replaced_symlink_or_non_directory_state(self):
        original = self.state.with_name('original-state')
        self.state.rename(original)
        self.state.symlink_to(original, target_is_directory=True)
        self.assert_restart_blocked()
        self.state.unlink()
        self.state.write_bytes(b'not-a-directory')
        self.assert_restart_blocked()

    def test_restart_rechecks_state_directory_owner(self):
        with patch.object(fixtures.os, 'getuid', return_value=os.getuid() + 1):
            self.assert_restart_blocked()

    def test_restart_rechecks_every_consumed_env_type_owner_and_privacy(self):
        for name in ('compose.env', 'platform.env', 'runtime.env'):
            with self.subTest(name=name):
                path = self.state / name
                content = path.read_bytes()
                path.chmod(0o640)
                self.assert_restart_blocked()
                path.chmod(0o600)
                path.unlink()
                path.mkdir(mode=0o700)
                self.assert_restart_blocked()
                path.rmdir()
                fixtures.new_file(path, content)
                actual_fstat = os.fstat
                file_info = path.stat()

                def foreign_env_owner(descriptor):
                    info = actual_fstat(descriptor)
                    if (info.st_dev, info.st_ino) == (file_info.st_dev, file_info.st_ino):
                        fields = list(info)
                        fields[4] = info.st_uid + 1
                        return os.stat_result(fields)
                    return info

                with patch.object(fixtures.os, 'fstat', side_effect=foreign_env_owner):
                    self.assert_restart_blocked()

    def test_restart_rejects_every_consumed_env_symlink(self):
        for name in ('compose.env', 'platform.env', 'runtime.env'):
            with self.subTest(name=name):
                path = self.state / name
                original = path.with_name(name + '.original')
                path.rename(original)
                path.symlink_to(original)
                self.assert_restart_blocked()
                path.unlink()
                original.rename(path)

    def test_restart_binds_effective_env_owner_and_state_to_manifest(self):
        for name in ('compose.env', 'platform.env', 'runtime.env'):
            path = self.state / name
            original = path.read_bytes()
            for key, value in (('RUNTIME_HA_OWNER', 'foreign-owner'), ('RUNTIME_HA_STATE_DIR', str(self.state.parent / 'foreign-state'))):
                with self.subTest(name=name, key=key):
                    path.write_bytes(original + (key + "='" + value + "'\n").encode())
                    self.assert_restart_blocked()
                    path.write_bytes(original)

    def test_restart_rejects_manifest_owner_change_after_construction(self):
        changed = {**self.manifest, 'owner': 'foreign-owner'}
        (self.state / 'owner.json').write_text(json.dumps(changed))
        self.assert_restart_blocked()

    def test_all_mutating_entry_points_recheck_local_context_and_labels(self):
        operations = (
            ('compose', ('up', '--detach', 'api')), ('compose', ('start', 'api')),
            ('compose', ('stop', 'api')), ('compose', ('restart', 'api')),
            ('compose', ('pause', 'api')), ('compose', ('unpause', 'api')),
            ('compose', ('rm', '--force', 'api')), ('compose', ('down', '--volumes')),
            ('compose', ('exec', '-T', 'redis', 'redis-cli', 'FLUSHALL', 'SYNC')),
            ('docker', ('kill', '--signal', 'TERM', 'owned-unit-container')),
            ('docker', ('rm', '--force', 'owned-unit-container')),
            ('docker', ('volume', 'rm', self.project + '_postgres_data')),
            ('docker', ('exec', 'owned-unit-container', 'touch', '/unit-mutation')),
        )
        for method, args in operations:
            with self.subTest(method=method, args=args, prerequisite='labels'):
                self.labels = {}
                with self.assertRaises(fixtures.FixtureError):
                    getattr(self.instance, method)(*args)
                self.assertEqual(self.mutations, [])
            with self.subTest(method=method, args=args, prerequisite='context'):
                self.labels = {fixtures.LABEL: 'true', fixtures.OWNER_LABEL: self.owner}
                self.endpoint = 'tcp://remote-unit.invalid:2375'
                with self.assertRaises(fixtures.FixtureError):
                    getattr(self.instance, method)(*args)
                self.assertEqual(self.mutations, [])
                self.endpoint = 'unix:///isolated-unit-docker.sock'

    def test_down_remove_owned_volumes_command_rejects_changed_state_before_mutation(self):
        for corruption in ('directory-mode', 'env-symlink', 'env-owner', 'env-state'):
            with self.subTest(corruption=corruption):
                path = self.state / 'compose.env'
                original = path.read_bytes()
                if corruption == 'directory-mode':
                    self.state.chmod(0o770)
                elif corruption == 'env-symlink':
                    path.rename(self.state / 'compose.env.original')
                    path.symlink_to(self.state / 'compose.env.original')
                else:
                    key = 'RUNTIME_HA_OWNER' if corruption == 'env-owner' else 'RUNTIME_HA_STATE_DIR'
                    path.write_bytes(original + (key + "='foreign'\n").encode())
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    result = fixtures.main(['--state-dir', str(self.state), 'down', '--remove-owned-volumes'])
                self.assertEqual(result, 2)
                self.assertEqual(self.mutations, [], 'down --volumes must enforce file prerequisites')
                self.state.chmod(0o700)
                if path.is_symlink():
                    path.unlink()
                    (self.state / 'compose.env.original').rename(path)
                else:
                    path.write_bytes(original)

    def test_read_only_operations_do_not_recurse_into_mutation_assertion(self):
        with patch.object(self.instance, 'assert_owned_resources', side_effect=AssertionError('read-only recursion')):
            self.instance.docker('inspect', 'owned-unit-container')
            self.instance.compose('ps', '--all', '--quiet', 'api')
        self.assertEqual(self.mutations, [])

    def test_private_owned_state_still_allows_restart_and_owned_volume_removal(self):
        self.instance.compose('restart', 'api')
        with contextlib.redirect_stdout(io.StringIO()):
            result = fixtures.main(['--state-dir', str(self.state), 'down', '--remove-owned-volumes'])
        self.assertEqual(result, 0)
        self.assertEqual(len(self.mutations), 2)
        self.assertEqual(self.mutations[0][-2:], ['restart', 'api'])
        self.assertEqual(self.mutations[1][-3:], ['down', '--remove-orphans', '--volumes'])

    def bao_boundary(self, method, path, payload=None, token=None, *, timeout=None):
        """External Bao double; these tests prove file-consumption safety, not native Bao."""
        self.bao_calls.append((method, path, token))
        if path == '/sys/init':
            return {'initialized': True}
        if path == '/sys/health':
            return {'initialized': True, 'sealed': False, 'cluster_id': 'unit-original-cluster'}
        if path == '/transit/encrypt/port-pii-kek':
            return {'data': {'batch_results': [{'ciphertext': 'isolated-unit-ciphertext'} for _ in range(3)]}}
        if path == '/auth/approle/role/api-pii-envelope/role-id':
            return {'data': {'role_id': 'isolated-unit-role'}}
        if path == '/auth/approle/role/api-pii-envelope/secret-id':
            return {'data': {'secret_id': 'isolated-unit-secret'}}
        return {}

    def test_bootstrap_and_seal_reject_unsafe_token_before_authenticated_bao_consumption(self):
        for consumer in ('bootstrap', 'seal'):
            for corruption in ('symlink', 'public-mode', 'foreign-owner', 'directory', 'oversized'):
                with self.subTest(consumer=consumer, corruption=corruption):
                    self.bao_calls.clear()
                    self.mutations.clear()
                    output = io.StringIO()
                    error = io.StringIO()
                    try:
                        with unsafe_private_file(self.state / 'bao-root.token', corruption), \
                                patch.object(fixtures.Fixtures, 'bao', side_effect=self.bao_boundary), \
                                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                            if consumer == 'bootstrap':
                                with self.assertRaises(fixtures.FixtureError):
                                    self.instance.bootstrap_bao()
                            else:
                                result = fixtures.main(['--state-dir', str(self.state), 'bao-seal'])
                                self.assertEqual(result, 2)
                                self.assertEqual(json.loads(error.getvalue())['status'], 'blocked')
                        self.assertFalse(any(method != 'GET' or token is not None for method, _, token in self.bao_calls),
                            'unsafe token must not reach an authenticated Bao request or mutation')
                        self.assertEqual(self.mutations, [], 'unsafe token must not publish fixture credentials')
                        self.assertNotIn('isolated-unit-token-never-log', output.getvalue() + error.getvalue())
                    finally:
                        (self.state / 'bao-bootstrap.json').unlink(missing_ok=True)

    def test_compose_rejects_unsafe_secret_bind_inputs_before_docker_consumes_them(self):
        for name in ('seal.key', 'tls/server.key'):
            for corruption in ('symlink', 'public-mode', 'foreign-owner', 'directory', 'fifo', 'missing'):
                with self.subTest(name=name, corruption=corruption):
                    self.commands.clear()
                    self.mutations.clear()
                    with unsafe_private_file(self.state / name, corruption):
                        with self.assertRaises(fixtures.FixtureError):
                            self.instance.compose('up', '--detach', 'openbao')
                    self.assertEqual(self.mutations, [], 'invalid bind inputs must block before Compose mounting')

    def test_compose_rejects_symlinked_tls_parent_before_private_key_mount(self):
        original = self.state / 'tls.unit-original'
        (self.state / 'tls').rename(original)
        (self.state / 'tls').symlink_to(original, target_is_directory=True)
        try:
            with self.assertRaises(fixtures.FixtureError):
                self.instance.compose('up', '--detach', 'openbao')
            self.assertEqual(self.mutations, [])
        finally:
            (self.state / 'tls').unlink()
            original.rename(self.state / 'tls')

    def test_healthy_private_token_bootstraps_and_seals_without_replacing_owned_keys(self):
        original_seal = (self.state / 'seal.key').read_bytes()
        original_tls = (self.state / 'tls/server.key').read_bytes()
        output = io.StringIO()
        with patch.object(fixtures.Fixtures, 'bao', side_effect=self.bao_boundary), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            self.instance.bootstrap_bao()
            result = fixtures.main(['--state-dir', str(self.state), 'bao-seal'])
        self.assertEqual(result, 0)
        self.assertTrue((self.state / 'bao-bootstrap.json').is_file())
        self.assertIn(('PUT', '/sys/seal', 'isolated-unit-token-never-log'), self.bao_calls)
        self.assertTrue(any(method == 'POST' and token == 'isolated-unit-token-never-log'
            for method, _, token in self.bao_calls))
        self.assertEqual((self.state / 'seal.key').read_bytes(), original_seal)
        self.assertEqual((self.state / 'tls/server.key').read_bytes(), original_tls)
        self.assertEqual((self.state / 'bao-root.token').read_bytes(), b'isolated-unit-token-never-log')
        self.assertNotIn('isolated-unit-token-never-log', output.getvalue())


    def test_expired_explicit_bao_budget_never_opens_transport_or_uses_init_default(self):
        for timeout in (0, -1):
            with self.subTest(timeout=timeout):
                with patch.object(self.instance, 'port', side_effect=AssertionError('expired Bao request reached Docker')), \
                        patch.object(fixtures.http.client, 'HTTPSConnection',
                            side_effect=AssertionError('expired Bao request opened HTTP')):
                    with self.assertRaises(fixtures.FixtureError):
                        self.instance.bao('POST', '/sys/init', {}, timeout=timeout)

    def test_bootstrap_total_budget_rejects_late_health_without_authenticated_bao_mutation(self):
        clock = [0.0]
        calls = []

        def response_after_budget(method, path, payload=None, token=None, **kwargs):
            calls.append((method, path, token))
            if path == '/sys/init':
                clock[0] = 0.75
                return {'initialized': True}
            if path == '/sys/health':
                clock[0] = 1.1
                return {'initialized': True, 'sealed': False, 'cluster_id': 'unit-original-cluster'}
            self.fail('expired bootstrap performed an authenticated mutation')

        fixtures.new_file(self.state / 'bao-bootstrap.json',
            b'{"fixtureOnly":true,"clusterId":"unit-original-cluster"}')
        with patch.object(fixtures.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(self.instance, 'bao', side_effect=response_after_budget):
            with self.assertRaises(fixtures.FixtureError):
                self.instance.bootstrap_bao(timeout=1)
        self.assertEqual(calls, [('GET', '/sys/init', None), ('GET', '/sys/health', None)])
        self.assertEqual(self.mutations, [], 'late initialized/unsealed response is not bounded restoration proof')

    def test_late_ownership_check_cannot_start_compose_or_docker_mutation_after_total_budget(self):
        for method, arguments in (
            ('compose', ('restart', 'api')),
            ('docker', ('kill', '--signal', 'TERM', 'owned-unit-container')),
        ):
            with self.subTest(method=method):
                clock = [0.0]
                self.commands.clear()
                self.mutations.clear()

                def late_context(command, **kwargs):
                    result = self.docker_boundary(command, **kwargs)
                    if command[1:3] == ['context', 'inspect']:
                        clock[0] = 1.1
                    return result

                with patch.object(fixtures.time, 'monotonic', side_effect=lambda: clock[0]), \
                        patch.object(fixtures, 'run', side_effect=late_context):
                    with self.assertRaises(fixtures.FixtureError):
                        getattr(self.instance, method)(*arguments, timeout=1)
                self.assertEqual(self.mutations, [],
                    'nested context/ownership time is part of the mutation caller total budget')

    def test_expired_bao_port_discovery_stops_before_further_docker_or_http_activity(self):
        clock = [0.0]

        def late_discovery(command, **kwargs):
            result = self.docker_boundary(command, **kwargs)
            if command[1] == 'compose' and 'ps' in command:
                clock[0] = 1.1
            if command[1] == 'inspect':
                return json.dumps([{'Config': {'Labels': self.labels}, 'NetworkSettings': {
                    'Ports': {'8200/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '18200'}]},
                }}]).encode()
            return result

        with patch.object(fixtures.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(fixtures, 'run', side_effect=late_discovery), \
                patch.object(fixtures.ssl, 'create_default_context', return_value=object()), \
                patch.object(fixtures.http.client, 'HTTPSConnection',
                    side_effect=AssertionError('expired port discovery opened HTTP')):
            with self.assertRaises(fixtures.FixtureError):
                self.instance.bao('GET', '/sys/health', timeout=1)
        self.assertEqual([command[1] for command in self.commands], ['compose'],
            'late Compose port discovery must not trigger a fresh Docker inspect')
        self.assertEqual(self.mutations, [])


class FixtureInitializationTransportTests(unittest.TestCase):
    def test_committed_init_response_after_raft_election_is_not_lost_to_five_second_read_budget(self):
        with tempfile.TemporaryDirectory(prefix='port-ha-init-https-unit-') as temporary:
            state = Path(temporary)
            result = subprocess.run(
                ['bash', str(ROOT / 'scripts/openbao-tls.sh')],
                env={**os.environ, 'OPENBAO_TLS_DIR': str(state / 'tls')},
                capture_output=True, timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 0, 'isolated TLS prerequisite failed')
            requests = []
            response_delay = threading.Event()

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    return

                def do_POST(self):
                    requests.append(self.path)
                    self.rfile.read(int(self.headers['Content-Length']))
                    # Real transport remains open across a bounded election delay,
                    # not an engine timer fake or a retry of accepted initialization.
                    response_delay.wait(6)
                    self.send_response(200)
                    self.end_headers()
                    try:
                        self.wfile.write(b'{\"root_token\":\"isolated-unit-response-only\"}')
                    except (BrokenPipeError, ssl.SSLEOFError):
                        pass

            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(state / 'tls/server.crt', state / 'tls/server.key')
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            instance = fixtures.Fixtures.__new__(fixtures.Fixtures)
            instance.state = state
            try:
                with patch.object(instance, 'port', return_value=[server.server_port]):
                    value = instance.bao('POST', '/sys/init', {'recovery_shares': 1, 'recovery_threshold': 1})
                self.assertEqual(value['root_token'], 'isolated-unit-response-only')
                self.assertEqual(requests, ['/v1/sys/init'], 'never retry accepted initialization')
            finally:
                response_delay.set()
                server.shutdown()
                server.server_close()
                thread.join()

    def test_uncertain_initialized_fixture_never_posts_init_again_or_substitutes_keys(self):
        with tempfile.TemporaryDirectory(prefix='port-ha-init-unknown-unit-') as temporary:
            state = Path(temporary)
            (state / 'bao-init-attempted.json').write_text('{\"fixtureOnly\":true}')
            key = state / 'seal.key'
            key.write_bytes(b'unchanged-owned-fixture-key-only!')
            instance = fixtures.Fixtures.__new__(fixtures.Fixtures)
            instance.state = state
            calls = []

            def observed(method, path, *args):
                calls.append((method, path))
                return {'initialized': True}

            with patch.object(instance, 'assert_owned_resources'), patch.object(instance, 'bao', side_effect=observed):
                with self.assertRaises(fixtures.FixtureError):
                    instance.bootstrap_bao()
            self.assertEqual(calls, [('GET', '/sys/init')])
            self.assertEqual(key.read_bytes(), b'unchanged-owned-fixture-key-only!')
            self.assertFalse((state / 'bao-root.token').exists())


class FixturePublicationBoundaryTests(unittest.TestCase):
    """Real configuration and image-inventory parsing; Docker is a process double."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='port-ha-publication-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()
        self.state = self.folder / 'state'
        self.state.mkdir(mode=0o700)
        owner = 'isolated-publication-unit-owner'
        fixtures.new_file(self.state / 'owner.json', json.dumps({
            'schemaVersion': 1, 'fixtureOnly': True, 'project': 'port-ha-fixture-publication-unit',
            'owner': owner, 'stateDir': str(self.state),
        }).encode())
        fixtures.new_file(self.state / 'compose.env', (
            "RUNTIME_HA_OWNER='" + owner + "'\nRUNTIME_HA_STATE_DIR='" + str(self.state) + "'\n"
        ).encode())
        fixtures.new_file(self.state / 'seal.key', bytes(range(32)), 0o400)
        (self.state / 'tls').mkdir(mode=0o700)
        fixtures.new_file(self.state / 'tls/server.key', b'isolated-unit-private-tls-key', 0o600)
        environment = patch.dict(os.environ, {'DOCKER_HOST': '', 'DOCKER_CONTEXT': 'isolated-unit-local'})
        environment.start()
        self.addCleanup(environment.stop)
        self.instance = fixtures.Fixtures(self.state)
        self.a = {
            'protocolRevision': 'runtime-recovery-v1', 'buildId': 'isolated-unit-a',
            'imageDigest': 'sha256:' + '1' * 64, 'compatibilityFingerprint': 'a' * 64,
            'sdkPatchDigest': '2' * 64, 'builtinInventoryDigest': '3' * 64,
            'modelCacheSelectionDigest': '4' * 64, 'checkpointCodec': 'port-runtime-checkpoint-v1',
        }
        self.b = {**self.a, 'buildId': 'isolated-unit-b', 'imageDigest': 'sha256:' + '5' * 64}
        for label, inventory in (('a', self.a), ('b', self.b)):
            inventory['imageReference'] = 'port-runtime-ha-unit-' + label + '@' + inventory['imageDigest']
        self.actual_a = dict(self.a)
        self.actual_b = dict(self.b)
        self.a_path = self.folder / 'input-a.json'
        self.b_path = self.folder / 'input-b.json'
        self.a_path.write_text(json.dumps(self.a))
        self.b_path.write_text(json.dumps(self.b))
        self.publications = tuple(self.state / name for name in
            ('worker-a.inventory.json', 'worker-b.inventory.json', 'runtime.env'))
        self.inspection_publications = []
        self.fail_b_inspection = False
        process = patch.object(subprocess, 'run', side_effect=self.docker_process)
        process.start()
        self.addCleanup(process.stop)

    def docker_process(self, command, **kwargs):
        self.assertEqual(command[0], 'docker')
        if command[1:3] == ['context', 'inspect']:
            data = [{'Endpoints': {'docker': {'Host': 'unix:///isolated-unit-docker.sock'}}}]
        elif command[1:3] == ['image', 'inspect']:
            reference = command[-1]
            inventories = {inventory['imageReference']: inventory for inventory in (self.actual_a, self.actual_b)}
            if reference in inventories:
                inventory = inventories[reference]
                data = [{'Id': inventory['imageDigest'], 'RepoDigests': [reference]}]
            else:
                self.assertIn(reference, ('port-runtime-ha-unit-api', 'port-runtime-ha-unit-migrator'))
                data = [{'Id': 'sha256:' + '6' * 64, 'RepoDigests': []}]
        elif command[1] == 'run':
            inventory = self.actual_a if command[-2] == self.actual_a['imageDigest'] else self.actual_b
            self.inspection_publications.append((inventory['buildId'],
                tuple(path.name for path in self.publications if path.exists())))
            if inventory is self.actual_b and self.fail_b_inspection:
                return subprocess.CompletedProcess(command, 1, b'', b'isolated-unit-inspection-failure')
            data = inventory
        else:
            self.fail('unexpected process outside the isolated image-inventory boundary')
        return subprocess.CompletedProcess(command, 0, json.dumps(data).encode(), b'')

    def configure(self):
        self.instance.configure_runtime('port-runtime-ha-unit-api', 'port-runtime-ha-unit-migrator',
            self.a_path, self.b_path, 2)

    def test_compatible_distinct_images_are_both_inspected_before_any_pin_is_published(self):
        self.configure()
        self.assertEqual(self.inspection_publications, [('isolated-unit-a', ()), ('isolated-unit-b', ())],
            'even compatible A must stay unpublished until the actual B inventory is verified')
        for label, inventory in (('a', self.actual_a), ('b', self.actual_b)):
            pinned = self.state / ('worker-' + label + '.inventory.json')
            self.assertEqual(fixtures.read_owned_json(pinned),
                {**inventory, 'imageId': inventory['imageDigest']})
            self.assertEqual(pinned.stat().st_mode & 0o077, 0)
        values = fixtures.read_owned_env(self.state / 'runtime.env')
        self.assertEqual(values['RUNTIME_HA_POOL_REPLICAS'], '2')
        self.assertEqual(values['RUNTIME_HA_WORKER_A_IMAGE'], self.actual_a['imageReference'])
        self.assertEqual(values['RUNTIME_HA_WORKER_B_IMAGE'], self.actual_b['imageReference'])
        self.assertTrue(self.instance.runtime)

    def test_incompatible_or_unsupported_b_inventory_blocks_before_every_permanent_pin(self):
        for field, value in (
            ('compatibilityFingerprint', 'b' * 64), ('sdkPatchDigest', 'b' * 64),
            ('builtinInventoryDigest', 'b' * 64), ('checkpointCodec', 'port-runtime-checkpoint-v2'),
            ('protocolRevision', 'runtime-recovery-v2'),
        ):
            with self.subTest(field=field):
                self.b[field] = value
                self.actual_b[field] = value
                self.b_path.write_text(json.dumps(self.b))
                try:
                    with self.assertRaises(fixtures.FixtureError):
                        self.configure()
                    self.assertFalse(any(path.exists() for path in self.publications),
                        'individually pinned A cannot escape a rejected A/B cohort')
                    self.assertFalse(self.instance.runtime)
                finally:
                    self.b[field] = self.a[field]
                    self.actual_b[field] = self.a[field]
                    self.b_path.write_text(json.dumps(self.b))
                    for path in self.publications:
                        path.unlink(missing_ok=True)

    def test_failed_actual_b_inspection_does_not_leave_an_a_pin(self):
        self.fail_b_inspection = True
        with self.assertRaises(fixtures.FixtureError):
            self.configure()
        self.assertFalse(any(path.exists() for path in self.publications),
            'failed real-image readback must not leave a partially published fleet')
        self.assertFalse(self.instance.runtime)

    def test_actual_b_mismatch_with_recorded_inventory_does_not_leave_an_a_pin(self):
        self.actual_b['sdkPatchDigest'] = 'e' * 64
        with self.assertRaises(fixtures.FixtureError):
            self.configure()
        self.assertFalse(any(path.exists() for path in self.publications),
            'an individually invalid B must block publication of both worker pins')
        self.assertFalse(self.instance.runtime)


if __name__ == '__main__':
    unittest.main()
