#!/usr/bin/env python3
"""Replica-fleet control. A desired revision is never an incarnation ACK."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import re
import ssl
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

PROTOCOL = 'runtime-recovery-v1'
RETIREMENT_COUNTERS = (
    'activeExecutions', 'pendingAttempts', 'recoverableSessions', 'unknownOperations',
    'pendingControlEffects', 'pendingLaunches', 'receiptObligations', 'unacknowledgedIncarnations',
)
LEGACY_COUNTERS = ('activeSessions', 'pendingAdmissions', 'pendingUsage', 'legacyCredentialRoutes', 'legacyInflightOperations')
JOB_INVENTORY_FIELDS = ('pendingJobIds', 'assignedJobIds', 'launchingJobIds', 'runningJobIds', 'cancelledJobIds')
EXTERNAL_GATES = (
    'cloudAssignmentFreshness', 'cloudAbsentParticipantRevocation', 'cloudTokenRefreshRevocation',
    'railwayPlacement', 'railwaySignalGrace', 'postgresqlFailoverDurability',
    'redisStateLoss', 'productionSealAvailability',
)
COMPATIBILITY_FIELDS = ('protocolRevision', 'compatibilityFingerprint', 'sdkPatchDigest', 'builtinInventoryDigest', 'checkpointCodec')
DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')
CODE_HASH = re.compile(r'^[0-9a-f]{64}$')
EVIDENCE_TIMESTAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z')
MAX_JSON_BYTES = 4 * 1024 * 1024
CHECKPOINT_CODEC = 'port-runtime-checkpoint-v1'
ROTATION_SCOPE = 'rotate-exposed-runtime-credentials'
ROTATION_CREDENTIAL_CLASSES = frozenset((
    'livekit-project', 'rag-global', 'internal-api-global', 'api-tool-provider',
    'sms-provider', 'mcp-provider', 'a2a-provider', 'speech-provider', 'model-provider',
))


class FleetError(RuntimeError):
    """An operator-safe error; never includes server response bodies or secrets."""


class FleetHTTPError(FleetError):
    def __init__(self, status: int):
        self.status = status
        super().__init__('runtime control request failed HTTP ' + str(status))


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise FleetError('missing authoritative timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as error:
        raise FleetError('invalid authoritative timestamp') from error
    if parsed.tzinfo is None:
        raise FleetError('timestamp must include a timezone')
    return parsed.astimezone(timezone.utc)


def evidence_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not EVIDENCE_TIMESTAMP.fullmatch(value):
        raise FleetError('acceptance evidence requires a valid UTC date-time ending in Z')
    return timestamp(value)


def utc_iso(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise FleetError('UTC evidence writer requires a timezone-aware timestamp')
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def fresh(registry: dict, *, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if registry.get('protocolRevision') != PROTOCOL:
        raise FleetError('unsupported registry protocol; no legacy-owner fallback')
    age = (now - timestamp(registry.get('observedAt'))).total_seconds()
    if age < -5 or age > 15:
        raise FleetError('registry observation is stale or server clock is untrusted')
    return now


def find_pool(registry: dict, pool_id: str) -> dict:
    pools = registry.get('pools')
    if not isinstance(pools, list):
        raise FleetError('pool registry is unavailable')
    matches = [pool for pool in pools if isinstance(pool, dict) and pool.get('poolId') == pool_id]
    if len(matches) != 1:
        raise FleetError('pool is missing or ambiguous: ' + pool_id)
    pool = matches[0]
    if type(pool.get('revision')) is not int or pool['revision'] < 1:
        raise FleetError('pool control revision is unknown')
    for field in ('initialAccepting', 'recoveryAccepting', 'retiring'):
        if type(pool.get(field)) is not bool:
            raise FleetError('pool admission state is unknown: ' + field)
    return pool


def require_pool_ack(registry: dict, pool_id: str, expected_replicas: int, *, now: datetime | None = None) -> list[dict]:
    now = fresh(registry, now=now)
    if type(expected_replicas) is not int or expected_replicas < 1:
        raise FleetError('explicit positive expected replica count is required')
    pool = find_pool(registry, pool_id)
    launchers = registry.get('launchers')
    if not isinstance(launchers, list):
        raise FleetError('per-incarnation registry is unavailable')
    live = []
    for launcher in launchers:
        if not isinstance(launcher, dict) or launcher.get('poolId') != pool_id:
            continue
        if timestamp(launcher.get('leaseExpiresAt')) > now:
            live.append(launcher)
    if len(live) != expected_replicas:
        raise FleetError(f'pool {pool_id} has {len(live)} fresh incarnations, expected {expected_replicas}')
    identities = set()
    for launcher in live:
        identity = (launcher.get('launcherId'), launcher.get('incarnation'))
        if any(not isinstance(value, str) or not value for value in identity) or identity[0] in identities:
            raise FleetError('launcher incarnation identity is missing or ambiguous')
        identities.add(identity[0])
        if type(launcher.get('ackRevision')) is not int or launcher['ackRevision'] != pool['revision']:
            raise FleetError('not every launcher incarnation acknowledged the desired revision')
        if (pool['initialAccepting'] or pool['recoveryAccepting']) and launcher.get('ready') is not True:
            raise FleetError('accepting pool has an unready incarnation')
        fingerprint = pool.get('compatibilityFingerprint')
        if not isinstance(fingerprint, str) or not CODE_HASH.fullmatch(fingerprint) or launcher.get('compatibilityFingerprint') != fingerprint:
            raise FleetError('pool has an unsupported or mixed compatibility inventory')
        inventory = launcher.get('inventory')
        if not isinstance(inventory, dict) or any(not isinstance(inventory.get(name), list) for name in JOB_INVENTORY_FIELDS):
            raise FleetError('pending/assigned/launching/running inventory is incomplete')
    return live


def require_zero_census(census: object, names: tuple[str, ...], label: str) -> None:
    if not isinstance(census, dict):
        raise FleetError(label + ' census is unavailable')
    for name in names:
        value = census.get(name)
        if type(value) is not int or value < 0:
            raise FleetError(label + ' census is unknown: ' + name)
        if value:
            raise FleetError(label + ' is blocked by ' + name + ': ' + str(value))


def require_retirable(registry: dict, pool_id: str, expected_replicas: int) -> None:
    live = require_pool_ack(registry, pool_id, expected_replicas)
    pool = find_pool(registry, pool_id)
    if pool['initialAccepting'] or pool['recoveryAccepting']:
        raise FleetError('withdraw both initial and recovery admission before retirement')
    require_zero_census(pool.get('retirementBlockers'), RETIREMENT_COUNTERS, 'pool retirement')
    for launcher in live:
        for name in JOB_INVENTORY_FIELDS[:-1]:
            if launcher['inventory'][name]:
                raise FleetError('pool retirement has pending or running work: ' + name)


def require_legacy_drained(registry: dict) -> None:
    fresh(registry)
    require_zero_census(registry.get('legacyDrain'), LEGACY_COUNTERS, 'pre-HA legacy drain')


def require_registered_inventory(inventory: dict) -> None:
    if not isinstance(inventory, dict) or inventory.get('protocolRevision') != PROTOCOL:
        raise FleetError('image is not a mandatory-authority HA build')
    if inventory.get('checkpointCodec') != CHECKPOINT_CODEC:
        raise FleetError('unsupported checkpoint codec; no compatibility fallback')
    if not isinstance(inventory.get('buildId'), str) or not inventory['buildId']:
        raise FleetError('immutable image inventory is missing buildId')
    if not isinstance(inventory.get('imageDigest'), str) or not DIGEST.fullmatch(inventory['imageDigest']):
        raise FleetError('immutable image inventory has invalid imageDigest')
    for field in ('compatibilityFingerprint', 'sdkPatchDigest', 'builtinInventoryDigest'):
        if not isinstance(inventory.get(field), str) or not CODE_HASH.fullmatch(inventory[field]):
            raise FleetError('immutable code inventory requires canonical bare 64hex ' + field)


def require_inventory(inventory: dict) -> None:
    require_registered_inventory(inventory)
    selection = inventory.get('modelCacheSelectionDigest')
    if not isinstance(selection, str) or not CODE_HASH.fullmatch(selection):
        raise FleetError('baked inventory requires exact effective model-cache selection digest')


def require_compatible(source: dict, target: dict) -> None:
    require_inventory(source)
    require_inventory(target)
    for field in COMPATIBILITY_FIELDS:
        if source[field] != target[field]:
            raise FleetError('rollback would reinterpret an incompatible recovery lineage: ' + field)


def require_pool_inventory(registry: dict, pool_id: str, replicas: int, expected: dict) -> list[dict]:
    require_registered_inventory(expected)
    live = require_pool_ack(registry, pool_id, replicas)
    for launcher in live:
        actual = launcher.get('compatibilityInventory')
        require_registered_inventory(actual)
        if any(actual.get(name) != expected[name] for name in (*COMPATIBILITY_FIELDS, 'buildId', 'imageDigest')):
            raise FleetError('an ACKed incarnation does not match the accepted deployed image/build inventory')
    return live


def require_deployment_inventory(registry: dict, builds: list[dict], desired_pools: list[dict]) -> None:
    fields = (*COMPATIBILITY_FIELDS, 'buildId', 'imageDigest', 'modelCacheSelectionDigest')
    by_digest = {}
    for build in builds:
        require_inventory(build)
        selected = {name: build[name] for name in fields}
        if build['imageDigest'] in by_digest and by_digest[build['imageDigest']] != selected:
            raise FleetError('image digest has conflicting accepted build inventories')
        by_digest[build['imageDigest']] = selected
    if not desired_pools:
        raise FleetError('explicit desired fleet configuration is required')
    expected_pool_ids = set()
    for desired in desired_pools:
        pool_id = desired.get('poolId')
        if not isinstance(pool_id, str) or not pool_id or pool_id in expected_pool_ids:
            raise FleetError('desired fleet pool identity is missing or ambiguous')
        expected_pool_ids.add(pool_id)
        expected = by_digest.get(desired.get('imageDigest'))
        if expected is None:
            raise FleetError('desired pool image is not in the accepted immutable build inventory')
        require_pool_inventory(registry, pool_id, desired.get('replicas'), expected)
    now = fresh(registry)
    if any(launcher.get('poolId') not in expected_pool_ids and timestamp(launcher.get('leaseExpiresAt')) > now
           for launcher in registry['launchers']):
        raise FleetError('fresh incarnation exists outside the explicitly approved replica fleet')


def require_data_ready(registry: dict) -> None:
    fresh(registry)
    readiness = registry.get('readiness')
    if not isinstance(readiness, dict) or any(readiness.get(name) is not True for name in ('database', 'redis', 'keyring')):
        raise FleetError('SQL/Redis/keyring runtime readiness is not proven; admission must remain withdrawn')


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise FleetError('evidence must be a regular file')
            for chunk in iter(lambda: source.read(65536), b''):
                digest.update(chunk)
    except OSError as error:
        raise FleetError('evidence artifact is unavailable or unsafe') from error
    return 'sha256:' + digest.hexdigest()


def load_json(path: Path, *, private: bool = False) -> dict:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or (private and (metadata.st_uid != os.getuid() or metadata.st_mode & 0o077)):
                raise FleetError('approval/config must be an owner-only regular file')
            raw = source.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise FleetError('JSON artifact is too large')
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FleetError('JSON artifact is unavailable or invalid') from error
    if not isinstance(value, dict):
        raise FleetError('JSON artifact must be an object')
    return value


def write_new_json(path: Path, value: object) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(canonical_json(value) + b'\n')
            output.flush()
            os.fsync(output.fileno())
    except OSError as error:
        raise FleetError('refusing to replace an existing or unsafe output file') from error


def require_clock_bound(bound: object) -> dict:
    fields = ('maxProviderClockSkewMs', 'maxRevocationRpcDurationMs')
    if not isinstance(bound, dict) or set(bound) != set(fields):
        raise FleetError('token revocation proof requires the exact observed provider clock/RPC bounds')
    skew, duration = (bound[name] for name in fields)
    if type(skew) is not int or type(duration) is not int or skew < 0 or duration < 1 or skew + duration > 60000:
        raise FleetError('observed provider clock skew plus revoke RPC duration must fit the 60000ms cutoff window')
    return {name: bound[name] for name in fields}


def require_external_gates(evidence: dict, environment_id: str, *, project_id: str | None = None, builds: list[dict] | None = None) -> None:
    now = datetime.now(timezone.utc)
    if evidence.get('provider') != 'livekit-cloud':
        raise FleetError('local/OSS fixture results cannot stand in for Cloud/provider acceptance')
    if (type(evidence.get('schemaVersion')) is not int or evidence['schemaVersion'] != 1
            or evidence.get('protocolRevision') != PROTOCOL
            or not isinstance(evidence.get('environmentId'), str) or not evidence['environmentId']
            or evidence['environmentId'] != environment_id):
        raise FleetError('acceptance evidence is not bound to this environment/protocol')
    try:
        uuid.UUID(evidence.get('evidenceId', ''))
    except (ValueError, TypeError, AttributeError) as error:
        raise FleetError('acceptance evidence ID is invalid') from error
    if not isinstance(evidence.get('projectId'), str) or not evidence['projectId']:
        raise FleetError('acceptance evidence requires the provider project identity')
    accepted_builds = evidence.get('builds')
    if not isinstance(accepted_builds, list) or not accepted_builds:
        raise FleetError('acceptance evidence requires immutable build inventories')
    for inventory in accepted_builds:
        require_inventory(inventory)
    if project_id is not None and evidence.get('projectId') != project_id:
        raise FleetError('acceptance evidence belongs to another provider project')
    if evidence_timestamp(evidence.get('observedAt')) > now or evidence_timestamp(evidence.get('expiresAt')) <= now:
        raise FleetError('acceptance evidence is expired or from the future')
    if builds is not None and canonical_json(evidence.get('builds')) != canonical_json(builds):
        raise FleetError('acceptance evidence is not bound to the exact image/SDK/builtin/codec inventory')
    gates = evidence.get('gates')
    if not isinstance(gates, dict):
        raise FleetError('external acceptance gates are unavailable')
    for name in EXTERNAL_GATES:
        gate = gates.get(name)
        if (not isinstance(gate, dict) or gate.get('status') != 'observed'
                or not isinstance(gate.get('scenario'), str) or not gate['scenario']):
            raise FleetError('external acceptance gate remains unproven: ' + name)
        if evidence_timestamp(gate.get('observedAt')) > now:
            raise FleetError('external acceptance gate observation is from the future: ' + name)
        approval_hash = gate.get('approvalSha256')
        if not isinstance(approval_hash, str) or not DIGEST.fullmatch(approval_hash):
            raise FleetError('external acceptance gate requires an approval SHA256: ' + name)
        if name == 'cloudTokenRefreshRevocation':
            require_clock_bound(gate.get('clockBound'))
        artifacts = gate.get('artifacts')
        if not isinstance(artifacts, list) or len(artifacts) < 2:
            raise FleetError('external acceptance gate requires scenario and redacted provider evidence artifacts: ' + name)
        for artifact in artifacts:
            if (not isinstance(artifact, dict) or not isinstance(artifact.get('path'), str) or not artifact['path']
                    or not isinstance(artifact.get('sha256'), str) or not DIGEST.fullmatch(artifact['sha256'])
                    or file_hash(Path(artifact['path'])) != artifact['sha256']):
                raise FleetError('external acceptance evidence hash mismatch: ' + name)


class FleetClient:
    def __init__(self, base_url: str, control_key: str, *, allow_loopback: bool = False):
        parsed = urlsplit(base_url)
        try:
            loopback = parsed.hostname == 'localhost' or ipaddress.ip_address(parsed.hostname or '').is_loopback
        except ValueError:
            loopback = False
        if parsed.scheme != 'https' and not (allow_loopback and parsed.scheme == 'http' and loopback):
            raise FleetError('control URL must use HTTPS (explicit loopback fixture exception only)')
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path.rstrip('/') != '/api/v1':
            raise FleetError('control URL must be an uncredentialed /api/v1 origin')
        if not control_key or any(character.isspace() for character in control_key):
            raise FleetError('RUNTIME_CONTROL_KEY is required')
        self.url = parsed
        self.control_key = control_key

    def request(self, method: str, path: str, body: dict | None = None, *, timeout: float = 10) -> dict:
        if timeout <= 0:
            raise FleetError('runtime control request budget expired')
        deadline = time.monotonic() + timeout
        connection_type = http.client.HTTPSConnection if self.url.scheme == 'https' else http.client.HTTPConnection
        connection = connection_type(self.url.hostname, self.url.port, timeout=timeout)
        data = canonical_json(body) if body is not None else None
        response = None
        try:
            connection.request(method, self.url.path.rstrip('/') + path, body=data, headers={
                'x-runtime-control-key': self.control_key, 'accept': 'application/json', 'content-type': 'application/json',
            })
            transport = connection.sock
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FleetError('runtime control request budget expired')
            if transport is not None:
                transport.settimeout(remaining)
            response = connection.getresponse()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FleetError('runtime control response arrived after its request budget')
            if transport is not None:
                transport.settimeout(remaining)
            raw = response.read(MAX_JSON_BYTES + 1)
            if time.monotonic() >= deadline:
                raise FleetError('runtime control response arrived after its request budget')
            if response.status == 409:
                raise FleetError('control revision conflict; reread and make a new explicit decision')
            if not 200 <= response.status < 300:
                raise FleetHTTPError(response.status)
            if len(raw) > MAX_JSON_BYTES:
                raise FleetError('runtime control response is too large')
            value = json.loads(raw)
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise FleetError('runtime control is unavailable; no admission/retirement proof') from error
        finally:
            if response is not None:
                response.close()
            connection.close()
        if not isinstance(value, dict) or type(value.get('statusCode')) is not int or \
                value['statusCode'] != response.status or not isinstance(value.get('data'), dict):
            raise FleetError('runtime control returned an invalid success envelope')
        return value['data']

    def registry(self, *, timeout: float = 10) -> dict:
        value = self.request('GET', '/internal/runtime-launchers', timeout=timeout)
        fresh(value)
        return value


def require_first_cutover_authorization(pool: dict, first_evidence: dict | None = None) -> None:
    fields = ('firstCutoverEvidenceId', 'firstCutoverEvidenceHash', 'firstCutoverAuthorizedAt')
    values = [pool.get(field) for field in fields]
    if first_evidence is not None:
        if not isinstance(first_evidence, dict) or set(first_evidence) != {'evidenceId', 'evidenceHash'}:
            raise FleetError('first cutover must name the exact immutable evidence ID/hash, not an authorization boolean')
        evidence_id, evidence_hash = first_evidence.get('evidenceId'), first_evidence.get('evidenceHash')
    elif all(value is None for value in values):
        raise FleetError('ordinary enabling requires the original authoritative first-cutover attestation')
    else:
        evidence_id, evidence_hash = values[:2]
    try:
        if not isinstance(evidence_id, str) or str(uuid.UUID(evidence_id)) != evidence_id:
            raise ValueError('noncanonical evidence ID')
    except (ValueError, TypeError, AttributeError) as error:
        raise FleetError('first-cutover evidence ID is missing or invalid') from error
    if not isinstance(evidence_hash, str) or not DIGEST.fullmatch(evidence_hash):
        raise FleetError('first-cutover artifact hash is missing or invalid')
    if any(value is not None for value in values):
        if any(value is None for value in values) or values[:2] != [evidence_id, evidence_hash]:
            raise FleetError('original first-cutover attestation is partial or cannot be substituted')
        if timestamp(values[2]) > datetime.now(timezone.utc):
            raise FleetError('first-cutover authorization is future-dated')


def set_admission(client: FleetClient, pool_id: str, replicas: int, *, initial: bool, recovery: bool, retiring: bool, timeout: float = 45, first_cutover_evidence: dict | None = None, deadline: float | None = None) -> dict:
    def request_timeout() -> float:
        if deadline is None:
            return 10
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FleetError('admission operation exceeded its caller acceptance deadline')
        return min(10, remaining)

    snapshot = client.registry(timeout=request_timeout())
    pool = find_pool(snapshot, pool_id)
    if pool['retiring']:
        raise FleetError('final SDK retirement is irreversible; use a new incarnation/pool, not resume')
    if retiring:
        if initial or recovery:
            raise FleetError('retiring pool cannot accept jobs')
        require_retirable(snapshot, pool_id, replicas)
    elif initial or recovery:
        require_data_ready(snapshot)
        require_legacy_drained(snapshot)
        require_first_cutover_authorization(pool, first_cutover_evidence)
        for launcher in require_pool_ack(snapshot, pool_id, replicas):
            require_registered_inventory(launcher.get('compatibilityInventory'))
    elif first_cutover_evidence is not None:
        raise FleetError('first-cutover evidence is only valid for an explicit enabling mutation')
    body = {
        'initialAccepting': initial, 'recoveryAccepting': recovery,
        'retiring': retiring, 'expectedRevision': pool['revision'],
    }
    if first_cutover_evidence is not None:
        body['firstCutoverEvidence'] = first_cutover_evidence
    desired = client.request('PUT', '/internal/runtime-launchers/pools/' + quote(pool_id, safe='') + '/admission', body, timeout=request_timeout())
    revision = desired.get('revision')
    if type(revision) is not int or revision <= pool['revision']:
        raise FleetError('control did not produce a new durable desired revision')
    ack_deadline = time.monotonic() + timeout
    if deadline is not None:
        ack_deadline = min(ack_deadline, deadline)
    while True:
        snapshot = client.registry(timeout=request_timeout())
        if deadline is not None and time.monotonic() >= deadline:
            raise FleetError('admission ACK arrived after its caller acceptance deadline')
        current = find_pool(snapshot, pool_id)
        if current['revision'] != revision:
            raise FleetError('desired state changed while waiting for incarnation ACKs')
        if (current['initialAccepting'], current['recoveryAccepting'], current['retiring']) != (initial, recovery, retiring):
            raise FleetError('durable desired state does not match requested state')
        try:
            require_pool_ack(snapshot, pool_id, replicas)
            if initial or recovery:
                require_data_ready(snapshot)
                require_first_cutover_authorization(current)
            return snapshot
        except FleetError:
            if time.monotonic() >= ack_deadline:
                raise FleetError('desired revision persisted but all incarnation ACKs and current readiness were not observed; do not deploy/retire') from None
            time.sleep(min(0.25, max(0, ack_deadline - time.monotonic())))


def bao_ready(address: str, ca_file: Path, expected_cluster_id: str) -> dict:
    parsed = urlsplit(address)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment or not expected_cluster_id:
        raise FleetError('verified HTTPS OpenBao origin and original cluster ID are required')
    try:
        context = ssl.create_default_context(cafile=str(ca_file))
        connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, context=context, timeout=5)
        try:
            connection.request('GET', '/v1/sys/health')
            response = connection.getresponse()
            value = json.loads(response.read(65536))
            if response.status != 200 or value.get('initialized') is not True or value.get('sealed') is not False or value.get('cluster_id') != expected_cluster_id:
                raise FleetError('original OpenBao cluster is not unsealed and ready')
        finally:
            connection.close()
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise FleetError('OpenBao unavailable; no key/data readiness proof') from error
    return {'initialized': True, 'sealed': False, 'clusterId': value['cluster_id']}


def approved_operation(approval: dict, *, environment_id: str, scope: str) -> None:
    if approval.get('environmentId') != environment_id or approval.get('scope') != scope or not approval.get('approvedBy'):
        raise FleetError('explicit operational approval is missing or targets another environment/scope')
    if timestamp(approval.get('expiresAt')) <= datetime.now(timezone.utc):
        raise FleetError('operational approval has expired')


def rotation_classes(value: object) -> list[str]:
    if not isinstance(value, list) or not value or any(
        not isinstance(name, str) or name not in ROTATION_CREDENTIAL_CLASSES for name in value
    ) or len(set(value)) != len(value):
        raise FleetError('approval must enumerate unique exposed runtime credential classes; original seal/KEK/DEKs are forbidden')
    return value


def rotation_proof_path(value: object) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise FleetError('rotation evidence must name an explicit absolute artifact path')
    return Path(value)


def require_rotation_verified(receipt: dict, environment_id: str) -> None:
    if receipt.get('schemaVersion') != 1 or receipt.get('environmentId') != environment_id or receipt.get('scope') != ROTATION_SCOPE:
        raise FleetError('rotation receipt is not bound to the first-cutover environment/purpose')
    if type(receipt.get('commandExitCode')) is not int or receipt['commandExitCode'] != 0 or receipt.get('providerVerification') != 'observed':
        raise FleetError('process exit alone is not provider revocation/replacement proof')
    classes = rotation_classes(receipt.get('credentialClasses'))
    now = datetime.now(timezone.utc)
    observed_at = timestamp(receipt.get('observedAt'))
    if observed_at > now:
        raise FleetError('rotation receipt is future-dated')
    approval_path = rotation_proof_path(receipt.get('approvalPath'))
    if file_hash(approval_path) != receipt.get('approvalSha256'):
        raise FleetError('rotation approval is unavailable or changed')
    approval = load_json(approval_path, private=True)
    if approval.get('scope') != ROTATION_SCOPE or approval.get('environmentId') != environment_id or not isinstance(approval.get('approvedBy'), str) or not approval['approvedBy']:
        raise FleetError('original rotation approval targets another environment/purpose')
    if timestamp(approval.get('expiresAt')) <= observed_at:
        raise FleetError('original rotation approval was not valid at the observed receipt time')
    if set(rotation_classes(approval.get('credentialClasses'))) != set(classes):
        raise FleetError('rotation receipt does not cover the explicitly approved credential census')
    result_path = rotation_proof_path(receipt.get('resultFile'))
    if result_path != rotation_proof_path(approval.get('resultFile')) or file_hash(result_path) != receipt.get('resultSha256'):
        raise FleetError('approved provider readback result is missing, changed or substituted')
    result = load_json(result_path, private=True)
    if result.get('schemaVersion') != 1 or result.get('scope') != ROTATION_SCOPE or result.get('environmentId') != environment_id or result.get('status') != 'observed':
        raise FleetError('provider result is unknown or targets another environment/purpose')
    if timestamp(result.get('observedAt')) > now or set(rotation_classes(result.get('credentialClasses'))) != set(classes):
        raise FleetError('provider result does not cover this actual credential census')
    proofs = result.get('providerReceipts')
    if not isinstance(proofs, list) or len(proofs) != len(classes):
        raise FleetError('every exposed credential class requires its own provider readback receipt')
    seen = set()
    for proof in proofs:
        if not isinstance(proof, dict) or proof.get('credentialClass') not in classes or proof.get('credentialClass') in seen:
            raise FleetError('provider credential-class receipts are incomplete or ambiguous')
        seen.add(proof['credentialClass'])
        if proof.get('status') != 'revoked-and-replaced' or any(
            proof.get(name) is not True for name in (
                'revocationVerified', 'replacementVerified', 'oldCredentialDenied', 'newCredentialAccepted',
            )
        ) or any(not isinstance(proof.get(name), str) or not proof[name] for name in ('provider', 'receiptId')):
            raise FleetError('old denial and replacement acceptance are not both observed for each credential class')
        artifacts = proof.get('artifacts')
        if not isinstance(artifacts, list) or not artifacts:
            raise FleetError('provider readback must retain actual redacted evidence artifacts')
        for artifact in artifacts:
            if not isinstance(artifact, dict) or artifact.get('redacted') is not True:
                raise FleetError('provider readback artifacts must be explicitly redacted')
            if file_hash(rotation_proof_path(artifact.get('path'))) != artifact.get('sha256'):
                raise FleetError('provider readback artifact is missing or content hash mismatched')


def rotation(client: FleetClient, approval_path: Path, environment_id: str, output: Path) -> dict:
    approval = load_json(approval_path, private=True)
    approved_operation(approval, environment_id=environment_id, scope=ROTATION_SCOPE)
    classes = rotation_classes(approval.get('credentialClasses'))
    command = approval.get('command')
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command) or not Path(command[0]).is_absolute():
        raise FleetError('approval must name an absolute audited operational executable')
    result_path = rotation_proof_path(approval.get('resultFile'))
    if output.exists() or output.is_symlink() or result_path.exists() or result_path.is_symlink():
        raise FleetError('rotation output/provider result must be new; uncertain operations are never automatically retried')
    if output.resolve() == result_path.resolve() or output.resolve() == approval_path.resolve():
        raise FleetError('rotation approval, provider result and receipt must be separate files')
    snapshot = client.registry()
    require_legacy_drained(snapshot)
    for pool in snapshot['pools']:
        pool = find_pool(snapshot, pool.get('poolId'))
        if pool['initialAccepting'] or pool['recoveryAccepting']:
            raise FleetError('withdraw every pool before first-cutover credential rotation')
    receipt = {
        'schemaVersion': 1, 'environmentId': environment_id, 'scope': ROTATION_SCOPE,
        'approvalPath': str(approval_path.absolute()), 'approvalSha256': file_hash(approval_path),
        'credentialClasses': classes, 'resultFile': str(result_path),
        'observedAt': utc_iso(), 'commandExitCode': None,
        'providerVerification': 'unproven',
    }
    # Reserve the attempt before a command can change credentials. A crash cannot silently authorize a retry.
    write_new_json(output.with_name(output.name + '.attempt'), receipt)
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        write_new_json(output, receipt)
        raise FleetError('approved rotation outcome is unknown; unproven receipt retained, no automatic retry') from error
    receipt['commandExitCode'] = result.returncode
    receipt['observedAt'] = utc_iso()
    if result.returncode:
        write_new_json(output, receipt)
        raise FleetError('approved rotation command failed; unproven receipt retained, admission remains withdrawn')
    try:
        receipt['resultSha256'] = file_hash(result_path)
        verified = {**receipt, 'providerVerification': 'observed'}
        require_rotation_verified(verified, environment_id)
    except FleetError as error:
        write_new_json(output, receipt)
        raise FleetError('provider revocation/replacement remains unproven; receipt retained, admission remains withdrawn') from error
    write_new_json(output, verified)
    return verified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', default=os.environ.get('INTERNAL_API_BASE_URL'))
    parser.add_argument('--allow-loopback', action='store_true', help='isolated fixture HTTP only')
    parser.add_argument('--environment-id')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    for name in ('withdraw', 'resume', 'retire'):
        command = sub.add_parser(name)
        command.add_argument('--pool', required=True)
        command.add_argument('--replicas', required=True, type=int)
        command.add_argument('--timeout', type=float, default=45)
        if name == 'withdraw':
            command.add_argument('--keep-recovery', action='store_true')
    rollback = sub.add_parser('rollback')
    rollback.add_argument('--source-pool', required=True)
    rollback.add_argument('--target-pool', required=True)
    rollback.add_argument('--source-inventory', type=Path, required=True)
    rollback.add_argument('--target-inventory', type=Path, required=True)
    rollback.add_argument('--source-replicas', type=int, required=True)
    rollback.add_argument('--target-replicas', type=int, required=True)
    preflight = sub.add_parser('preflight')
    preflight.add_argument('--evidence', type=Path, required=True)
    preflight.add_argument('--project-id', required=True)
    preflight.add_argument('--inventory', type=Path, action='append', required=True)
    preflight.add_argument('--fleet-config', type=Path, required=True, help='owner-only actual provider desired fleet config')
    preflight.add_argument('--bao-address', required=True)
    preflight.add_argument('--bao-ca', type=Path, required=True)
    preflight.add_argument('--bao-cluster-id', required=True)
    cutover = sub.add_parser('cutover')
    cutover.add_argument('--pool', required=True)
    cutover.add_argument('--replicas', type=int, required=True)
    cutover.add_argument('--evidence', type=Path, required=True)
    cutover.add_argument('--project-id', required=True)
    cutover.add_argument('--inventory', type=Path, action='append', required=True)
    cutover.add_argument('--fleet-config', type=Path, required=True)
    cutover.add_argument('--rotation-receipt', type=Path, required=True)
    cutover.add_argument('--first-cutover-evidence', type=Path, required=True, help='actual approved immutable artifact mounted into the API')
    cutover.add_argument('--bao-address', required=True)
    cutover.add_argument('--bao-ca', type=Path, required=True)
    cutover.add_argument('--bao-cluster-id', required=True)
    rotate = sub.add_parser('rotate-credentials')
    rotate.add_argument('--approval', type=Path, required=True)
    rotate.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        client = FleetClient(args.api or '', os.environ.get('RUNTIME_CONTROL_KEY', ''), allow_loopback=args.allow_loopback)
        if args.command == 'status':
            print(json.dumps(client.registry(), indent=2))
        elif args.command in ('withdraw', 'resume', 'retire'):
            snapshot = set_admission(client, args.pool, args.replicas,
                initial=args.command == 'resume',
                recovery=args.command == 'resume' or (args.command == 'withdraw' and args.keep_recovery),
                retiring=args.command == 'retire', timeout=args.timeout)
            print(json.dumps(find_pool(snapshot, args.pool), indent=2))
        elif args.command == 'rollback':
            source, target = load_json(args.source_inventory), load_json(args.target_inventory)
            require_compatible(source, target)
            snapshot = client.registry()
            require_deployment_inventory(snapshot, [source, target], [
                {'poolId': args.source_pool, 'replicas': args.source_replicas, 'imageDigest': source['imageDigest']},
                {'poolId': args.target_pool, 'replicas': args.target_replicas, 'imageDigest': target['imageDigest']},
            ])
            set_admission(client, args.target_pool, args.target_replicas, initial=True, recovery=True, retiring=False)
            set_admission(client, args.source_pool, args.source_replicas, initial=False, recovery=False, retiring=False)
            print(json.dumps({'compatibleRollback': True, 'sourcePool': args.source_pool, 'targetPool': args.target_pool}))
        elif args.command in ('preflight', 'cutover'):
            if not args.environment_id:
                raise FleetError('--environment-id is required')
            builds = [load_json(path) for path in args.inventory]
            for inventory in builds:
                require_inventory(inventory)
            require_external_gates(load_json(args.evidence), args.environment_id, project_id=args.project_id, builds=builds)
            fleet_config = load_json(args.fleet_config, private=True)
            if fleet_config.get('environmentId') != args.environment_id or fleet_config.get('livekitProjectId') != args.project_id:
                raise FleetError('desired fleet configuration belongs to another provider project/environment')
            import runtime_provider
            try:
                runtime_provider.validate_config(fleet_config)
            except runtime_provider.FleetError as error:
                raise FleetError(str(error)) from error
            pool_services = [service for service in fleet_config['services'] if service['role'] in ('pool-a', 'pool-b')]
            if (len(pool_services) != 2 or {service['role'] for service in pool_services} != {'pool-a', 'pool-b'}
                    or any(not isinstance(service.get('poolId'), str) or not service['poolId'] for service in pool_services)
                    or pool_services[0]['poolId'] == pool_services[1]['poolId']):
                raise FleetError('full HA requires exactly distinct pool-a and pool-b role pools')
            desired_pools = [{'poolId': service['poolId'], 'replicas': service['replicas'],
                'imageDigest': service['imageReference'].rsplit('@', 1)[-1]} for service in pool_services]
            snapshot = client.registry()
            require_data_ready(snapshot)
            require_deployment_inventory(snapshot, builds, desired_pools)
            bao = bao_ready(args.bao_address, args.bao_ca, args.bao_cluster_id)
            if args.command == 'cutover':
                require_legacy_drained(snapshot)
                receipt = load_json(args.rotation_receipt, private=True)
                require_rotation_verified(receipt, args.environment_id)
                original_evidence = load_json(args.first_cutover_evidence, private=True)
                if original_evidence.get('environmentId') != args.environment_id or original_evidence.get('projectId') != args.project_id or original_evidence.get('protocolRevision') != PROTOCOL:
                    raise FleetError('first-cutover artifact belongs to another provider project/environment/protocol')
                first_identity = {'evidenceId': original_evidence.get('evidenceId'), 'evidenceHash': file_hash(args.first_cutover_evidence)}
                require_first_cutover_authorization(find_pool(snapshot, args.pool), first_identity)
                if find_pool(snapshot, args.pool).get('compatibilityFingerprint') not in {build['compatibilityFingerprint'] for build in builds}:
                    raise FleetError('cutover pool is not bound to acceptance build inventory')
                set_admission(client, args.pool, args.replicas, initial=True, recovery=True, retiring=False, first_cutover_evidence=first_identity)
            print(json.dumps({'preflight': 'observed', 'environmentId': args.environment_id, 'openbao': bao, 'action': args.command}))
        elif args.command == 'rotate-credentials':
            if not args.environment_id:
                raise FleetError('--environment-id is required')
            receipt = rotation(client, args.approval, args.environment_id, args.output)
            print(json.dumps({'rotationCommandCompleted': True, 'providerVerification': receipt['providerVerification'], 'receipt': str(args.output)}))
        return 0
    except FleetError as error:
        print(json.dumps({'status': 'blocked', 'reason': str(error)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
