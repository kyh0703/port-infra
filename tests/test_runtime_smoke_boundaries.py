import copy
import json
import tempfile
import unittest
from collections import deque
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from test_runtime_fleet import fleet, isolated_inventory, registry
from test_runtime_smoke import smoke


class SmokeAcceptanceClock:
    """Orchestrator elapsed time advances at action/response delivery, not SDK timers."""

    def __init__(self):
        self.elapsed = 0.0

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


class SmokeHTTPResponse:
    def __init__(self, status, body):
        self.status = status
        self.raw = json.dumps(body).encode()

    def read(self, limit):
        return self.raw[:limit]

    def close(self):
        pass


class SmokeHTTPConnection:
    def __init__(self, boundary, port, timeout):
        self.boundary = boundary
        self.port = port
        self.timeout = timeout
        self.sock = None

    def request(self, method, path, body=None, headers=None):
        self.method, self.path = method, path
        self.body = json.loads(body) if body is not None else None

    def getresponse(self):
        return self.boundary.response(self.port, self.method, self.path, self.body, self.timeout)

    def close(self):
        pass


class SmokeDomainBoundary:
    """Registry/health DTO and Docker/Bao boundary doubles, never native/Cloud proof.

    The production smoke predicates, HTTP decoding, admission CAS/ACK wait and
    orchestration remain real. PUT changes desired state only; separate queued
    heartbeat observations can be withheld from an exact owned incarnation.
    """

    API_PORT = 18000
    A_PORTS = [18101, 18102]
    B_PORTS = [18201, 18202]

    def __init__(self):
        self.api_port = self.API_PORT
        self.api_host_ports = {self.API_PORT}
        self.remap_api_ports = False
        self.sealed_api_fallback = False
        self.observed_sealed_fallback = False
        self.signal_exit_code = 0
        self.stack = ExitStack()
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='port-ha-smoke-boundary-unit-'))
        self.state = Path(directory).resolve()
        (self.state / 'tls').mkdir(mode=0o700)
        self.owner = 'isolated-smoke-boundary-unit-owner'
        manifest = {'schemaVersion': 1, 'fixtureOnly': True, 'project': 'port-ha-fixture-boundary-unit',
            'owner': self.owner, 'stateDir': str(self.state)}
        contents = {
            'owner.json': json.dumps(manifest).encode(),
            'compose.env': ("RUNTIME_HA_OWNER='" + self.owner + "'\nRUNTIME_HA_STATE_DIR='" + str(self.state) +
                "'\nRUNTIME_HA_CONTROL_KEY='isolated-unit-control-key'\n").encode(),
            'runtime.env': b"RUNTIME_HA_POOL_REPLICAS='2'\n",
            'bao-root.token': b'isolated-unit-token-NOT-a-real-credential',
            'seal.key': b'isolated-unit-seal-key-bytes-only',
            'tls/server.key': b'isolated-private-TLS-key-bytes-NOT-a-certificate',
        }
        self.builds = {}
        for label, digest in (('a', '1'), ('b', '5')):
            build = {**isolated_inventory(), 'buildId': 'isolated-unit-' + label,
                'imageDigest': 'sha256:' + digest * 64}
            self.builds[label] = build
            contents['worker-' + label + '.inventory.json'] = json.dumps(build).encode()
        for name, content in contents.items():
            path = self.state / name
            path.write_bytes(content)
            path.chmod(0o600)
        self.instance = smoke.Fixtures(self.state, runtime=True)
        self.clock = SmokeAcceptanceClock()
        template = registry()
        self.current = {**template, 'pools': [], 'launchers': []}
        self.identities = {}
        self.heartbeat_events = {}
        self.container_ports = {}
        for label, ports in (('a', self.A_PORTS), ('b', self.B_PORTS)):
            pool_id = 'fixture-' + label
            pool = copy.deepcopy(template['pools'][0])
            pool['poolId'] = pool_id
            for name in ('firstCutoverEvidenceId', 'firstCutoverEvidenceHash', 'firstCutoverAuthorizedAt'):
                pool[name] = None
            self.current['pools'].append(pool)
            for index, port in enumerate(ports):
                launcher = copy.deepcopy(template['launchers'][index])
                launcher.update(poolId=pool_id, launcherId=pool_id + '-' + str(index),
                    incarnation='unit-' + label + '-incarnation-' + str(index),
                    workerId='unit-native-worker-' + label + '-' + str(index), ready=False,
                    compatibilityInventory=copy.deepcopy(self.builds[label]))
                self.current['launchers'].append(launcher)
                self.identities[port] = {name: launcher[name] for name in ('poolId', 'launcherId', 'incarnation', 'workerId')}
                self.heartbeat_events[launcher['launcherId']] = deque(range(9, 40))
                self.container_ports['unit-' + label + '-container-' + str(index)] = port
        self.phase = 'baseline'
        self.paused = None
        self.missing_outage = set()
        self.pending_acks = set()
        self.withheld_phase = None
        self.withheld_launcher = None
        self.health_overrides = {}
        self.restored_health_overrides = {}
        self.health_failure_phase = 'restore-postgres'
        self.signal_census = {}
        self.signal_census_missing = set()
        self.signal_jobs = {}
        self.signal_jobs_missing = set()
        self.signal_replacement = False
        self.signal_health_overrides = {}
        self.sealed = False
        self.seal_response_error = False
        self.restored_bao = False
        self.term_sent = False
        self.action_delays = {}
        self.operations = []
        self.admission_writes = []
        for target, name, options in (
            (self.instance, 'assert_owned_resources', {}),
            (self.instance, 'connections', {'return_value': {
                'apiPorts': [self.API_PORT], 'poolAPorts': self.A_PORTS, 'poolBPorts': self.B_PORTS}}),
            (self.instance, 'containers', {'side_effect': self.containers}),
            (self.instance, 'port', {'side_effect': self.port}),
            (self.instance, 'compose', {'side_effect': self.compose}),
            (self.instance, 'docker', {'side_effect': self.docker}),
            (self.instance, 'bao', {'side_effect': self.bao}),
            (self.instance, 'bootstrap_bao', {'side_effect': self.bootstrap_bao}),
            (smoke.http.client, 'HTTPConnection', {'side_effect': self.connection}),
            (smoke, 'time', {'new': self.clock}),
            (fleet, 'time', {'new': self.clock}),
        ):
            self.stack.enter_context(patch.object(target, name, **options))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stack.close()

    def run(self, *, include_signal=False):
        return smoke.smoke(self.instance, 2, include_signal=include_signal)

    def record(self, kind, args, timeout):
        started = self.clock.monotonic()
        delay = self.action_delays.pop((kind, args), 0)
        self.clock.elapsed += delay
        self.operations.append({'kind': kind, 'args': args, 'phase': self.phase,
            'started': started, 'received': self.clock.monotonic(), 'timeout': timeout})

    def connection(self, host, port, *, timeout):
        if host != '127.0.0.1' or port not in [*self.api_host_ports, *self.A_PORTS, *self.B_PORTS]:
            raise AssertionError('unit transport must never contact an external origin')
        return SmokeHTTPConnection(self, port, timeout)

    def port(self, service, internal, *, timeout=60, allow_stopped=False):
        if service != 'api' or internal != 8000:
            raise AssertionError('unit port lookup must identify the current API binding')
        self.record('port', (service, internal), timeout)
        return [self.api_port]

    def response(self, port, method, path, body, timeout):
        self.record('http', (port, method, path), timeout)
        unavailable = self.paused is not None and self.paused not in self.missing_outage
        if port in self.api_host_ports:
            if port != self.api_port:
                raise OSError('isolated removed old API host binding')
            if unavailable and self.paused == 'api':
                raise OSError('isolated paused control transport')
            if method == 'GET' and path == '/api/v1/health':
                if self.sealed and self.sealed_api_fallback:
                    self.observed_sealed_fallback = True
                    return SmokeHTTPResponse(200, {})
                return SmokeHTTPResponse(503 if self.sealed else 200, {})
            if method == 'GET' and path == '/api/v1/internal/runtime-launchers':
                if unavailable and self.paused == 'postgres':
                    raise OSError('isolated unavailable authoritative SQL registry')
                for launcher in self.current['launchers']:
                    pool = fleet.find_pool(self.current, launcher['poolId'])
                    if launcher['poolId'] not in self.pending_acks or launcher['ackRevision'] == pool['revision']:
                        continue
                    if self.phase == self.withheld_phase and launcher['launcherId'] == self.withheld_launcher:
                        continue
                    launcher['ackRevision'] = self.heartbeat_events[launcher['launcherId']].popleft()
                snapshot = copy.deepcopy(self.current)
                snapshot['observedAt'] = datetime.now(timezone.utc).isoformat()
                if unavailable and self.paused == 'redis':
                    snapshot['readiness']['redis'] = False
                return SmokeHTTPResponse(200, {'statusCode': 200, 'message': 'OK', 'data': snapshot})
            if method == 'PUT' and path.startswith('/api/v1/internal/runtime-launchers/pools/') and path.endswith('/admission'):
                pool_id = path.split('/')[-2]
                pool = fleet.find_pool(self.current, pool_id)
                self.admission_writes.append({'poolId': pool_id, 'phase': self.phase, 'body': copy.deepcopy(body)})
                if body['initialAccepting'] or body['recoveryAccepting']:
                    return SmokeHTTPResponse(503, {})
                if body['expectedRevision'] != pool['revision']:
                    return SmokeHTTPResponse(409, {})
                pool['revision'] += 1
                pool['retiring'] = body['retiring']
                self.pending_acks.add(pool_id)
                return SmokeHTTPResponse(200, {'statusCode': 200, 'message': 'OK', 'data': copy.deepcopy(pool)})
            raise AssertionError('unexpected API boundary operation')
        if method != 'GET' or path not in ('/livez', '/readyz', '/startupz'):
            raise AssertionError('unexpected worker health boundary operation')
        identity = self.identities[port]
        launcher = next(item for item in self.current['launchers'] if item['launcherId'] == identity['launcherId'])
        health = {**identity, 'healthy': True, 'ready': False, 'sdkRegistered': True,
            'sdkHealthy': True, 'assetsReady': True, 'registryAvailable': not unavailable,
            'ackRevision': launcher['ackRevision'], 'registryAckRevision': launcher['ackRevision'],
            'leaseExpiresAt': launcher['leaseExpiresAt'],
            'desired': copy.deepcopy(fleet.find_pool(self.current, identity['poolId'])),
            'inventory': {name: 0 for name in ('pending', 'assigned', 'launching', 'running', 'canceled', 'completed')},
            'compatibilityInventory': copy.deepcopy(self.builds['a' if port in self.A_PORTS else 'b'])}
        if port == self.A_PORTS[0]:
            health.update(self.health_overrides)
            if self.phase == self.health_failure_phase:
                health.update(self.restored_health_overrides)
            if self.phase == 'before-signal':
                health.update(self.signal_health_overrides)
        return SmokeHTTPResponse(503 if path == '/readyz' else 200, health)

    def compose(self, *args, timeout=180, **kwargs):
        if args[0] == 'pause':
            self.paused = args[1]
            self.phase = 'paused-' + args[1]
        elif args[0] == 'unpause':
            self.paused = None
            self.phase = 'restore-' + args[1]
        elif args == ('restart', 'openbao'):
            self.sealed = False
            self.restored_bao = True
            self.phase = 'restore-bao'
        elif args == ('restart', 'api') and self.remap_api_ports:
            self.api_port += 1
            self.api_host_ports.add(self.api_port)
        self.record('compose', args, timeout)
        return b''

    def bao(self, method, path, payload=None, token=None, *, timeout=None):
        self.record('bao', (method, path), 5 if timeout is None else timeout)
        if method == 'PUT' and path == '/sys/seal':
            self.sealed = True
            if self.seal_response_error:
                raise smoke.FixtureError('isolated Bao seal response unavailable after mutation')
            return {}
        if method == 'GET' and path == '/sys/seal-status':
            return {'initialized': True, 'sealed': self.sealed}
        if method == 'GET' and path == '/sys/health':
            return {'initialized': True, 'sealed': self.sealed, 'cluster_id': 'original-unit-cluster'}
        raise AssertionError('unexpected isolated Bao operation')

    def bootstrap_bao(self, *, timeout=None):
        self.record('bootstrap', (), 60 if timeout is None else timeout)

    def containers(self, service, *, timeout=60):
        if service != 'pool-a':
            raise AssertionError('signal must target only the owned A fleet')
        self.phase = 'before-signal'
        pool = fleet.find_pool(self.current, 'fixture-a')
        pool['retirementBlockers'].update(self.signal_census)
        for name in self.signal_census_missing:
            pool['retirementBlockers'].pop(name, None)
        launcher = self.current['launchers'][0]
        launcher['inventory'].update(self.signal_jobs)
        for name in self.signal_jobs_missing:
            launcher['inventory'].pop(name, None)
        if self.signal_replacement:
            launcher['incarnation'] = 'unit-new-registry-incarnation-not-the-target-container'
            launcher['workerId'] = 'unit-new-native-worker-not-the-target-container'
        return [container for container, port in self.container_ports.items() if port in self.A_PORTS]

    def docker(self, *args, timeout=60, **kwargs):
        kind = 'docker-target' if args[0] == 'inspect' and not self.term_sent else 'docker'
        self.record(kind, args[:1], timeout)
        if args[:3] == ('kill', '--signal', 'TERM'):
            self.term_sent = True
            return b''
        if args[0] == 'wait':
            if not self.term_sent:
                raise AssertionError('native completion requires an original signal')
            return b'0\n0\n'
        if args[0] == 'inspect':
            items = []
            for container in args[1:]:
                port = self.container_ports[container]
                items.append({'Id': container,
                    'Config': {'Hostname': self.identities[port]['launcherId'], 'Labels': {
                        'io.port.runtime-ha.fixture': 'true', 'io.port.runtime-ha.owner': self.owner}},
                    'NetworkSettings': {'Ports': {'8000/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(port)}]}},
                    'State': {'Running': not self.term_sent, 'ExitCode': self.signal_exit_code if self.term_sent else 0}})
            return json.dumps(items).encode()
        raise AssertionError('unexpected isolated Docker operation')


class SmokeRegistryHealthBoundaryTests(unittest.TestCase):
    def test_liveness_startup_and_pinned_inventory_do_not_mask_false_sdk_or_assets_health(self):
        for field in ('sdkRegistered', 'sdkHealthy', 'assetsReady'):
            for invalid in (False, None, 'true'):
                with self.subTest(field=field, invalid=invalid), SmokeDomainBoundary() as boundary:
                    boundary.health_overrides[field] = invalid
                    with self.assertRaises((smoke.FixtureError, fleet.FleetError)):
                        boundary.run()
                    self.assertFalse(any(item['kind'] == 'compose' and item['args'][0] == 'pause'
                        for item in boundary.operations), 'reject the unhealthy baseline before introducing faults')

    def test_health_must_match_the_exact_acknowledged_launcher_incarnation_native_id_and_revision(self):
        for field, invalid in (
            ('launcherId', 'unit-unacknowledged-launcher'), ('incarnation', 'unit-old-incarnation'),
            ('workerId', 'unit-other-native-worker'), ('workerId', None), ('poolId', 'fixture-b'),
            ('ackRevision', 7), ('registryAckRevision', 7),
        ):
            with self.subTest(field=field, invalid=invalid), SmokeDomainBoundary() as boundary:
                boundary.health_overrides[field] = invalid
                with self.assertRaises((smoke.FixtureError, fleet.FleetError)):
                    boundary.run()

    def test_recovery_does_not_inherit_pre_outage_ack_even_while_old_leases_are_valid(self):
        for service in ('postgres', 'redis', 'api'):
            for launcher in ('fixture-a-0', 'fixture-b-1'):
                with self.subTest(service=service, launcher=launcher), SmokeDomainBoundary() as boundary:
                    boundary.withheld_phase = 'restore-' + service
                    boundary.withheld_launcher = launcher
                    with self.assertRaises((smoke.FixtureError, fleet.FleetError)):
                        boundary.run()
                    self.assertIsNone(boundary.paused, 'owned data must be unpaused after failed recovery proof')
                    pool = fleet.find_pool(boundary.current, launcher.rsplit('-', 1)[0])
                    self.assertFalse(pool['initialAccepting'])
                    self.assertFalse(pool['recoveryAccepting'])
                    self.assertIsNone(pool['firstCutoverEvidenceId'])

    def test_exact_post_restore_control_ack_still_requires_current_sdk_and_assets_health(self):
        for field in ('sdkRegistered', 'sdkHealthy', 'assetsReady'):
            with self.subTest(field=field), SmokeDomainBoundary() as boundary:
                boundary.restored_health_overrides[field] = False
                with self.assertRaises((smoke.FixtureError, fleet.FleetError)):
                    boundary.run()
                self.assertIsNone(boundary.paused)

    def test_already_blocked_readyz_is_not_evidence_of_a_redis_or_control_outage(self):
        for service in ('redis', 'api'):
            with self.subTest(service=service), SmokeDomainBoundary() as boundary:
                boundary.missing_outage.add(service)
                with self.assertRaises((smoke.FixtureError, fleet.FleetError)):
                    boundary.run()
                self.assertIsNone(boundary.paused, 'a failed fault criterion still restores the owned service')
                self.assertTrue(any(item['kind'] == 'compose' and item['args'] == ('unpause', service)
                    for item in boundary.operations))


class SmokeSignalRetirementBoundaryTests(unittest.TestCase):
    def test_each_nonzero_or_unknown_cpp_retirement_counter_blocks_before_term(self):
        for counter in fleet.RETIREMENT_COUNTERS:
            for invalid in (1, None, False, -1):
                with self.subTest(counter=counter, invalid=invalid), SmokeDomainBoundary() as boundary:
                    boundary.signal_census[counter] = invalid
                    with self.assertRaises((smoke.FixtureError, fleet.FleetError)):
                        boundary.run(include_signal=True)
                    self.assertFalse(boundary.term_sent, 'never signal a nonempty or unknown obligation census')
            with self.subTest(counter=counter, missing=True), SmokeDomainBoundary() as boundary:
                boundary.signal_census_missing.add(counter)
                with self.assertRaises((smoke.FixtureError, fleet.FleetError)):
                    boundary.run(include_signal=True)
                self.assertFalse(boundary.term_sent)

    def test_nonempty_or_unknown_pending_assigned_launching_running_jobs_block_before_term(self):
        for field in ('pendingJobIds', 'assignedJobIds', 'launchingJobIds', 'runningJobIds'):
            for invalid in (['unit-outstanding-native-job'], None):
                with self.subTest(field=field, invalid=invalid), SmokeDomainBoundary() as boundary:
                    boundary.signal_jobs[field] = invalid
                    with self.assertRaises((smoke.FixtureError, fleet.FleetError)):
                        boundary.run(include_signal=True)
                    self.assertFalse(boundary.term_sent)
            with self.subTest(field=field, missing=True), SmokeDomainBoundary() as boundary:
                boundary.signal_jobs_missing.add(field)
                with self.assertRaises((smoke.FixtureError, fleet.FleetError)):
                    boundary.run(include_signal=True)
                self.assertFalse(boundary.term_sent)

    def test_empty_registry_inventory_does_not_hide_nonempty_or_unknown_current_native_jobs(self):
        for field in ('pending', 'assigned', 'launching', 'running'):
            for invalid in (1, None, False):
                with self.subTest(field=field, invalid=invalid), SmokeDomainBoundary() as boundary:
                    native = {name: 0 for name in ('pending', 'assigned', 'launching', 'running')}
                    native[field] = invalid
                    boundary.signal_health_overrides['inventory'] = native
                    with self.assertRaises((smoke.FixtureError, fleet.FleetError)):
                        boundary.run(include_signal=True)
                    self.assertFalse(boundary.term_sent)
            with self.subTest(field=field, missing=True), SmokeDomainBoundary() as boundary:
                native = {name: 0 for name in ('pending', 'assigned', 'launching', 'running') if name != field}
                boundary.signal_health_overrides['inventory'] = native
                with self.assertRaises((smoke.FixtureError, fleet.FleetError)):
                    boundary.run(include_signal=True)
                self.assertFalse(boundary.term_sent)

    def test_empty_replacement_registry_incarnation_cannot_certify_the_old_target_containers(self):
        with SmokeDomainBoundary() as boundary:
            boundary.signal_replacement = True
            with self.assertRaises((smoke.FixtureError, fleet.FleetError)):
                boundary.run(include_signal=True)
            self.assertFalse(boundary.term_sent)

    def test_stopped_samples_delivered_after_45_seconds_fail_including_signal_command_time(self):
        for signal_seconds, wait_seconds, inspect_seconds in ((46, 0, 0), (0, 46, 0), (0, 0, 46), (20, 20, 6)):
            with self.subTest(signal_seconds=signal_seconds, wait_seconds=wait_seconds, inspect_seconds=inspect_seconds), SmokeDomainBoundary() as boundary:
                boundary.action_delays[('docker', ('kill',))] = signal_seconds
                boundary.action_delays[('docker', ('wait',))] = wait_seconds
                boundary.action_delays[('docker', ('inspect',))] = inspect_seconds
                with self.assertRaisesRegex(smoke.FixtureError, '45'):
                    boundary.run(include_signal=True)
                self.assertTrue(boundary.term_sent, 'exercise actual signal/inspection delivery, not census rejection')


    def test_abnormal_or_unknown_native_container_exit_is_not_successful_empty_shutdown(self):
        for exit_code in (1, 143, None, False, '0'):
            with self.subTest(exit_code=exit_code), SmokeDomainBoundary() as boundary:
                boundary.signal_exit_code = exit_code
                with self.assertRaises(smoke.FixtureError):
                    boundary.run(include_signal=True)
                self.assertTrue(boundary.term_sent)

    def test_genuinely_empty_exact_two_replica_target_reports_measured_signal_elapsed(self):
        with SmokeDomainBoundary() as boundary:
            boundary.action_delays[('docker', ('kill',))] = 1.5
            boundary.action_delays[('docker', ('inspect',))] = 2.25
            result = boundary.run(include_signal=True)
            observed = next(item for item in result if item['scenario'] == 'local-empty-fleet-sigterm')
            self.assertEqual(observed.get('elapsedSeconds'), 3.75)
            self.assertEqual(observed.get('budgetSeconds'), 45)
            self.assertEqual(observed['railwaySignalGrace'], 'unproven')
            self.assertFalse(any(pool['initialAccepting'] or pool['recoveryAccepting'] for pool in boundary.current['pools']))


class SmokeRestorationBudgetBoundaryTests(unittest.TestCase):
    def test_actual_restarted_loopback_binding_is_used_for_restored_control_and_health(self):
        with SmokeDomainBoundary() as boundary:
            boundary.remap_api_ports = True
            result = boundary.run(include_signal=True)
            observed = next(item for item in result if item['scenario'] == 'same-static-seal-original-fixture-data-restart')
            self.assertTrue(observed['apiRecoveryHealthy'])
            self.assertTrue(boundary.term_sent, 'the final native census must also use the restored API binding')

    def test_removed_old_binding_cannot_hide_plaintext_fallback_at_the_current_sealed_api(self):
        with SmokeDomainBoundary() as boundary:
            boundary.remap_api_ports = True
            boundary.sealed_api_fallback = True
            with self.assertRaises(smoke.FixtureError):
                boundary.run()
            self.assertTrue(boundary.observed_sealed_fallback)

    def test_seal_response_failure_still_restores_same_owned_bao_and_api(self):
        with SmokeDomainBoundary() as boundary:
            boundary.seal_response_error = True
            with self.assertRaisesRegex(smoke.FixtureError, 'seal response'):
                boundary.run()
            self.assertFalse(boundary.sealed, 'a seal transport failure must not strand owned data sealed')
            self.assertTrue(boundary.restored_bao)
            self.assertTrue(any(item['kind'] == 'compose' and item['phase'] == 'restore-bao' and
                item['args'] == ('restart', 'api') for item in boundary.operations))

    def test_bao_restore_actions_are_inside_60_seconds_and_finish_owned_restoration_after_failure(self):
        for action in (('compose', ('restart', 'openbao')), ('bootstrap', ()), ('compose', ('restart', 'api'))):
            with self.subTest(action=action), SmokeDomainBoundary() as boundary:
                # The API is also restarted while sealed; inject the restore
                # latency only after the explicit original-Bao restart event.
                if action == ('compose', ('restart', 'api')):
                    original_bootstrap = boundary.bootstrap_bao

                    def bootstrap(*, timeout=None):
                        original_bootstrap(timeout=timeout)
                        boundary.action_delays[action] = 61

                    boundary.stack.enter_context(patch.object(boundary.instance, 'bootstrap_bao', side_effect=bootstrap))
                else:
                    boundary.action_delays[action] = 61
                with self.assertRaisesRegex(smoke.FixtureError, '60'):
                    boundary.run()
                restore = [item for item in boundary.operations if item['phase'] == 'restore-bao']
                self.assertTrue(any(item['kind'] == 'compose' and item['args'] == ('restart', 'openbao') for item in restore))
                self.assertTrue(any(item['kind'] == 'bootstrap' for item in restore))
                self.assertTrue(any(item['kind'] == 'compose' and item['args'] == ('restart', 'api') for item in restore))
                self.assertFalse(boundary.sealed, 'criterion failure must not leave this owned Bao sealed')

    def test_healthy_api_response_received_after_60_seconds_is_not_success(self):
        with SmokeDomainBoundary() as boundary:
            original_bootstrap = boundary.bootstrap_bao

            def bootstrap(*, timeout=None):
                original_bootstrap(timeout=timeout)
                boundary.action_delays[('http', (boundary.API_PORT, 'GET', '/api/v1/health'))] = 61

            boundary.stack.enter_context(patch.object(boundary.instance, 'bootstrap_bao', side_effect=bootstrap))
            with self.assertRaisesRegex(smoke.FixtureError, '60'):
                boundary.run()
            self.assertFalse(boundary.sealed)

    def test_data_unpause_itself_counts_toward_the_60_second_restore_budget(self):
        with SmokeDomainBoundary() as boundary:
            boundary.action_delays[('compose', ('unpause', 'redis'))] = 61
            with self.assertRaisesRegex(smoke.FixtureError, '60'):
                boundary.run()
            self.assertIsNone(boundary.paused)

    def test_timely_original_bao_recovery_reports_elapsed_and_bounds_remaining_action_timeouts(self):
        with SmokeDomainBoundary() as boundary:
            boundary.action_delays[('compose', ('restart', 'openbao'))] = 2
            boundary.action_delays[('bootstrap', ())] = 3
            original_bootstrap = boundary.bootstrap_bao

            def bootstrap(*, timeout=None):
                original_bootstrap(timeout=timeout)
                boundary.action_delays[('compose', ('restart', 'api'))] = 4
                boundary.action_delays[('http', (boundary.API_PORT, 'GET', '/api/v1/health'))] = 5

            boundary.stack.enter_context(patch.object(boundary.instance, 'bootstrap_bao', side_effect=bootstrap))
            result = boundary.run()
            observed = next(item for item in result if item['scenario'] == 'same-static-seal-original-fixture-data-restart')
            self.assertEqual(observed.get('elapsedSeconds'), 14)
            self.assertEqual(observed.get('budgetSeconds'), 60)
            restore = [item for item in boundary.operations if item['phase'] == 'restore-bao']
            started = restore[0]['started']
            for operation in restore:
                with self.subTest(kind=operation['kind'], args=operation['args']):
                    self.assertGreater(operation['timeout'], 0)
                    self.assertLessEqual(operation['timeout'], 60 - (operation['started'] - started))
            self.assertFalse(any(pool['initialAccepting'] or pool['recoveryAccepting'] for pool in boundary.current['pools']))
            self.assertTrue(all(pool['firstCutoverEvidenceId'] is None for pool in boundary.current['pools']))


class SmokeTokenConsumptionBoundaryTests(unittest.TestCase):
    def test_unsafe_token_blocks_before_any_bao_seal_mutation(self):
        for unsafe in ('symlink', 'public-mode', 'oversized'):
            with self.subTest(unsafe=unsafe), SmokeDomainBoundary() as boundary:
                token = boundary.state / 'bao-root.token'
                if unsafe == 'symlink':
                    target = boundary.state / 'unrelated-unit.token'
                    target.write_bytes(b'unrelated-isolated-unit-token')
                    target.chmod(0o600)
                    token.unlink()
                    token.symlink_to(target)
                elif unsafe == 'public-mode':
                    token.chmod(0o644)
                else:
                    token.write_bytes(b'x' * 65537)
                with self.assertRaises(smoke.FixtureError):
                    boundary.run()
                self.assertFalse(any(item['kind'] == 'bao' and item['args'] == ('PUT', '/sys/seal')
                    for item in boundary.operations), 'unsafe token must never be transmitted to Bao')


if __name__ == '__main__':
    unittest.main()
