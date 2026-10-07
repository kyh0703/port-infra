import copy
import hashlib
import importlib.util
import io
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runtime_fleet', ROOT / 'scripts/runtime_fleet.py')
fleet = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fleet
spec.loader.exec_module(fleet)
sys.path.insert(0, str(ROOT / 'scripts'))
import runtime_provider as provider


def registry():
    now = datetime.now(timezone.utc)
    return {
        'protocolRevision': 'runtime-recovery-v1',
        'observedAt': now.isoformat(),
        'readiness': {'database': True, 'redis': True, 'keyring': True},
        'pools': [{
            'poolId': 'a', 'revision': 8, 'initialAccepting': False,
            'recoveryAccepting': False, 'retiring': False,
            'compatibilityFingerprint': 'a' * 64,
            'retirementBlockers': {name: 0 for name in fleet.RETIREMENT_COUNTERS},
            'firstCutoverEvidenceId': '01999999-1111-4111-8111-111111111111',
            'firstCutoverEvidenceHash': 'sha256:' + '9' * 64,
            'firstCutoverAuthorizedAt': now.isoformat(),
        }],
        'launchers': [{
            'launcherId': 'a-' + str(index), 'incarnation': 'inc-' + str(index),
            'poolId': 'a', 'ready': True, 'ackRevision': 8,
            'compatibilityFingerprint': 'a' * 64,
            'leaseExpiresAt': (now + timedelta(seconds=60)).isoformat(),
            'inventory': {name: [] for name in fleet.JOB_INVENTORY_FIELDS},
            'compatibilityInventory': {
                'protocolRevision': 'runtime-recovery-v1', 'buildId': 'registry-fixture',
                'imageDigest': 'sha256:' + '1' * 64, 'compatibilityFingerprint': 'a' * 64,
                'sdkPatchDigest': '2' * 64, 'builtinInventoryDigest': '3' * 64,
                'checkpointCodec': 'port-runtime-checkpoint-v1',
            },
        } for index in range(2)],
        'legacyDrain': {name: 0 for name in fleet.LEGACY_COUNTERS},
    }


def isolated_inventory():
    return {
        'protocolRevision': 'runtime-recovery-v1', 'buildId': 'isolated-structural-fixture',
        'imageDigest': 'sha256:' + '1' * 64, 'compatibilityFingerprint': 'a' * 64,
        'sdkPatchDigest': '2' * 64, 'builtinInventoryDigest': '3' * 64,
        'modelCacheSelectionDigest': '4' * 64, 'checkpointCodec': 'port-runtime-checkpoint-v1',
    }


def fixture_hash(path):
    return 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()


def isolated_acceptance_evidence(state, build):
    """Exercise the actual file producer's structure, never provider acceptance."""
    now = datetime.now(timezone.utc)
    evidence = {
        'schemaVersion': 1, 'evidenceId': '01999999-1111-4111-8111-111111111111',
        'provider': 'livekit-cloud', 'projectId': 'isolated-cloud-project',
        'railwayProjectId': 'isolated-railway-project', 'environmentId': 'isolated-test',
        'protocolRevision': 'runtime-recovery-v1', 'observedAt': fleet.utc_iso(now),
        'expiresAt': fleet.utc_iso(now + timedelta(hours=1)), 'builds': [build],
        'gates': {name: {'status': 'unproven', 'scenario': None, 'artifacts': []}
                  for name in fleet.EXTERNAL_GATES},
    }
    approval = {
        'scope': 'record-runtime-acceptance-evidence', 'environmentId': evidence['environmentId'],
        'approvedBy': 'isolated-structural-fixture-NOT-provider-proof', 'expiresAt': evidence['expiresAt'],
    }
    approval_path = state / 'structural-approval.json'
    approval_path.write_text(json.dumps(approval))
    approval_path.chmod(0o600)
    approval['_contentHash'] = fixture_hash(approval_path)
    for name in fleet.EXTERNAL_GATES:
        artifact = state / (name + '.redacted.txt')
        artifact.write_text('Isolated structural test bytes only. NOT Cloud, placement, key or durability proof.')
        artifact.chmod(0o600)
        cloud = name.startswith('cloud')
        result = {
            'schemaVersion': 1, 'status': 'observed', 'gate': name,
            'provider': 'livekit-cloud' if cloud else 'railway',
            'projectId': evidence['projectId'] if cloud else evidence['railwayProjectId'],
            'environmentId': evidence['environmentId'], 'protocolRevision': evidence['protocolRevision'],
            'builds': evidence['builds'], 'scenario': 'isolated-structural-fixture-NOT-provider-proof',
            'observedAt': fleet.utc_iso(now - timedelta(seconds=1)),
            'assertions': {assertion: True for assertion in provider.REQUIRED_ASSERTIONS[name]},
            'artifacts': [{'path': str(artifact), 'sha256': fixture_hash(artifact), 'redacted': True}],
        }
        if name == 'cloudTokenRefreshRevocation':
            result['clockBound'] = {'maxProviderClockSkewMs': 100, 'maxRevocationRpcDurationMs': 250}
        result_path = state / (name + '.json')
        result_path.write_text(json.dumps(result))
        result_path.chmod(0o600)
        evidence = provider.record_existing_evidence(evidence, result_path, approval)
    return evidence


def isolated_fleet_config(topology):
    build = isolated_inventory()
    return {
        'projectId': 'isolated-railway-project', 'environmentId': 'isolated-test',
        'livekitProjectId': 'isolated-cloud-project', 'builds': [build],
        'services': [{
            'serviceId': role + '-service-' + str(index), 'role': role, 'poolId': pool_id,
            'replicas': replicas, 'imageReference': 'registry.example.invalid/structural-fixture@' + build['imageDigest'],
            'healthcheckPath': '/readyz', 'startCommand': 'node dist/production-launcher.js',
            'drainingSeconds': 45, 'regions': {'isolated-region': replicas},
        } for index, (role, pool_id, replicas) in enumerate(topology)],
    }


def isolated_rotation_receipt(state):
    """Retained synthetic files only; no rotation command or credential is used."""
    observed = fleet.utc_iso()
    classes = ['livekit-project']
    artifact = state / 'rotation.redacted.txt'
    artifact.write_text('Isolated structural rotation receipt. NOT provider revocation proof.')
    artifact.chmod(0o600)
    approval_path, result_path = state / 'rotation-approval.json', state / 'rotation-result.json'
    approval = {
        'scope': fleet.ROTATION_SCOPE, 'environmentId': 'isolated-test', 'approvedBy': 'isolated-test-only',
        'expiresAt': fleet.utc_iso(datetime.now(timezone.utc) + timedelta(hours=1)),
        'command': ['/not-executed-by-structural-fixture'], 'credentialClasses': classes,
        'resultFile': str(result_path),
    }
    approval_path.write_text(json.dumps(approval))
    approval_path.chmod(0o600)
    result_path.write_text(json.dumps({
        'schemaVersion': 1, 'scope': fleet.ROTATION_SCOPE, 'environmentId': 'isolated-test',
        'status': 'observed', 'observedAt': observed, 'credentialClasses': classes,
        'providerReceipts': [{
            'credentialClass': 'livekit-project', 'provider': 'isolated-test-NOT-provider-proof',
            'receiptId': 'isolated-structural-receipt', 'status': 'revoked-and-replaced',
            'revocationVerified': True, 'replacementVerified': True,
            'oldCredentialDenied': True, 'newCredentialAccepted': True,
            'artifacts': [{'path': str(artifact), 'sha256': fixture_hash(artifact), 'redacted': True}],
        }],
    }))
    result_path.chmod(0o600)
    receipt_path = state / 'rotation-receipt.json'
    receipt_path.write_text(json.dumps({
        'schemaVersion': 1, 'scope': fleet.ROTATION_SCOPE, 'environmentId': 'isolated-test',
        'approvalPath': str(approval_path), 'approvalSha256': fixture_hash(approval_path),
        'credentialClasses': classes, 'observedAt': observed, 'commandExitCode': 0,
        'providerVerification': 'observed', 'resultFile': str(result_path), 'resultSha256': fixture_hash(result_path),
    }))
    receipt_path.chmod(0o600)
    return receipt_path


class FleetSafetyTests(unittest.TestCase):
    def test_one_replica_ack_does_not_acknowledge_a_fleet(self):
        value = registry()
        value['launchers'][1]['ackRevision'] = 7
        with self.assertRaises(fleet.FleetError):
            fleet.require_pool_ack(value, 'a', 2)
        value['launchers'][1]['ackRevision'] = 8
        fleet.require_pool_ack(value, 'a', 2)

    def test_restarted_or_missing_replica_does_not_inherit_old_ack(self):
        value = registry()
        value['launchers'][1]['incarnation'] = 'replacement'
        value['launchers'][1]['ackRevision'] = 0
        with self.assertRaises(fleet.FleetError):
            fleet.require_pool_ack(value, 'a', 2)
        value['launchers'].pop()
        with self.assertRaises(fleet.FleetError):
            fleet.require_pool_ack(value, 'a', 2)

    def test_expired_registration_and_stale_observation_fail_closed(self):
        value = registry()
        value['launchers'][1]['leaseExpiresAt'] = '2020-01-01T00:00:00Z'
        with self.assertRaises(fleet.FleetError):
            fleet.require_pool_ack(value, 'a', 2)
        value = registry()
        value['observedAt'] = '2020-01-01T00:00:00Z'
        with self.assertRaises(fleet.FleetError):
            fleet.require_pool_ack(value, 'a', 2)

    def test_empty_jobs_are_not_proof_recoverable_lineage_can_retire(self):
        for counter in fleet.RETIREMENT_COUNTERS:
            with self.subTest(counter=counter):
                value = registry()
                value['pools'][0]['retirementBlockers'][counter] = 1
                with self.assertRaises(fleet.FleetError):
                    fleet.require_retirable(value, 'a', 2)
        fleet.require_retirable(registry(), 'a', 2)

    def test_pending_assigned_and_launching_jobs_block_retirement(self):
        for field in ('pendingJobIds', 'assignedJobIds', 'launchingJobIds', 'runningJobIds'):
            value = registry()
            value['launchers'][0]['inventory'][field] = ['job-before-child-spawn']
            with self.assertRaises(fleet.FleetError):
                fleet.require_retirable(value, 'a', 2)

    def test_missing_census_is_unknown_not_zero_and_accepting_pool_cannot_retire(self):
        value = registry()
        del value['pools'][0]['retirementBlockers']['unknownOperations']
        with self.assertRaises(fleet.FleetError):
            fleet.require_retirable(value, 'a', 2)
        value = registry()
        value['pools'][0]['initialAccepting'] = True
        with self.assertRaises(fleet.FleetError):
            fleet.require_retirable(value, 'a', 2)

    def test_legacy_cutover_checks_pending_usage_admission_and_credential_routes(self):
        for counter in fleet.LEGACY_COUNTERS:
            value = registry()
            value['legacyDrain'][counter] = 1
            with self.assertRaises(fleet.FleetError):
                fleet.require_legacy_drained(value)
        value = registry()
        del value['legacyDrain']['legacyCredentialRoutes']
        with self.assertRaises(fleet.FleetError):
            fleet.require_legacy_drained(value)
        fleet.require_legacy_drained(registry())

    def test_compatible_rollback_requires_exact_codec_sdk_and_builtin_inventory(self):
        source = {
            'protocolRevision': 'runtime-recovery-v1', 'buildId': 'build-a',
            'imageDigest': 'sha256:' + '1' * 64,
            'compatibilityFingerprint': 'a' * 64,
            'sdkPatchDigest': '2' * 64,
            'builtinInventoryDigest': '3' * 64,
            'modelCacheSelectionDigest': '4' * 64,
            'checkpointCodec': 'port-runtime-checkpoint-v1',
        }
        target = {**source, 'buildId': 'build-b', 'imageDigest': 'sha256:' + '4' * 64}
        fleet.require_compatible(source, target)
        for field in ('compatibilityFingerprint', 'sdkPatchDigest', 'builtinInventoryDigest', 'checkpointCodec', 'protocolRevision'):
            with self.subTest(field=field):
                mismatch = {**target, field: 'different'}
                with self.assertRaises(fleet.FleetError):
                    fleet.require_compatible(source, mismatch)

    def test_actual_worker_inventory_producer_crosses_infra_without_digest_rewrite(self):
        worker = Path(os.environ.get('RUNTIME_WORKER_SOURCE', ROOT.parents[2] / 'voice-agent/.worktrees/feat-runtime-ha'))
        producer = worker / 'src/runtime-build-inventory.ts'
        loader = worker / 'node_modules/tsx/dist/loader.mjs'
        with tempfile.TemporaryDirectory(prefix='port-ha-inventory-boundary-') as temporary:
            fixture = Path(temporary)
            for directory in ('patches', 'dist', 'assets', 'model-cache'):
                (fixture / directory).mkdir()
            (fixture / 'patches/@livekit+agents+1.9.1.patch').write_text('isolated byte fixture, not a production SDK patch')
            (fixture / 'dist/fixture.js').write_text('export const fixture = true;')
            (fixture / 'assets/fixture.bin').write_bytes(b'isolated-inventory-bytes')
            (fixture / 'model-cache/fixture-model.bin').write_bytes(b'isolated-model-inventory-bytes')
            (fixture / 'package-lock.json').write_text('{\"fixture\":true}')
            (fixture / 'instrument.mjs').write_text('export {};')
            sdk = fixture / 'node_modules/@livekit/agents'
            inference = fixture / 'node_modules/@livekit/local-inference'
            (sdk / 'resources').mkdir(parents=True)
            inference.mkdir(parents=True)
            for name in ('agents', 'rtc-node', 'agents-plugin-soniox'):
                package = fixture / 'node_modules/@livekit' / name
                (package / 'dist').mkdir(parents=True)
                (package / 'package.json').write_text(json.dumps({
                    'name': '@livekit/' + name,
                    'exports': {'.': {'import': './dist/index.js', 'require': './dist/index.cjs'}},
                }))
                for entry in ('index.js', 'index.cjs'):
                    (package / 'dist' / entry).write_text('synthetic installed SDK inventory bytes; never executed')
            for relative in (
                'agents/dist/ipc/job_inventory.js', 'agents/dist/ipc/job_proc_executor.cjs',
                'rtc-node/dist/room.cjs', 'agents-plugin-soniox/dist/tts.js', 'agents-plugin-soniox/dist/tts.cjs',
            ):
                path = fixture / 'node_modules/@livekit' / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic installed patched execution bytes; never executed')
            for clip in ('hold_music.ogg', 'office-ambience.ogg', 'keyboard-typing.ogg', 'keyboard-typing2.ogg'):
                (sdk / 'resources' / clip).write_text('synthetic inventory resource; never played')
            (inference / 'package.json').write_text('{"main":"index.js"}')
            (inference / 'index.js').write_text('synthetic native wrapper inventory; never executed')
            environment = dict(os.environ)
            for selector in ('HF_HOME', 'HF_HUB_CACHE', 'HUGGINGFACE_HUB_CACHE', 'XDG_CACHE_HOME', 'HOME'):
                environment.pop(selector, None)
            environment['HOME'] = str(fixture / 'home')
            environment['HF_HOME'] = str(fixture / 'model-cache')
            program = (
                'const fs=await import("node:fs/promises");'
                'const suffix=process.platform+"-"+process.arch+(process.platform==="linux"?"-gnu":process.platform==="win32"?"-msvc":"");'
                'const native="node_modules/@livekit/local-inference-"+suffix;'
                'await fs.mkdir(native,{recursive:true});'
                'await fs.writeFile(native+"/package.json",JSON.stringify({main:"model.node"}));'
                'await fs.writeFile(native+"/model.node","synthetic native inventory bytes; never loaded");'
                'const ffi="node_modules/@livekit/rtc-ffi-bindings";'
                'const rtcNative="@livekit/rtc-ffi-bindings-"+suffix;'
                'const rtcEntry="rtc-node."+suffix+".node";'
                'await fs.mkdir(ffi,{recursive:true});'
                'await fs.writeFile(ffi+"/package.json",JSON.stringify({name:"@livekit/rtc-ffi-bindings",main:"index.js",optionalDependencies:{[rtcNative]:"0.12.73"}}));'
                'await fs.writeFile(ffi+"/index.js","synthetic FFI entry inventory bytes; never executed");'
                'await fs.writeFile(ffi+"/native.js","synthetic FFI wrapper inventory bytes; never executed");'
                'await fs.mkdir("node_modules/"+rtcNative,{recursive:true});'
                'await fs.writeFile("node_modules/"+rtcNative+"/package.json",JSON.stringify({name:rtcNative,main:rtcEntry}));'
                'await fs.writeFile("node_modules/"+rtcNative+"/"+rtcEntry,"synthetic RTC native inventory bytes; never loaded");'
                'const {createRuntimeBuildInventory}=await import(' + json.dumps(producer.as_uri()) + ');'
                'process.stdout.write(JSON.stringify(await createRuntimeBuildInventory(\"actual-producer-fixture\")));'
            )
            result = subprocess.run([
                os.environ.get('RUNTIME_NODE_BINARY', 'node'), '--import', loader.as_uri(),
                '--input-type=module', '--eval', program,
            ], cwd=fixture, env=environment, capture_output=True, timeout=30, check=False)
            self.assertEqual(result.returncode, 0, 'actual worker producer prerequisite failed: ' + result.stderr.decode())
            inventory = {**json.loads(result.stdout), 'imageDigest': 'sha256:' + '1' * 64}
        fleet.require_inventory(inventory)
        for field in ('compatibilityFingerprint', 'sdkPatchDigest', 'builtinInventoryDigest', 'modelCacheSelectionDigest'):
            with self.subTest(field=field), self.assertRaises(fleet.FleetError):
                fleet.require_inventory({**inventory, field: 'sha256:' + inventory[field]})
        missing_selection = dict(inventory)
        del missing_selection['modelCacheSelectionDigest']
        with self.assertRaises(fleet.FleetError):
            fleet.require_inventory(missing_selection)

    def test_every_enabling_path_rejects_missing_partial_or_substituted_first_cutover_attestation(self):
        for values in (
            (None, None, None), ('01999999-1111-4111-8111-111111111111', None, None),
            ('invalid-id', 'sha256:' + '9' * 64, datetime.now(timezone.utc).isoformat()),
            ('01999999-1111-4111-8111-111111111111', 'unverified', datetime.now(timezone.utc).isoformat()),
        ):
            for initial, recovery in ((True, False), (False, True), (True, True)):
                with self.subTest(values=values, initial=initial, recovery=recovery):
                    snapshot = registry()
                    pool = snapshot['pools'][0]
                    for name, value in zip(('firstCutoverEvidenceId', 'firstCutoverEvidenceHash', 'firstCutoverAuthorizedAt'), values):
                        pool[name] = value
                    writes = []

                    class Client:
                        def registry(self, *, timeout=10):
                            return snapshot

                        def request(self, method, path, body, *, timeout=10):
                            writes.append(body)
                            raise fleet.FleetError('unit boundary must not be called')

                    with self.assertRaises(fleet.FleetError):
                        fleet.set_admission(Client(), 'a', 2, initial=initial, recovery=recovery, retiring=False, timeout=1)
                    self.assertEqual(writes, [], 'first-cutover proof must precede any enabling PUT')

    def test_retained_first_cutover_does_not_bypass_current_legacy_drain(self):
        snapshot = registry()
        snapshot['legacyDrain']['pendingUsage'] = 1
        writes = []

        class Client:
            def registry(self, *, timeout=10):
                return snapshot

            def request(self, method, path, body, *, timeout=10):
                writes.append(body)
                raise fleet.FleetError('unit boundary must not be called')

        with self.assertRaises(fleet.FleetError):
            fleet.set_admission(Client(), 'a', 2, initial=True, recovery=True, retiring=False, timeout=1)
        self.assertEqual(writes, [])

    def test_identical_fingerprint_does_not_substitute_an_unaccepted_image_incarnation(self):
        inventory = {
            'protocolRevision': 'runtime-recovery-v1', 'buildId': 'accepted-build',
            'imageDigest': 'sha256:' + '1' * 64,
            'compatibilityFingerprint': 'a' * 64,
            'sdkPatchDigest': '2' * 64,
            'builtinInventoryDigest': '3' * 64,
            'modelCacheSelectionDigest': '4' * 64,
            'checkpointCodec': 'port-runtime-checkpoint-v1',
        }
        desired = [{'poolId': 'a', 'replicas': 2, 'imageDigest': inventory['imageDigest']}]
        value = registry()
        for launcher in value['launchers']:
            launcher['compatibilityInventory'] = dict(inventory)
        fleet.require_deployment_inventory(value, [inventory], desired)
        value['launchers'][1]['compatibilityInventory']['imageDigest'] = 'sha256:' + '4' * 64
        with self.assertRaises(fleet.FleetError):
            fleet.require_deployment_inventory(value, [inventory], desired)
        del value['launchers'][1]['compatibilityInventory']
        with self.assertRaises(fleet.FleetError):
            fleet.require_deployment_inventory(value, [inventory], desired)

    def test_rotation_process_exit_does_not_prove_exposed_credentials_were_revoked(self):
        receipt = {
            'environmentId': 'fixture-env', 'scope': 'rotate-exposed-runtime-credentials',
            'commandExitCode': 0, 'providerVerification': 'unproven',
            'credentialClasses': ['livekit-project', 'rag-global'],
        }
        with self.assertRaises(fleet.FleetError):
            fleet.require_rotation_verified(receipt, 'fixture-env')

    def test_successful_noop_rotation_stays_unproven_and_retains_receipt(self):
        class FixtureClient:
            def registry(self):
                return registry()

        with tempfile.TemporaryDirectory(prefix='port-ha-rotation-unit-') as temporary:
            state = Path(temporary)
            approval_path, receipt_path = state / 'approval.json', state / 'receipt.json'
            approval_path.write_text(json.dumps({
                'scope': 'rotate-exposed-runtime-credentials', 'environmentId': 'fixture-env',
                'approvedBy': 'unit-fixture-only',
                'expiresAt': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                'command': [sys.executable, '-c', 'raise SystemExit(0)'],
                'credentialClasses': ['livekit-project', 'rag-global'],
                'resultFile': str(state / 'missing-provider-result.json'),
            }))
            approval_path.chmod(0o600)
            with self.assertRaises(fleet.FleetError):
                fleet.rotation(FixtureClient(), approval_path, 'fixture-env', receipt_path)
            receipt = fleet.load_json(receipt_path, private=True)
            self.assertEqual(receipt['commandExitCode'], 0)
            self.assertEqual(receipt['providerVerification'], 'unproven')

    def test_runtime_rotation_cannot_authorize_original_seal_or_envelope_key_changes(self):
        class FixtureClient:
            def registry(self):
                return registry()

        for target in ('openbao-static-seal', 'envelope-kek', 'envelope-dek'):
            with self.subTest(target=target), tempfile.TemporaryDirectory(prefix='port-ha-rotation-unit-') as temporary:
                state = Path(temporary)
                sentinel = state / 'audited-command-ran'
                approval_path = state / 'approval.json'
                approval_path.write_text(json.dumps({
                    'scope': 'rotate-exposed-runtime-credentials', 'environmentId': 'fixture-env',
                    'approvedBy': 'unit-fixture-only',
                    'expiresAt': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                    'command': [sys.executable, '-c',
                        'from pathlib import Path; Path(' + json.dumps(str(sentinel)) + ').write_text(\"ran\")'],
                    'credentialClasses': [target], 'resultFile': str(state / 'provider-result.json'),
                }))
                approval_path.chmod(0o600)
                with self.assertRaises(fleet.FleetError):
                    fleet.rotation(FixtureClient(), approval_path, 'fixture-env', state / 'receipt.json')
                self.assertFalse(sentinel.exists(), 'reject at-rest key changes before running the command')

    def test_rotation_receipt_requires_each_revocation_and_unchanged_provider_artifacts(self):
        with tempfile.TemporaryDirectory(prefix='port-ha-rotation-unit-') as temporary:
            state = Path(temporary)
            classes = ['livekit-project', 'rag-global']
            observed = datetime.now(timezone.utc).isoformat()
            approval_path = state / 'approval.json'
            result_path = state / 'provider-result.json'
            approval = {
                'scope': 'rotate-exposed-runtime-credentials', 'environmentId': 'fixture-env',
                'approvedBy': 'unit-fixture-only',
                'expiresAt': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                'command': ['/not-executed-by-unit-test'], 'credentialClasses': classes,
                'resultFile': str(result_path),
            }
            approval_path.write_text(json.dumps(approval))
            approval_path.chmod(0o600)
            proofs = []
            for credential_class in classes:
                artifact = state / (credential_class + '.redacted.txt')
                artifact.write_text('unit fixture only: old denied; replacement accepted')
                artifact.chmod(0o600)
                proofs.append({
                    'credentialClass': credential_class, 'provider': 'unit-fixture-only',
                    'receiptId': 'unit-' + credential_class, 'status': 'revoked-and-replaced',
                    'revocationVerified': True, 'replacementVerified': True,
                    'oldCredentialDenied': True, 'newCredentialAccepted': True,
                    'artifacts': [{'path': str(artifact), 'sha256': fleet.file_hash(artifact), 'redacted': True}],
                })
            result = {
                'schemaVersion': 1, 'scope': approval['scope'], 'environmentId': 'fixture-env',
                'credentialClasses': classes, 'status': 'observed', 'observedAt': observed,
                'providerReceipts': proofs,
            }
            result_path.write_text(json.dumps(result))
            result_path.chmod(0o600)
            receipt = {
                'schemaVersion': 1, 'scope': approval['scope'], 'environmentId': 'fixture-env',
                'approvalPath': str(approval_path), 'approvalSha256': fleet.file_hash(approval_path),
                'credentialClasses': classes, 'observedAt': observed, 'commandExitCode': 0,
                'providerVerification': 'observed',
                'resultFile': str(result_path), 'resultSha256': fleet.file_hash(result_path),
            }
            fleet.require_rotation_verified(receipt, 'fixture-env')
            historical = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            approval['expiresAt'] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            approval_path.write_text(json.dumps(approval))
            result['observedAt'] = historical
            result_path.write_text(json.dumps(result))
            receipt.update({'observedAt': historical, 'approvalSha256': fleet.file_hash(approval_path),
                'resultSha256': fleet.file_hash(result_path)})
            # A retained originally valid approval is not a request to rotate again.
            fleet.require_rotation_verified(receipt, 'fixture-env')
            with self.assertRaises(fleet.FleetError):
                fleet.require_rotation_verified(receipt, 'another-environment')
            removed = proofs.pop()
            result_path.write_text(json.dumps(result))
            receipt['resultSha256'] = fleet.file_hash(result_path)
            with self.assertRaises(fleet.FleetError):
                fleet.require_rotation_verified(receipt, 'fixture-env')
            proofs.append(removed)
            result_path.write_text(json.dumps(result))
            receipt['resultSha256'] = fleet.file_hash(result_path)
            (state / 'livekit-project.redacted.txt').write_text('changed artifact after verification')
            with self.assertRaises(fleet.FleetError):
                fleet.require_rotation_verified(receipt, 'fixture-env')

    def test_external_unknown_gate_cannot_be_converted_to_pass_by_a_template(self):
        with tempfile.TemporaryDirectory(prefix='port-ha-unknown-gate-unit-') as temporary:
            evidence = isolated_acceptance_evidence(Path(temporary), isolated_inventory())
            evidence['gates']['cloudAssignmentFreshness'] = {'status': 'unproven', 'scenario': None, 'artifacts': []}
            with self.assertRaises(fleet.FleetError):
                fleet.require_external_gates(evidence, 'isolated-test')

    def test_cutover_gate_reader_rejects_missing_or_oversized_token_clock_bounds(self):
        with tempfile.TemporaryDirectory(prefix='port-ha-cutoff-bound-unit-') as temporary:
            evidence = isolated_acceptance_evidence(Path(temporary), isolated_inventory())
            del evidence['gates']['cloudTokenRefreshRevocation']['clockBound']
            with self.assertRaises(fleet.FleetError):
                fleet.require_external_gates(evidence, 'isolated-test')
            gate = evidence['gates']['cloudTokenRefreshRevocation']
            gate['clockBound'] = {'maxProviderClockSkewMs': 40000, 'maxRevocationRpcDurationMs': 20001}
            with self.assertRaises(fleet.FleetError):
                fleet.require_external_gates(evidence, 'isolated-test')
            gate['clockBound'] = {'maxProviderClockSkewMs': 40000, 'maxRevocationRpcDurationMs': 20000}
            fleet.require_external_gates(evidence, 'isolated-test')

    def test_producer_format_structural_evidence_is_consumed_without_provider_claim(self):
        with tempfile.TemporaryDirectory(prefix='port-ha-evidence-reader-unit-') as temporary:
            build = isolated_inventory()
            evidence = isolated_acceptance_evidence(Path(temporary), build)
            fleet.require_external_gates(evidence, 'isolated-test', project_id='isolated-cloud-project', builds=[build])

    def test_observed_gate_timestamps_are_required_valid_utc_and_not_future(self):
        with tempfile.TemporaryDirectory(prefix='port-ha-gate-time-unit-') as temporary:
            evidence = isolated_acceptance_evidence(Path(temporary), isolated_inventory())
            invalid_times = (
                None, 'not-a-timestamp', '2020-01-01T00:00:00',
                '2020-01-01T00:00:00+00:00', '2020-01-01 00:00:00Z',
                fleet.utc_iso(datetime.now(timezone.utc) + timedelta(hours=1)),
            )
            for name in fleet.EXTERNAL_GATES:
                for observed_at in invalid_times:
                    with self.subTest(gate=name, observed_at=observed_at):
                        invalid = copy.deepcopy(evidence)
                        if observed_at is None:
                            del invalid['gates'][name]['observedAt']
                        else:
                            invalid['gates'][name]['observedAt'] = observed_at
                        with self.assertRaises(fleet.FleetError):
                            fleet.require_external_gates(invalid, 'isolated-test')

    def test_observed_gates_require_approval_sha256_and_two_complete_hashed_artifacts(self):
        faults = (
            'missing-approval', 'malformed-approval', 'bare-approval', 'non-string-scenario',
            'missing-result-artifact', 'missing-provider-artifact', 'missing-artifact-path',
            'empty-artifact-path', 'missing-artifact-hash', 'malformed-artifact-hash',
        )
        with tempfile.TemporaryDirectory(prefix='port-ha-gate-structure-unit-') as temporary:
            evidence = isolated_acceptance_evidence(Path(temporary), isolated_inventory())
            for name in fleet.EXTERNAL_GATES:
                for fault in faults:
                    with self.subTest(gate=name, fault=fault):
                        invalid = copy.deepcopy(evidence)
                        gate = invalid['gates'][name]
                        if fault == 'missing-approval':
                            del gate['approvalSha256']
                        elif fault == 'malformed-approval':
                            gate['approvalSha256'] = 'sha256:unverified'
                        elif fault == 'bare-approval':
                            gate['approvalSha256'] = gate['approvalSha256'].removeprefix('sha256:')
                        elif fault == 'non-string-scenario':
                            gate['scenario'] = ['not-a-scenario']
                        elif fault == 'missing-result-artifact':
                            gate['artifacts'].pop(0)
                        elif fault == 'missing-provider-artifact':
                            gate['artifacts'].pop()
                        elif fault == 'missing-artifact-path':
                            del gate['artifacts'][1]['path']
                        elif fault == 'empty-artifact-path':
                            gate['artifacts'][1]['path'] = ''
                        elif fault == 'missing-artifact-hash':
                            del gate['artifacts'][1]['sha256']
                        else:
                            gate['artifacts'][1]['sha256'] = 'unverified'
                        with self.assertRaises(fleet.FleetError):
                            fleet.require_external_gates(invalid, 'isolated-test')

    def test_external_evidence_schema_requires_project_builds_and_utc_envelope(self):
        with tempfile.TemporaryDirectory(prefix='port-ha-envelope-schema-unit-') as temporary:
            evidence = isolated_acceptance_evidence(Path(temporary), isolated_inventory())
            variants = (
                ('schemaVersion', True), ('projectId', None), ('projectId', ''),
                ('builds', None), ('builds', []),
                ('observedAt', evidence['observedAt'].replace('Z', '+00:00')),
                ('expiresAt', evidence['expiresAt'].replace('Z', '+00:00')),
            )
            for field, value in variants:
                with self.subTest(field=field, value=value):
                    invalid = copy.deepcopy(evidence)
                    if value is None:
                        del invalid[field]
                    else:
                        invalid[field] = value
                    with self.assertRaises(fleet.FleetError):
                        fleet.require_external_gates(invalid, 'isolated-test')

    def test_external_evidence_requires_complete_published_build_inventory(self):
        with tempfile.TemporaryDirectory(prefix='port-ha-evidence-build-schema-unit-') as temporary:
            evidence = isolated_acceptance_evidence(Path(temporary), isolated_inventory())
            for field in (
                'protocolRevision', 'buildId', 'imageDigest', 'compatibilityFingerprint',
                'sdkPatchDigest', 'builtinInventoryDigest', 'modelCacheSelectionDigest', 'checkpointCodec',
            ):
                with self.subTest(missing_field=field):
                    invalid = copy.deepcopy(evidence)
                    del invalid['builds'][0][field]
                    with self.assertRaises(fleet.FleetError):
                        fleet.require_external_gates(invalid, 'isolated-test')
            for field, value in (
                ('imageDigest', 'moving-tag'), ('checkpointCodec', 'legacy-codec'),
                ('compatibilityFingerprint', 'sha256:' + 'a' * 64),
            ):
                with self.subTest(field=field, value=value):
                    invalid = copy.deepcopy(evidence)
                    invalid['builds'][0][field] = value
                    with self.assertRaises(fleet.FleetError):
                        fleet.require_external_gates(invalid, 'isolated-test')

    def test_external_evidence_retains_original_binding_expiry_and_artifact_integrity(self):
        with tempfile.TemporaryDirectory(prefix='port-ha-evidence-binding-unit-') as temporary:
            state, build = Path(temporary), isolated_inventory()
            evidence = isolated_acceptance_evidence(state, build)
            for field, value in (
                ('provider', 'livekit-oss'), ('environmentId', 'other-environment'),
                ('projectId', 'other-project'), ('protocolRevision', 'legacy'),
                ('evidenceId', 'invalid-id'),
                ('observedAt', fleet.utc_iso(datetime.now(timezone.utc) + timedelta(hours=1))),
                ('expiresAt', fleet.utc_iso(datetime.now(timezone.utc) - timedelta(seconds=1))),
                ('builds', [{**build, 'imageDigest': 'sha256:' + '5' * 64}]),
            ):
                with self.subTest(field=field):
                    invalid = copy.deepcopy(evidence)
                    invalid[field] = value
                    with self.assertRaises(fleet.FleetError):
                        fleet.require_external_gates(invalid, 'isolated-test', project_id='isolated-cloud-project', builds=[build])
            (state / 'cloudAssignmentFreshness.redacted.txt').write_text('changed after evidence was hashed')
            with self.assertRaises(fleet.FleetError):
                fleet.require_external_gates(evidence, 'isolated-test', project_id='isolated-cloud-project', builds=[build])

    def test_generic_ack_inventory_and_retirement_keep_single_replica_scope(self):
        snapshot = registry()
        snapshot['launchers'] = snapshot['launchers'][:1]
        build = isolated_inventory()
        snapshot['launchers'][0]['compatibilityInventory'] = dict(build)
        fleet.require_pool_ack(snapshot, 'a', 1)
        fleet.require_deployment_inventory(snapshot, [build], [
            {'poolId': 'a', 'replicas': 1, 'imageDigest': build['imageDigest']},
        ])
        fleet.require_retirable(snapshot, 'a', 1)


class FleetHaPreflightTests(unittest.TestCase):
    INVALID_TOPOLOGIES = (
        ('single-pool', [('pool-a', 'a', 2)]),
        ('one-plus-one', [('pool-a', 'a', 1), ('pool-b', 'b', 1)]),
        ('duplicate-role', [('pool-a', 'a', 2), ('pool-a', 'b', 2)]),
        ('extra-role', [('pool-a', 'a', 2), ('pool-b', 'b', 2), ('pool-a', 'c', 2)]),
        ('same-pool-id', [('pool-a', 'a', 2), ('pool-b', 'a', 2)]),
    )

    @classmethod
    def setUpClass(cls):
        cls.tls_directory = tempfile.TemporaryDirectory(prefix='port-ha-preflight-tls-unit-')
        cls.addClassCleanup(cls.tls_directory.cleanup)
        cls.tls = Path(cls.tls_directory.name) / 'tls'
        result = subprocess.run(
            ['bash', str(ROOT / 'scripts/openbao-tls.sh')],
            env={'PATH': os.environ['PATH'], 'OPENBAO_TLS_DIR': str(cls.tls)},
            capture_output=True, timeout=30, check=False,
        )
        if result.returncode:
            raise RuntimeError('isolated loopback TLS prerequisite failed: ' + result.stderr.decode())

    def run_cli(self, state, config, evidence, command='preflight'):
        """Real CLI and TLS/HTTP loopback boundaries; no native ACK or provider proof."""
        build = config['builds'][0]
        inventory_path, evidence_path, config_path = state / 'inventory.json', state / 'evidence.json', state / 'fleet.json'
        for path, value in ((inventory_path, build), (evidence_path, evidence), (config_path, config)):
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        snapshot = registry()
        pool_template, launcher_template = snapshot['pools'][0], snapshot['launchers'][0]
        snapshot['pools'], snapshot['launchers'] = [], []
        pool_ids = set()
        for service in config['services']:
            pool_id = service['poolId']
            if pool_id in pool_ids:
                continue
            pool_ids.add(pool_id)
            snapshot['pools'].append({
                **copy.deepcopy(pool_template), 'poolId': pool_id,
                'firstCutoverEvidenceId': evidence['evidenceId'],
                'firstCutoverEvidenceHash': fixture_hash(evidence_path),
            })
            snapshot['launchers'].extend({
                **copy.deepcopy(launcher_template), 'poolId': pool_id,
                'launcherId': pool_id + '-' + str(index), 'incarnation': pool_id + '-inc-' + str(index),
                'compatibilityInventory': dict(build),
            } for index in range(service['replicas']))
        writes, health_reads = [], []

        class RegistryHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                self.send_response(200 if self.path == '/api/v1/internal/runtime-launchers' else 404)
                self.end_headers()
                self.wfile.write(json.dumps({'statusCode': 200, 'message': 'OK', 'data': snapshot}).encode())

            def do_PUT(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                writes.append((self.path, body))
                # The fixture never manufactures a durable revision or an incarnation ACK.
                self.send_response(503)
                self.end_headers()

        class BaoHealthHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                health_reads.append(self.path)
                self.send_response(200 if self.path == '/v1/sys/health' else 404)
                self.end_headers()
                self.wfile.write(json.dumps({
                    'initialized': True, 'sealed': False,
                    'cluster_id': 'isolated-loopback-cluster-NOT-key-proof',
                }).encode())

        control_server = ThreadingHTTPServer(('127.0.0.1', 0), RegistryHandler)
        bao_server = ThreadingHTTPServer(('127.0.0.1', 0), BaoHealthHandler)
        self.addCleanup(control_server.server_close)
        self.addCleanup(bao_server.server_close)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.tls / 'server.crt', self.tls / 'server.key')
        bao_server.socket = context.wrap_socket(bao_server.socket, server_side=True)
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (control_server, bao_server)]
        for thread in threads:
            thread.start()
        try:
            arguments = [
                sys.executable, str(ROOT / 'scripts/runtime_fleet.py'),
                '--api', 'http://127.0.0.1:' + str(control_server.server_port) + '/api/v1',
                '--allow-loopback', '--environment-id', 'isolated-test', command,
                '--evidence', str(evidence_path), '--project-id', 'isolated-cloud-project',
                '--inventory', str(inventory_path), '--fleet-config', str(config_path),
                '--bao-address', 'https://127.0.0.1:' + str(bao_server.server_port),
                '--bao-ca', str(self.tls / 'ca.crt'), '--bao-cluster-id', 'isolated-loopback-cluster-NOT-key-proof',
            ]
            if command == 'cutover':
                arguments.extend([
                    '--pool', config['services'][0]['poolId'], '--replicas', str(config['services'][0]['replicas']),
                    '--rotation-receipt', str(isolated_rotation_receipt(state)),
                    '--first-cutover-evidence', str(evidence_path),
                ])
            result = subprocess.run(arguments, cwd=ROOT,
                env={'PATH': os.environ['PATH'], 'RUNTIME_CONTROL_KEY': 'isolated-loopback-fixture-key'},
                capture_output=True, timeout=15, check=False)
            return result, writes, health_reads
        finally:
            control_server.shutdown()
            bao_server.shutdown()
            for thread in threads:
                thread.join()

    def test_preflight_rejects_invalid_ha_topology_before_acceptance(self):
        with tempfile.TemporaryDirectory(prefix='port-ha-topology-preflight-unit-') as temporary:
            state = Path(temporary)
            evidence = isolated_acceptance_evidence(state, isolated_inventory())
            for name, topology in self.INVALID_TOPOLOGIES:
                with self.subTest(topology=name):
                    result, writes, health_reads = self.run_cli(state, isolated_fleet_config(topology), evidence)
                    self.assertEqual(result.returncode, 2, result.stdout.decode() + result.stderr.decode())
                    self.assertEqual(writes, [], 'invalid full HA topology must never enable admission')
                    self.assertEqual(health_reads, [], 'invalid topology must block before physical readiness probes')

    def test_cutover_rejects_invalid_ha_topology_without_any_enabling_put(self):
        with tempfile.TemporaryDirectory(prefix='port-ha-topology-cutover-unit-') as temporary:
            state = Path(temporary)
            evidence = isolated_acceptance_evidence(state, isolated_inventory())
            for name, topology in self.INVALID_TOPOLOGIES:
                with self.subTest(topology=name):
                    result, writes, health_reads = self.run_cli(state, isolated_fleet_config(topology), evidence, 'cutover')
                    self.assertEqual(result.returncode, 2, result.stdout.decode() + result.stderr.decode())
                    self.assertEqual(writes, [], 'invalid full HA topology reached an enabling PUT')
                    self.assertEqual(health_reads, [], 'invalid topology must block before physical readiness probes')

    def test_preflight_accepts_two_distinct_two_replica_pools_with_structural_evidence_only(self):
        with tempfile.TemporaryDirectory(prefix='port-ha-valid-preflight-unit-') as temporary:
            state = Path(temporary)
            evidence = isolated_acceptance_evidence(state, isolated_inventory())
            result, writes, health_reads = self.run_cli(
                state, isolated_fleet_config([('pool-a', 'a', 2), ('pool-b', 'b', 2)]), evidence)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(json.loads(result.stdout)['preflight'], 'observed')
            self.assertEqual(writes, [])
            self.assertEqual(health_reads, ['/v1/sys/health'])


class FleetHttpTests(unittest.TestCase):
    def test_control_rejects_bare_payload_or_inconsistent_success_envelope(self):
        snapshot = registry()
        reply = snapshot

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(reply).encode())

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = fleet.FleetClient('http://127.0.0.1:' + str(server.server_port) + '/api/v1',
                'fixture-control-key', allow_loopback=True)
            for reply in (
                snapshot,
                {'statusCode': 201, 'data': snapshot},
                {'statusCode': '200', 'data': snapshot},
                {'statusCode': 200, 'data': []},
                {'statusCode': 200},
            ):
                with self.subTest(reply=reply), self.assertRaises(fleet.FleetError):
                    client.registry()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_withdrawal_is_reversible_and_control_is_cas_not_one_url_ack(self):
        snapshot = registry()
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'statusCode': 200, 'message': 'OK', 'data': snapshot}).encode())

            def do_PUT(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, self.headers.get('x-runtime-control-key'), body))
                pool = snapshot['pools'][0]
                if body['expectedRevision'] != pool['revision']:
                    self.send_response(409)
                    self.end_headers()
                    return
                pool.update({key: body[key] for key in ('initialAccepting', 'recoveryAccepting', 'retiring')})
                pool['revision'] += 1
                for launcher in snapshot['launchers']:
                    launcher['ackRevision'] = pool['revision']
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'statusCode': 200, 'message': 'OK', 'data': pool}).encode())

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = fleet.FleetClient('http://127.0.0.1:' + str(server.server_port) + '/api/v1', 'fixture-control-key', allow_loopback=True)
            fleet.set_admission(client, 'a', 2, initial=False, recovery=False, retiring=False, timeout=1)
            fleet.set_admission(client, 'a', 2, initial=True, recovery=True, retiring=False, timeout=1)
            self.assertEqual([item[2]['expectedRevision'] for item in requests], [8, 9])
            self.assertEqual(requests[0][0], '/api/v1/internal/runtime-launchers/pools/a/admission')
            self.assertEqual(requests[0][1], 'fixture-control-key')
            self.assertTrue(snapshot['pools'][0]['initialAccepting'])
            self.assertFalse(snapshot['pools'][0]['retiring'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_rollback_does_not_mutate_when_same_fingerprint_has_an_unaccepted_image(self):
        snapshot = registry()
        snapshot['pools'].append({**snapshot['pools'][0], 'poolId': 'b'})
        source = {
            'protocolRevision': 'runtime-recovery-v1', 'buildId': 'source-build',
            'imageDigest': 'sha256:' + '1' * 64,
            'compatibilityFingerprint': 'a' * 64,
            'sdkPatchDigest': '2' * 64,
            'builtinInventoryDigest': '3' * 64,
            'modelCacheSelectionDigest': '4' * 64,
            'checkpointCodec': 'port-runtime-checkpoint-v1',
        }
        target = {**source, 'buildId': 'target-build', 'imageDigest': 'sha256:' + '4' * 64}
        source_launchers = snapshot['launchers']
        snapshot['launchers'] = [
            {**launcher, 'poolId': pool_id, 'launcherId': pool_id + launcher['launcherId'],
                'incarnation': pool_id + launcher['incarnation'], 'compatibilityInventory': dict(inventory)}
            for pool_id, inventory in (('a', source), ('b', target)) for launcher in source_launchers
        ]
        snapshot['launchers'][-1]['compatibilityInventory']['imageDigest'] = 'sha256:' + '5' * 64
        writes = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'statusCode': 200, 'message': 'OK', 'data': snapshot}).encode())

            def do_PUT(self):
                writes.append(self.path)
                self.send_response(409)
                self.end_headers()

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='port-ha-rollback-unit-') as temporary:
                source_path, target_path = Path(temporary) / 'source.json', Path(temporary) / 'target.json'
                source_path.write_text(json.dumps(source))
                target_path.write_text(json.dumps(target))
                with patch.dict('os.environ', {'RUNTIME_CONTROL_KEY': 'fixture-control-key'}), \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    result = fleet.main([
                        '--api', 'http://127.0.0.1:' + str(server.server_port) + '/api/v1',
                        '--allow-loopback', 'rollback', '--source-pool', 'a', '--target-pool', 'b',
                        '--source-replicas', '2', '--target-replicas', '2',
                        '--source-inventory', str(source_path), '--target-inventory', str(target_path),
                    ])
                self.assertEqual(result, 2)
                self.assertEqual(writes, [], 'reject unknown image before changing either pool')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
