import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from test_runtime_fleet import fleet, registry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import runtime_provider as provider


def inventory():
    return {
        'protocolRevision': 'runtime-recovery-v1', 'buildId': 'provider-unit-fixture',
        'imageDigest': 'sha256:' + '1' * 64, 'compatibilityFingerprint': 'a' * 64,
        'sdkPatchDigest': '2' * 64, 'builtinInventoryDigest': '3' * 64,
        'modelCacheSelectionDigest': '4' * 64,
        'checkpointCodec': 'port-runtime-checkpoint-v1',
    }


def config():
    build = inventory()
    return {
        'projectId': 'railway-unit-fixture', 'environmentId': 'unit-fixture-env',
        'livekitProjectId': 'cloud-unit-fixture', 'builds': [build],
        'services': [{
            'serviceId': 'pool-a-service', 'role': 'pool-a', 'poolId': 'a', 'replicas': 2,
            'imageReference': 'registry.example.invalid/fixture@' + build['imageDigest'],
            'healthcheckPath': '/readyz', 'startCommand': 'node dist/production-launcher.js',
            'drainingSeconds': 45, 'regions': {'fixture-region': 2},
        }],
    }


def deploy_approval():
    return {
        'scope': 'deploy-runtime-ha-immutable-image', 'projectId': 'railway-unit-fixture',
        'environmentId': 'unit-fixture-env', 'serviceId': 'pool-a-service',
        'imageReference': config()['services'][0]['imageReference'], 'approvedBy': 'unit-fixture-only',
        'expiresAt': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }


def deployed_registry():
    value = registry()
    for launcher in value['launchers']:
        launcher['compatibilityInventory'] = inventory()
    return value


def active_registry_with_alternative():
    value, desired = deployed_registry(), config()
    value['pools'][0]['retirementBlockers']['recoverableSessions'] = 1
    alternative = {**inventory(), 'buildId': 'accepted-alternative', 'imageDigest': 'sha256:' + '2' * 64}
    desired['builds'].append(alternative)
    desired['services'].append({
        **desired['services'][0], 'serviceId': 'pool-b-service', 'role': 'pool-b', 'poolId': 'b',
        'imageReference': 'registry.example.invalid/fixture@' + alternative['imageDigest'],
    })
    value['pools'].append({
        **value['pools'][0], 'poolId': 'b', 'recoveryAccepting': True,
        'retirementBlockers': {name: 0 for name in fleet.RETIREMENT_COUNTERS},
    })
    value['launchers'].extend([
        {**launcher, 'poolId': 'b', 'launcherId': 'b-' + launcher['launcherId'],
            'incarnation': 'b-' + launcher['incarnation'], 'compatibilityInventory': dict(alternative)}
        for launcher in list(value['launchers'])
    ])
    return value, desired


class ProviderSafetyTests(unittest.TestCase):
    def test_same_fingerprint_deploy_does_not_withdraw_when_duties_have_no_recovery_alternative(self):
        for counter in fleet.RETIREMENT_COUNTERS:
            with self.subTest(counter=counter):
                snapshot = deployed_registry()
                snapshot['pools'][0]['retirementBlockers'][counter] = 1
                with patch.object(provider, 'FleetClient') as client, \
                     patch.object(provider, 'set_admission', return_value=snapshot) as withdraw, \
                     patch.object(provider, 'railway') as railway:
                    client.return_value.registry.return_value = snapshot
                    railway.side_effect = [[{'id': 'pool-a-service', 'source': {
                        'image': config()['services'][0]['imageReference']}}], {'submitted': True}]
                    with self.assertRaises(fleet.FleetError):
                        provider.deploy_approved(config(), 'pool-a-service', deploy_approval(), 'https://fixture.invalid/api/v1', False)
                    withdraw.assert_not_called()
                    railway.assert_not_called()

    def test_active_duties_allow_deploy_only_with_fresh_accepted_compatible_recovery_alternative(self):
        snapshot, desired = active_registry_with_alternative()
        with patch.object(provider, 'FleetClient') as client, \
             patch.object(provider, 'set_admission', return_value=snapshot) as withdraw, \
             patch.object(provider, 'railway') as railway:
            client.return_value.registry.return_value = snapshot
            railway.side_effect = [[{'id': 'pool-a-service', 'source': {
                'image': desired['services'][0]['imageReference']}}], {'submitted': True}]
            result = provider.deploy_approved(desired, 'pool-a-service', deploy_approval(), 'https://fixture.invalid/api/v1', False)
            self.assertEqual(result['acceptance'], 'unproven')
            withdraw.assert_called_once()

    def test_withdrawn_expired_unready_or_unaccepted_alternative_never_authorizes_source_withdrawal(self):
        for fault in ('withdrawn', 'expired', 'unready', 'unaccepted-image'):
            with self.subTest(fault=fault):
                snapshot, desired = active_registry_with_alternative()
                if fault == 'withdrawn':
                    snapshot['pools'][1]['recoveryAccepting'] = False
                elif fault == 'expired':
                    snapshot['launchers'][-1]['leaseExpiresAt'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
                elif fault == 'unready':
                    snapshot['launchers'][-1]['ready'] = False
                else:
                    snapshot['launchers'][-1]['compatibilityInventory']['imageDigest'] = 'sha256:' + '3' * 64
                with patch.object(provider, 'FleetClient') as client, \
                     patch.object(provider, 'set_admission', return_value=snapshot) as withdraw, \
                     patch.object(provider, 'railway') as railway:
                    client.return_value.registry.return_value = snapshot
                    railway.side_effect = [[{'id': 'pool-a-service', 'source': {
                        'image': desired['services'][0]['imageReference']}}], {'submitted': True}]
                    with self.assertRaises(fleet.FleetError):
                        provider.deploy_approved(desired, 'pool-a-service', deploy_approval(), 'https://fixture.invalid/api/v1', False)
                    withdraw.assert_not_called()
                    railway.assert_not_called()

    def test_empty_complete_census_allows_approved_deploy_without_fake_recovery_pool(self):
        snapshot = deployed_registry()
        with patch.object(provider, 'FleetClient') as client, \
             patch.object(provider, 'set_admission', return_value=snapshot) as withdraw, \
             patch.object(provider, 'railway') as railway:
            client.return_value.registry.return_value = snapshot
            railway.side_effect = [[{'id': 'pool-a-service', 'source': {
                'image': config()['services'][0]['imageReference']}}], {'submitted': True}]
            result = provider.deploy_approved(config(), 'pool-a-service', deploy_approval(), 'https://fixture.invalid/api/v1', False)
            self.assertEqual(result['acceptance'], 'unproven')
            withdraw.assert_called_once()
            self.assertEqual(railway.call_count, 2)

    def test_token_clock_bounds_are_explicit_verified_input_and_fit_the_cutoff_window(self):
        with patch.object(provider, 'railway', side_effect=fleet.FleetError('unit fixture has no provider auth')):
            evidence = provider.inspect_provider(config())
        with tempfile.TemporaryDirectory(prefix='port-ha-clock-evidence-unit-') as temporary:
            state = Path(temporary)
            artifact = state / 'clock-and-rpc.redacted.txt'
            artifact.write_text('unit fixture only: explicit measured clock/RPC bounds')
            result_path = state / 'provider-result.json'
            approval = {
                'scope': 'record-runtime-acceptance-evidence', 'environmentId': evidence['environmentId'],
                'approvedBy': 'unit-fixture-only', '_contentHash': 'sha256:' + '4' * 64,
                'expiresAt': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            }
            result = {
                'schemaVersion': 1, 'status': 'observed', 'gate': 'cloudTokenRefreshRevocation',
                'provider': 'livekit-cloud', 'projectId': evidence['projectId'],
                'environmentId': evidence['environmentId'], 'protocolRevision': evidence['protocolRevision'],
                'builds': evidence['builds'], 'scenario': 'unit-fixture-only',
                'observedAt': (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                'assertions': {name: True for name in provider.REQUIRED_ASSERTIONS['cloudTokenRefreshRevocation']},
                'artifacts': [{'path': str(artifact), 'sha256': fleet.file_hash(artifact)}],
            }
            for bound in (None, {'maxProviderClockSkewMs': -1, 'maxRevocationRpcDurationMs': 1},
                          {'maxProviderClockSkewMs': 0, 'maxRevocationRpcDurationMs': 0},
                          {'maxProviderClockSkewMs': 30001, 'maxRevocationRpcDurationMs': 30000}):
                with self.subTest(bound=bound):
                    value = dict(result)
                    if bound is not None:
                        value['clockBound'] = bound
                    result_path.write_text(json.dumps(value))
                    with self.assertRaises(fleet.FleetError):
                        provider.record_existing_evidence(evidence, result_path, approval)
            bound = {'maxProviderClockSkewMs': 100, 'maxRevocationRpcDurationMs': 250}
            result_path.write_text(json.dumps({**result, 'clockBound': bound}))
            recorded = provider.record_existing_evidence(evidence, result_path, approval)
            self.assertEqual(recorded['gates']['cloudTokenRefreshRevocation']['clockBound'], bound)

    def test_producer_evidence_utc_format_crosses_actual_api_consumer(self):
        with patch.object(provider, 'railway', side_effect=fleet.FleetError('unit fixture has no provider auth')):
            evidence = provider.inspect_provider(config())
        with tempfile.TemporaryDirectory(prefix='port-ha-evidence-boundary-') as temporary:
            state = Path(temporary)
            approval = {
                'scope': 'record-runtime-acceptance-evidence', 'environmentId': 'unit-fixture-env',
                'approvedBy': 'unit-fixture-only',
                'expiresAt': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                '_contentHash': 'sha256:' + '4' * 64,
            }
            for gate in ('cloudAssignmentFreshness', 'cloudAbsentParticipantRevocation', 'cloudTokenRefreshRevocation'):
                artifact = state / (gate + '.redacted.txt')
                artifact.write_text('isolated unit evidence only, not Cloud acceptance')
                result_path = state / (gate + '.json')
                result_path.write_text(json.dumps({
                    'schemaVersion': 1, 'status': 'observed', 'gate': gate,
                    'provider': 'livekit-cloud', 'projectId': evidence['projectId'],
                    'environmentId': evidence['environmentId'], 'protocolRevision': evidence['protocolRevision'],
                    'builds': evidence['builds'], 'scenario': 'unit-fixture-only',
                    'observedAt': (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                    'assertions': {name: True for name in provider.REQUIRED_ASSERTIONS[gate]},
                    'clockBound': {'maxProviderClockSkewMs': 100, 'maxRevocationRpcDurationMs': 250},
                    'artifacts': [{'path': str(artifact), 'sha256': fleet.file_hash(artifact)}],
                }))
                evidence = provider.record_existing_evidence(evidence, result_path, approval)
            payload = state / 'evidence.json'
            payload.write_text(json.dumps(evidence))
            api = Path(os.environ.get('RUNTIME_API_SOURCE', ROOT.parents[2] / 'api/.worktrees/feat-runtime-ha'))
            worker = Path(os.environ.get('RUNTIME_WORKER_SOURCE', ROOT.parents[2] / 'voice-agent/.worktrees/feat-runtime-ha'))
            consumer = api / 'src/modules/runtime-recovery/domain/runtime-evidence.ts'
            loader = worker / 'node_modules/tsx/dist/loader.mjs'
            program = (
                'import {readFileSync} from "node:fs";'
                'const {runtimeEvidenceSchema}=await import(' + json.dumps(consumer.as_uri()) + ');'
                'process.stdout.write(JSON.stringify(runtimeEvidenceSchema.safeParse(JSON.parse(readFileSync('
                + json.dumps(str(payload)) + ',"utf8"))).success));'
            )
            result = subprocess.run([
                os.environ.get('RUNTIME_NODE_BINARY', 'node'), '--import', loader.as_uri(),
                '--input-type=module', '--eval', program,
            ], cwd=state, capture_output=True, timeout=30, check=False)
            self.assertEqual(result.returncode, 0, 'actual API schema prerequisite failed: ' + result.stderr.decode())
            self.assertTrue(json.loads(result.stdout), 'real API consumer rejects producer timestamp format')
            self.assertTrue(evidence['observedAt'].endswith('Z'))
            self.assertTrue(evidence['expiresAt'].endswith('Z'))
            self.assertTrue(all(evidence['gates'][gate]['observedAt'].endswith('Z') for gate in (
                'cloudAssignmentFreshness', 'cloudAbsentParticipantRevocation', 'cloudTokenRefreshRevocation')))


if __name__ == '__main__':
    unittest.main()
