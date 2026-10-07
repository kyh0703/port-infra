#!/usr/bin/env python3
"""Exercise real labeled local fleet/data outage boundaries without paid calls.

This is an executable acceptance harness for the parent/operator, not proof until
run. Its output explicitly does not prove Cloud fencing, placement or RPO=0.
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
from pathlib import Path

from runtime_fixtures import FixtureError, Fixtures, read_private_secret
from runtime_fleet import (
    FleetClient, FleetError, FleetHTTPError, find_pool, load_json, require_data_ready,
    require_inventory, require_pool_inventory, require_retirable, require_zero_census, set_admission, write_new_json,
)


class AcceptanceBudget:
    def __init__(self, seconds: int, scenario: str):
        self.seconds = seconds
        self.scenario = scenario
        self.started = time.monotonic()
        self.deadline = self.started + seconds

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def remaining(self, maximum: float | None = None) -> float:
        elapsed = self.elapsed
        remaining = self.seconds - elapsed
        if remaining <= 0:
            raise FixtureError(f'{self.scenario} exceeded {self.seconds}-second acceptance budget (elapsed {elapsed:.3f}s)')
        return remaining if maximum is None else min(maximum, remaining)


def http_status(port: int, path: str, *, timeout: float = 3) -> int:
    if timeout <= 0:
        return 0
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    response = None
    try:
        connection.request('GET', path)
        transport = connection.sock
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0
        if transport is not None:
            transport.settimeout(remaining)
        response = connection.getresponse()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # Do not hide a received API 200 from the sealed-bootstrap probe.
            # Positive acceptance still checks its outer budget after receipt.
            return response.status
        if transport is not None:
            transport.settimeout(remaining)
        response.read(65536)
        return response.status
    except (OSError, http.client.HTTPException):
        return 0
    finally:
        if response is not None:
            response.close()
        connection.close()


def await_health(ports: list[int], *, ready: bool, budget: AcceptanceBudget | None = None) -> None:
    budget = budget or AcceptanceBudget(60, 'runtime liveness/admission health')
    while True:
        observed = [(http_status(port, '/livez', timeout=budget.remaining(3)),
            http_status(port, '/readyz', timeout=budget.remaining(3))) for port in ports]
        budget.remaining()
        if all(live == 200 and ((status == 200) if ready else (status == 503)) for live, status in observed):
            return
        time.sleep(min(0.25, budget.remaining()))


def require_unproven_enabling_blocked(client: FleetClient, pool_id: str) -> None:
    snapshot = client.registry()
    require_data_ready(snapshot)
    pool = find_pool(snapshot, pool_id)
    if pool['initialAccepting'] or pool['recoveryAccepting'] or any(
        pool.get(name) is not None for name in (
            'firstCutoverEvidenceId', 'firstCutoverEvidenceHash', 'firstCutoverAuthorizedAt',
        )
    ):
        raise FleetError('local unproven smoke requires a withdrawn pool without a fabricated first-cutover attestation')
    try:
        client.request('PUT', '/internal/runtime-launchers/pools/' + pool_id + '/admission', {
            'initialAccepting': True, 'recoveryAccepting': True, 'retiring': False,
            'expectedRevision': pool['revision'],
        })
    except FleetHTTPError as error:
        if error.status != 503:
            raise FleetError('a missing/unauthenticated/conflicting route is not unproven-cutover enforcement') from error
    else:
        raise FleetError('unproven local/OSS enabling was accepted; never label this Cloud or first-cutover proof')
    current = find_pool(client.registry(), pool_id)
    if current['revision'] != pool['revision'] or current['initialAccepting'] or current['recoveryAccepting'] or any(
        current.get(name) is not None for name in (
            'firstCutoverEvidenceId', 'firstCutoverEvidenceHash', 'firstCutoverAuthorizedAt',
        )
    ):
        raise FleetError('rejected enabling changed admission or wrote a fabricated first-cutover attestation')


def require_worker_health_inventory(port: int, expected: dict, launchers: list[dict], pool_id: str, *, timeout: float = 3) -> dict:
    require_inventory(expected)
    if timeout <= 0:
        raise FixtureError('worker health request budget expired')
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    response = None
    try:
        connection.request('GET', '/livez')
        transport = connection.sock
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FixtureError('worker health request budget expired')
        if transport is not None:
            transport.settimeout(remaining)
        response = connection.getresponse()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FixtureError('worker health response arrived after its request budget')
        if transport is not None:
            transport.settimeout(remaining)
        raw = response.read(65537)
        if response.status != 200 or len(raw) > 65536 or time.monotonic() >= deadline:
            raise FixtureError('worker liveness inventory is unavailable within its request budget')
        health = json.loads(raw)
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise FixtureError('worker liveness inventory is unavailable') from error
    finally:
        if response is not None:
            response.close()
        connection.close()
    if not isinstance(health, dict) or any(
        health.get(name) is not True for name in ('sdkRegistered', 'sdkHealthy', 'assetsReady', 'registryAvailable')
    ):
        raise FixtureError('worker native SDK/assets/control health is not current')
    identity_fields = ('launcherId', 'incarnation', 'workerId')
    if health.get('poolId') != pool_id or any(
        not isinstance(health.get(name), str) or not health[name] for name in identity_fields
    ):
        raise FixtureError('worker native incarnation identity is unavailable')
    matching = [launcher for launcher in launchers if all(
        launcher.get(name) == health[name] for name in identity_fields
    ) and launcher.get('poolId') == pool_id]
    if len(matching) != 1 or any(
        type(health.get(name)) is not int or health[name] != matching[0]['ackRevision']
        for name in ('ackRevision', 'registryAckRevision')
    ):
        raise FixtureError('worker health does not match its exact acknowledged registry/native incarnation')
    actual = health.get('compatibilityInventory')
    require_inventory(actual)
    if any(actual.get(name) != expected[name] for name in (
        'protocolRevision', 'buildId', 'imageDigest', 'compatibilityFingerprint',
        'sdkPatchDigest', 'builtinInventoryDigest', 'modelCacheSelectionDigest', 'checkpointCodec',
    )):
        raise FixtureError('actual worker health does not match the pinned baked cache/code/image inventory')
    return health


def await_fleet(client: FleetClient, connections: dict, inventories: dict[str, dict], replicas: int, *,
                budget: AcceptanceBudget, revisions: dict[str, int] | None = None) -> dict:
    for label, key in (('a', 'poolAPorts'), ('b', 'poolBPorts')):
        if len(connections[key]) != replicas or len(set(connections[key])) != replicas:
            raise FixtureError('owned worker health ports do not identify the exact replica fleet')
    while True:
        try:
            snapshot = client.registry(timeout=budget.remaining(10))
            budget.remaining()
            require_data_ready(snapshot)
            for label, key in (('a', 'poolAPorts'), ('b', 'poolBPorts')):
                pool_id = 'fixture-' + label
                if revisions is not None and find_pool(snapshot, pool_id)['revision'] != revisions[pool_id]:
                    raise FleetError('post-restore desired revision changed before native health acknowledgment')
                launchers = require_pool_inventory(snapshot, pool_id, replicas, inventories[label])
                identities = set()
                for port in connections[key]:
                    health = require_worker_health_inventory(port, inventories[label], launchers, pool_id,
                        timeout=budget.remaining(3))
                    budget.remaining()
                    identities.add((health['launcherId'], health['incarnation'], health['workerId']))
                    if http_status(port, '/startupz', timeout=budget.remaining(3)) != 200 or \
                            http_status(port, '/readyz', timeout=budget.remaining(3)) != 503:
                        raise FixtureError('native startup or withdrawn admission health is not current')
                    budget.remaining()
                if identities != {(item['launcherId'], item['incarnation'], item['workerId']) for item in launchers}:
                    raise FixtureError('owned health ports do not cover every exact acknowledged incarnation')
            require_data_ready(snapshot)
            budget.remaining()
            return snapshot
        except (FixtureError, FleetError):
            budget.remaining()
            time.sleep(min(0.25, budget.remaining()))


def restore_fleet_registration(client: FleetClient, connections: dict, inventories: dict[str, dict],
                               replicas: int, budget: AcceptanceBudget) -> dict:
    revisions = {}
    try:
        await_fleet(client, connections, inventories, replicas, budget=budget)
        for pool_id in ('fixture-a', 'fixture-b'):
            snapshot = set_admission(client, pool_id, replicas, initial=False, recovery=False, retiring=False,
                timeout=budget.remaining(), deadline=budget.deadline)
            budget.remaining()
            revisions[pool_id] = find_pool(snapshot, pool_id)['revision']
        return await_fleet(client, connections, inventories, replicas, budget=budget, revisions=revisions)
    except FleetError:
        budget.remaining()
        raise


def await_outage(client: FleetClient, worker_ports: list[int], service: str) -> None:
    budget = AcceptanceBudget(60, service + ' outage observation')
    while True:
        await_health(worker_ports, ready=False, budget=budget)
        try:
            snapshot = client.registry(timeout=budget.remaining(10))
            readiness = snapshot.get('readiness')
            unavailable = service == 'redis' and isinstance(readiness, dict) and readiness.get('redis') is False
        except FleetHTTPError as error:
            if error.status not in (500, 502, 503, 504):
                raise
            unavailable = service in ('postgres', 'api')
        except FleetError:
            unavailable = service in ('postgres', 'api')
        budget.remaining()
        if unavailable:
            return
        time.sleep(min(0.25, budget.remaining()))


def restore_original_bao(fixtures: Fixtures, client: FleetClient, connections: dict,
                         inventories: dict[str, dict], replicas: int, original_cluster: str) -> tuple[AcceptanceBudget, FleetClient]:
    budget = AcceptanceBudget(60, 'same-original-Bao/API restoration')
    # Expiry fails the proof, not the obligation to restore THIS owned data.
    # Only necessary restoration steps may use ordinary safe limits afterward.
    for stage, operation, arguments, ordinary_limit in (
        ('bao', fixtures.compose, ('restart', 'openbao'), 180),
        ('bootstrap', fixtures.bootstrap_bao, (), 60),
        ('cluster', fixtures.bao, ('GET', '/sys/health'), 5),
        ('api', fixtures.compose, ('restart', 'api'), 180),
    ):
        remaining = budget.deadline - time.monotonic()
        timeout = min(ordinary_limit, remaining) if remaining > 0 else ordinary_limit
        try:
            result = operation(*arguments, timeout=timeout)
        except FixtureError:
            if budget.elapsed < budget.seconds:
                raise
            result = operation(*arguments, timeout=ordinary_limit)
        if stage == 'cluster' and result.get('cluster_id') != original_cluster:
            raise FixtureError('same-key restart substituted the original fixture Bao cluster')
    budget.remaining()
    while True:
        connections['apiPorts'] = fixtures.port('api', 8000, timeout=budget.remaining(60))
        budget.remaining()
        healthy = all(http_status(port, '/api/v1/health', timeout=budget.remaining(3)) == 200
            for port in connections['apiPorts'])
        budget.remaining()
        if healthy:
            break
        time.sleep(min(0.5, budget.remaining()))
    client = FleetClient('http://127.0.0.1:' + str(connections['apiPorts'][0]) + '/api/v1',
        client.control_key, allow_loopback=True)
    restore_fleet_registration(client, connections, inventories, replicas, budget)
    budget.remaining()
    return budget, client


def smoke(fixtures: Fixtures, replicas: int, *, include_signal: bool) -> list[dict]:
    fixtures.assert_owned_resources()
    if not fixtures.runtime:
        raise FixtureError('full immutable API+A/B fixture must be up before runtime smoke')
    values = fixtures.assert_state_files()
    connections = fixtures.connections()
    client = FleetClient('http://127.0.0.1:' + str(connections['apiPorts'][0]) + '/api/v1', values['RUNTIME_HA_CONTROL_KEY'], allow_loopback=True)
    worker_ports = connections['poolAPorts'] + connections['poolBPorts']
    observations = []
    inventories = {label: load_json(fixtures.state / ('worker-' + label + '.inventory.json'), private=True)
        for label in ('a', 'b')}
    await_fleet(client, connections, inventories, replicas,
        budget=AcceptanceBudget(60, 'healthy native fleet baseline'))
    for pool in ('fixture-a', 'fixture-b'):
        require_unproven_enabling_blocked(client, pool)
    observations.append({'scenario': 'ordinary-enabling-blocked-without-cloud-first-proof',
        'status': 'observed', 'externalAcceptance': 'unproven', 'replicasPerPool': replicas})
    set_admission(client, 'fixture-a', replicas, initial=False, recovery=False, retiring=False)
    await_fleet(client, connections, inventories, replicas,
        budget=AcceptanceBudget(60, 'withdrawn native fleet health'))
    observations.append({'scenario': 'withdrawal-keeps-native-sdk-process-live',
        'status': 'observed', 'resumeWithApprovedProof': 'unproven', 'sdkDrainRequested': False})

    for service in ('postgres', 'redis', 'api'):
        try:
            fixtures.compose('pause', service)
            await_outage(client, worker_ports, service)
            observations.append({'scenario': service + '-outage', 'status': 'observed',
                'runtimeReady': False, 'processLive': True, 'admissionAlreadyBlocked': True,
                'controlOrDataUnavailable': True})
        finally:
            restore_budget = AcceptanceBudget(60, service + ' restoration')
            try:
                fixtures.compose('unpause', service, timeout=restore_budget.remaining(180))
            except FixtureError:
                # Even a failed bounded unpause must not leave owned data paused.
                fixtures.compose('unpause', service)
                restore_budget.remaining()
                raise
            restore_budget.remaining()
            restore_fleet_registration(client, connections, inventories, replicas, restore_budget)
        observations.append({'scenario': service + '-restore', 'status': 'observed',
            'elapsedSeconds': restore_budget.elapsed, 'budgetSeconds': restore_budget.seconds,
            'admissionAlreadyBlocked': True})

    # Seal THIS disposable Bao, then restart API while no key bootstrap is
    # available. A cached process key is not mistaken for server availability.
    token = read_private_secret(fixtures.state / 'bao-root.token').strip()
    original = fixtures.bao('GET', '/sys/health')['cluster_id']
    try:
        fixtures.bao('PUT', '/sys/seal', {}, token)
        fixtures.compose('restart', 'api')
        observation_seconds = 30
        deadline = time.monotonic() + observation_seconds
        while True:
            if fixtures.bao('GET', '/sys/seal-status').get('sealed') is not True:
                raise FixtureError('fixture Bao did not remain sealed during cold API bootstrap observation')
            current_api_ports = fixtures.port('api', 8000, allow_stopped=True)
            if any(http_status(port, '/api/v1/health') == 200 for port in current_api_ports):
                raise FixtureError('API started despite unavailable encrypted key bootstrap')
            if time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        await_health(worker_ports, ready=False)
        observations.append({'scenario': 'sealed-key-bootstrap', 'status': 'observed',
            'plaintextFallback': False, 'coldStartObservationSeconds': observation_seconds,
            'productionSealAvailability': 'unproven'})
    finally:
        restore_budget, client = restore_original_bao(fixtures, client, connections, inventories, replicas, original)
    observations.append({'scenario': 'same-static-seal-original-fixture-data-restart', 'status': 'observed',
        'apiRecoveryHealthy': True, 'productionSealAvailability': 'unproven',
        'elapsedSeconds': restore_budget.elapsed, 'budgetSeconds': restore_budget.seconds})

    if include_signal:
        preflight = AcceptanceBudget(60, 'exact EMPTY A-fleet TERM preflight')
        containers = fixtures.containers('pool-a', timeout=preflight.remaining(60))
        targeted = json.loads(fixtures.docker('inspect', *containers, timeout=preflight.remaining(60)))
        preflight.remaining()
        if len(containers) != replicas or len(set(containers)) != replicas or len(targeted) != replicas or \
                {item.get('Id') for item in targeted} != set(containers):
            raise FixtureError('TERM targets do not identify the exact owned A replica containers')
        target_ports = []
        for item in targeted:
            mappings = item.get('NetworkSettings', {}).get('Ports', {}).get('8000/tcp') or []
            local = [mapping for mapping in mappings if mapping.get('HostIp') == '127.0.0.1']
            if len(local) != 1 or item.get('State', {}).get('Running') is not True:
                raise FixtureError('TERM target has no unique live loopback native health binding')
            target_ports.append(int(local[0]['HostPort']))
        if len(set(target_ports)) != replicas or set(target_ports) != set(connections['poolAPorts']):
            raise FixtureError('TERM target containers changed from the acknowledged owned A health fleet')
        snapshot = await_fleet(client, connections, inventories, replicas, budget=preflight)
        launchers = require_pool_inventory(snapshot, 'fixture-a', replicas, inventories['a'])
        for port in target_ports:
            health = require_worker_health_inventory(port, inventories['a'], launchers, 'fixture-a',
                timeout=preflight.remaining(3))
            preflight.remaining()
            require_zero_census(health.get('inventory'), ('pending', 'assigned', 'launching', 'running'),
                'target native job inventory')
        require_retirable(snapshot, 'fixture-a', replicas)
        preflight.remaining()
        # This exact EMPTY scenario does not prove active-task continuation,
        # inference shutdown, or the external provider's signal delivery.
        signal_budget = AcceptanceBudget(45, 'local EMPTY A-fleet TERM shutdown')
        try:
            fixtures.docker('kill', '--signal', 'TERM', *containers, timeout=signal_budget.remaining(60))
            signal_budget.remaining()
            fixtures.docker('wait', *containers, timeout=signal_budget.remaining(60))
            signal_budget.remaining()
            inspected = json.loads(fixtures.docker('inspect', *containers, timeout=signal_budget.remaining(60)))
            signal_budget.remaining()
            if len(inspected) != len(containers) or {item.get('Id') for item in inspected} != set(containers):
                raise FixtureError('TERM inspection does not cover the exact signaled containers')
            if any(item['State']['Running'] is not False for item in inspected):
                raise FixtureError('native container completion did not stop the exact signaled containers')
            if any(item['State'].get('ExitCode') == 137 for item in inspected):
                raise FixtureError('launcher SIGTERM required SIGKILL instead of bounded shutdown')
            if any(type(item['State'].get('ExitCode')) is not int or item['State']['ExitCode'] != 0 for item in inspected):
                raise FixtureError('empty launcher TERM did not complete successfully')
        except FixtureError:
            signal_budget.remaining()
            raise
        observations.append({'scenario': 'local-empty-fleet-sigterm', 'status': 'observed',
            'elapsedSeconds': signal_budget.elapsed, 'budgetSeconds': signal_budget.seconds,
            'exitCodes': [item['State']['ExitCode'] for item in inspected], 'railwaySignalGrace': 'unproven'})
    return observations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--replicas', type=int, default=2)
    parser.add_argument('--include-signal', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.exists() or args.output.is_symlink():
            raise FixtureError('smoke output already exists')
        observations = smoke(Fixtures(args.state_dir, runtime=True), args.replicas, include_signal=args.include_signal)
        result = {'fixtureOnly': True, 'status': 'observed', 'observations': observations,
            'externalAcceptance': 'blocked', 'twoPoolHaActivation': 'unproven',
            'cloudAssignmentFreshness': 'unproven', 'cloudAbsentParticipantRevocation': 'unproven',
            'cloudTokenRefreshRevocation': 'unproven', 'railwayPlacement': 'unproven',
            'railwaySignalGrace': 'unproven', 'postgresqlFailoverDurability': 'unproven',
            'redisStateLoss': 'unproven', 'productionSealAvailability': 'unproven'}
        write_new_json(args.output, result)
        print(json.dumps(result, indent=2))
        return 0
    except (FixtureError, FleetError, OSError, ValueError) as error:
        reason = str(error) if isinstance(error, (FixtureError, FleetError)) else 'smoke environment unavailable'
        print(json.dumps({'fixtureOnly': True, 'status': 'failed', 'reason': reason}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
