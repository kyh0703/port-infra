#!/usr/bin/env python3
"""Read actual Railway deployment configuration and bind exercised acceptance evidence.

Read-only inspection never turns configuration/replicas into availability proof.
Only deploy-approved mutates Railway, using an explicit expiring approval and an
already configured immutable image. It never creates services, volumes or keys.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runtime_fleet import (
    COMPATIBILITY_FIELDS, EXTERNAL_GATES, JOB_INVENTORY_FIELDS, PROTOCOL, RETIREMENT_COUNTERS,
    FleetClient, FleetError, approved_operation, canonical_json, file_hash, find_pool, load_json,
    require_clock_bound, require_compatible, require_data_ready, require_first_cutover_authorization, require_inventory,
    require_pool_ack, require_pool_inventory, require_registered_inventory, require_retirable,
    set_admission, timestamp, utc_iso, write_new_json,
)

REQUIRED_ASSERTIONS = {
    'cloudAssignmentFreshness': ('serverObservedAssignment', 'staleAssignmentRejected', 'poolIncarnationBound'),
    'cloudAbsentParticipantRevocation': ('absentIdentityRevoked', 'sameSecondRaceFailClosed', 'explicitUnixSecondsCutoff', 'notProvenBy404'),
    'cloudTokenRefreshRevocation': ('recentlyRefreshedTokenRejected', 'allInheritedIdentitiesCovered', 'staleOwnerCannotActivate'),
    'railwayPlacement': ('actualInstancesObserved', 'failureDomainsConfirmed', 'replicaInventoryBound'),
    'railwaySignalGrace': ('sigtermObserved', 'noPrematureSigkill', 'childInferenceShutdownBounded'),
    'postgresqlFailoverDurability': ('singleFencedWritablePrimary', 'committedWritesReadAfterPromotion', 'measuredDurabilityRecorded'),
    'redisStateLoss': ('missingRawFormNotAcknowledged', 'checkpointConsumptionSurvives', 'inputUncertaintyPreserved'),
    'productionSealAvailability': ('sameExistingSealKey', 'originalClusterId', 'originalEnvelopeDecrypts', 'restartUnseals', 'noReinitialization'),
}


def railway(*arguments: str) -> object:
    executable = shutil.which('railway')
    if executable is None:
        raise FleetError('prerequisite missing: install the current Railway CLI; no provider configuration was observed')
    try:
        result = subprocess.run([executable, *arguments], stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FleetError('Railway provider request unavailable/timed out; no deployment proof') from error
    if result.returncode:
        raise FleetError('Railway request failed: authenticate with scoped RAILWAY_TOKEN/RAILWAY_API_TOKEN or railway login, and supply real project/environment/service IDs')
    try:
        return json.loads(result.stdout)
    except ValueError as error:
        raise FleetError('Railway CLI returned unsupported JSON; update CLI or provider parser, never assume placement') from error


def validate_config(config: dict) -> None:
    for name in ('projectId', 'environmentId', 'livekitProjectId'):
        if not isinstance(config.get(name), str) or not config[name]:
            raise FleetError('provider configuration requires actual ' + name)
    builds = config.get('builds')
    if not isinstance(builds, list) or not builds:
        raise FleetError('provider configuration requires exact immutable build inventories')
    for build in builds:
        require_inventory(build)
    services = config.get('services')
    if not isinstance(services, list) or not services:
        raise FleetError('provider configuration requires exact existing service IDs')
    for service in services:
        if not isinstance(service, dict) or not service.get('serviceId') or not service.get('role'):
            raise FleetError('provider service identity/role is missing')
        if type(service.get('replicas')) is not int or service['replicas'] < 1:
            raise FleetError('provider service requires explicit replica count')
        image = service.get('imageReference')
        if not isinstance(image, str) or '@sha256:' not in image:
            raise FleetError('Railway source must be a registry image pinned by digest, never a moving tag or local image ID')
        if not service.get('healthcheckPath') or not service.get('startCommand') or not isinstance(service.get('regions'), dict) or not service['regions']:
            raise FleetError('provider service requires explicit health/start and actual desired region mapping')
        if service['role'] in ('pool-a', 'pool-b') and (service['replicas'] < 2 or int(service.get('drainingSeconds', 0)) < 45 or not service.get('poolId')):
            raise FleetError('runtime pool requires >=2 replicas, >=45s draining and an explicit pool ID')
        if service['role'] == 'openbao' and service['replicas'] != 1:
            raise FleetError('preserved single-node Bao raft is not replica HA; never deploy two independent masters')
        if sum(service['regions'].values()) != service['replicas']:
            raise FleetError('provider desired region counts must equal explicit fleet size')


def serving_config(deployments: object, deployment_id: str) -> dict:
    if not isinstance(deployments, list):
        raise FleetError('Railway returned unsupported deployment inventory')
    matches = [entry for entry in deployments if isinstance(entry, dict) and entry.get('id') == deployment_id]
    if len(matches) != 1:
        raise FleetError('serving Railway deployment metadata is unavailable; increase bounded deployment list or inspect exact deployment')
    meta = matches[0].get('meta')
    manifest = meta.get('serviceManifest') if isinstance(meta, dict) else None
    deploy = manifest.get('deploy') if isinstance(manifest, dict) else None
    if not isinstance(deploy, dict):
        raise FleetError('effective Railway deploy manifest is unavailable; a local template is not evidence')
    # Never persist raw meta: it can contain registry credentials/build secrets.
    fields = ('startCommand', 'healthcheckPath', 'healthcheckTimeout', 'numReplicas', 'multiRegionConfig', 'region', 'drainingSeconds', 'overlapSeconds', 'restartPolicyType')
    return {name: deploy.get(name) for name in fields}


def inspect_provider(config: dict) -> dict:
    validate_config(config)
    observed_at = datetime.now(timezone.utc)
    artifact = {
        'schemaVersion': 1, 'evidenceId': str(uuid.uuid4()), 'provider': 'livekit-cloud',
        'projectId': config['livekitProjectId'], 'railwayProjectId': config['projectId'],
        'environmentId': config['environmentId'], 'protocolRevision': PROTOCOL,
        'observedAt': utc_iso(observed_at), 'expiresAt': utc_iso(observed_at + timedelta(hours=24)),
        'builds': config['builds'],
        'gates': {name: {'status': 'unproven', 'scenario': None, 'artifacts': []} for name in EXTERNAL_GATES},
        'providerConfiguration': {'status': 'unproven', 'services': [], 'prerequisites': []},
    }
    try:
        services = railway('service', 'list', '--project', config['projectId'], '--environment', config['environmentId'], '--json')
        if not isinstance(services, list):
            raise FleetError('Railway service JSON shape unsupported; exact provider observation required')
        for desired in config['services']:
            matches = [service for service in services if isinstance(service, dict) and service.get('id') == desired['serviceId']]
            if len(matches) != 1:
                raise FleetError('actual Railway service is missing: ' + desired['serviceId'])
            service = matches[0]
            source = service.get('source') or {}
            replicas = service.get('replicas') or {}
            if source.get('image') != desired['imageReference']:
                raise FleetError('Railway service source is not the approved immutable image: ' + desired['serviceId'])
            if service.get('status') != 'SUCCESS' or service.get('deploymentStopped') is not False:
                raise FleetError('Railway service is not serving a successful deployment: ' + desired['serviceId'])
            if replicas.get('configured') != desired['replicas'] or replicas.get('running') != desired['replicas'] or replicas.get('crashed') != 0:
                raise FleetError('Railway actual replica inventory does not match desired fleet: ' + desired['serviceId'])
            actual_regions = {region['name']: region['configured'] for region in service.get('regions', []) if isinstance(region, dict) and 'name' in region and 'configured' in region}
            if actual_regions != desired['regions']:
                raise FleetError('Railway region configuration does not match explicit provider contract')
            deployments = railway('deployment', 'list', '--project', config['projectId'], '--environment', config['environmentId'], '--service', desired['serviceId'], '--limit', '100', '--json')
            effective = serving_config(deployments, service.get('deploymentId'))
            for field in ('startCommand', 'healthcheckPath'):
                if effective.get(field) != desired[field]:
                    raise FleetError('effective provider manifest mismatch: ' + field)
            if int(effective.get('drainingSeconds') or 0) < int(desired.get('drainingSeconds', 45)):
                raise FleetError('effective Railway grace is too short or unproven')
            artifact['providerConfiguration']['services'].append({
                'serviceId': desired['serviceId'], 'role': desired['role'], 'deploymentId': service['deploymentId'],
                'imageReference': source['image'], 'replicas': replicas, 'regions': actual_regions,
                'effectiveDeploy': effective,
            })
        artifact['providerConfiguration']['status'] = 'observed'
    except FleetError as error:
        artifact['providerConfiguration']['status'] = 'blocked'
        artifact['providerConfiguration']['prerequisites'].append(str(error))
    # Region config does NOT prove failure-domain placement. A healthy deploy does
    # NOT prove SIGTERM/grace, Cloud revocation, failover durability or seal access.
    return artifact


def record_existing_evidence(base: dict, result_path: Path, approval: dict) -> dict:
    approved_operation(approval, environment_id=base.get('environmentId', ''), scope='record-runtime-acceptance-evidence')
    result = load_json(result_path)
    gate = result.get('gate')
    if gate not in REQUIRED_ASSERTIONS or result.get('schemaVersion') != 1 or result.get('status') != 'observed':
        raise FleetError('scenario result must be an exercised, supported acceptance result, not a configuration template')
    expected_provider = 'livekit-cloud' if gate.startswith('cloud') else 'railway'
    expected_project = base.get('projectId') if expected_provider == 'livekit-cloud' else base.get('railwayProjectId')
    if result.get('provider') != expected_provider or not expected_project or result.get('projectId') != expected_project:
        raise FleetError('local/OSS results cannot prove the selected real provider project')
    for field in ('environmentId', 'protocolRevision', 'builds'):
        if canonical_json(result.get(field)) != canonical_json(base.get(field)):
            raise FleetError('scenario result is not bound to exact provider/build/SDK/protocol: ' + field)
    now = datetime.now(timezone.utc)
    if timestamp(result.get('observedAt')) > now or timestamp(base.get('expiresAt')) <= now:
        raise FleetError('scenario result is future-dated or acceptance envelope expired')
    assertions = result.get('assertions')
    if not isinstance(assertions, dict) or any(assertions.get(name) is not True for name in REQUIRED_ASSERTIONS[gate]):
        raise FleetError('scenario has not observed every required uncertain-edge assertion: ' + gate)
    clock_bound = require_clock_bound(result.get('clockBound')) if gate == 'cloudTokenRefreshRevocation' else None
    if not isinstance(result.get('scenario'), str) or not result['scenario']:
        raise FleetError('scenario must identify the actual approved exercised command/run')
    evidence_files = result.get('artifacts')
    if not isinstance(evidence_files, list) or not evidence_files:
        raise FleetError('scenario result must retain actual redacted provider evidence artifacts')
    artifacts = [{'path': str(result_path.resolve()), 'sha256': file_hash(result_path)}]
    for item in evidence_files:
        if not isinstance(item, dict) or not isinstance(item.get('path'), str) or file_hash(Path(item['path'])) != item.get('sha256'):
            raise FleetError('scenario provider evidence is missing or content hash mismatched')
        artifacts.append(item)
    updated = {**base, 'observedAt': utc_iso(timestamp(base.get('observedAt'))),
        'expiresAt': utc_iso(timestamp(base.get('expiresAt'))), 'gates': dict(base['gates'])}
    updated['gates'][gate] = {
        'status': 'observed', 'scenario': result['scenario'], 'observedAt': utc_iso(timestamp(result['observedAt'])),
        'artifacts': artifacts, 'approvalSha256': approval['_contentHash'],
    }
    if gate == 'cloudTokenRefreshRevocation':
        updated['gates'][gate]['clockBound'] = clock_bound
    return updated


def pool_has_obligations(snapshot: dict, desired: dict, accepted_builds: list[dict]) -> bool:
    live = require_pool_ack(snapshot, desired['poolId'], desired['replicas'])
    census = find_pool(snapshot, desired['poolId']).get('retirementBlockers')
    if not isinstance(census, dict):
        raise FleetError('deployment obligation census is unavailable')
    obligations = False
    for name in RETIREMENT_COUNTERS:
        value = census.get(name)
        if type(value) is not int or value < 0:
            raise FleetError('deployment obligation census is unknown: ' + name)
        obligations |= value > 0
    for launcher in live:
        actual = launcher.get('compatibilityInventory')
        require_registered_inventory(actual)
        if not any(all(actual[name] == build[name] for name in (*COMPATIBILITY_FIELDS, 'buildId', 'imageDigest'))
                   for build in accepted_builds):
            raise FleetError('source incarnation is not an accepted immutable deployment inventory')
        obligations |= any(launcher['inventory'][name] for name in JOB_INVENTORY_FIELDS[:-1])
    return bool(obligations)


def require_recovery_alternative(snapshot: dict, config: dict, source_pool: str, source_build: dict) -> None:
    require_data_ready(snapshot)
    for desired in config['services']:
        if desired['role'] not in ('pool-a', 'pool-b') or desired.get('poolId') == source_pool:
            continue
        try:
            pool = find_pool(snapshot, desired['poolId'])
            if not pool['recoveryAccepting'] or pool['retiring']:
                continue
            require_first_cutover_authorization(pool)
            matches = [build for build in config['builds'] if desired['imageReference'].endswith('@' + build['imageDigest'])]
            if len(matches) != 1:
                continue
            require_compatible(source_build, matches[0])
            require_pool_inventory(snapshot, desired['poolId'], desired['replicas'], matches[0])
            return
        except FleetError:
            continue
    raise FleetError('deployment with outstanding duties requires a fresh ACKed accepted compatible recovery pool')


def deploy_approved(config: dict, service_id: str, approval: dict, api: str, allow_loopback: bool) -> dict:
    validate_config(config)
    approved_operation(approval, environment_id=config['environmentId'], scope='deploy-runtime-ha-immutable-image')
    if approval.get('projectId') != config['projectId'] or approval.get('serviceId') != service_id:
        raise FleetError('deployment approval must bind exact Railway project and service')
    desired = next((service for service in config['services'] if service['serviceId'] == service_id), None)
    if desired is None or desired['role'] not in ('pool-a', 'pool-b'):
        raise FleetError('this command deploys only an explicitly approved isolated runtime pool, never data masters')
    if approval.get('imageReference') != desired['imageReference']:
        raise FleetError('deployment approval image digest mismatch')
    selected_builds = [build for build in config['builds'] if desired['imageReference'].endswith('@' + build['imageDigest'])]
    if len(selected_builds) != 1:
        raise FleetError('approved provider image must bind one exact baked build inventory')
    target = selected_builds[0]
    client = FleetClient(api, os.environ.get('RUNTIME_CONTROL_KEY', ''), allow_loopback=allow_loopback)
    snapshot = client.registry()
    require_data_ready(snapshot)
    if pool_has_obligations(snapshot, desired, config['builds']):
        if find_pool(snapshot, desired['poolId'])['compatibilityFingerprint'] != target['compatibilityFingerprint']:
            raise FleetError('incompatible deployment requires zero original-lineage obligations')
        require_recovery_alternative(snapshot, config, desired['poolId'], target)
    # Check the source before withdrawal and again after it: newly accepted work must not lose its only recovery pool.
    snapshot = set_admission(client, desired['poolId'], desired['replicas'], initial=False, recovery=False, retiring=False)
    if pool_has_obligations(snapshot, desired, config['builds']):
        if find_pool(snapshot, desired['poolId'])['compatibilityFingerprint'] != target['compatibilityFingerprint']:
            raise FleetError('new original-lineage duties prevent an incompatible deployment')
        require_recovery_alternative(snapshot, config, desired['poolId'], target)
    else:
        require_retirable(snapshot, desired['poolId'], desired['replicas'])
    services = railway('service', 'list', '--project', config['projectId'], '--environment', config['environmentId'], '--json')
    actual = next((item for item in services if isinstance(item, dict) and item.get('id') == service_id), None) if isinstance(services, list) else None
    if actual is None or (actual.get('source') or {}).get('image') != desired['imageReference']:
        raise FleetError('configure the exact approved image source in Railway first; service-level source mutation may affect other environments and is never automatic')
    result = railway('service', 'redeploy', '--from-source', '--yes', '--json', '--project', config['projectId'], '--environment', config['environmentId'], '--service', service_id)
    return {'providerRequest': 'submitted', 'serviceId': service_id, 'imageReference': desired['imageReference'], 'admission': 'withdrawn', 'providerResult': result, 'acceptance': 'unproven'}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    inspect = sub.add_parser('inspect')
    inspect.add_argument('--config', type=Path, required=True)
    inspect.add_argument('--output', type=Path, required=True)
    record = sub.add_parser('record-evidence')
    record.add_argument('--base', type=Path, required=True)
    record.add_argument('--result', type=Path, required=True)
    record.add_argument('--approval', type=Path, required=True)
    record.add_argument('--output', type=Path, required=True)
    deploy = sub.add_parser('deploy-approved')
    deploy.add_argument('--config', type=Path, required=True)
    deploy.add_argument('--service-id', required=True)
    deploy.add_argument('--approval', type=Path, required=True)
    deploy.add_argument('--api', required=True)
    deploy.add_argument('--allow-loopback', action='store_true')
    deploy.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'inspect':
            artifact = inspect_provider(load_json(args.config, private=True))
            write_new_json(args.output, artifact)
            print(json.dumps({'evidenceId': artifact['evidenceId'], 'sha256': file_hash(args.output), 'providerConfiguration': artifact['providerConfiguration'], 'gates': {name: 'unproven' for name in EXTERNAL_GATES}}, indent=2))
            return 0 if artifact['providerConfiguration']['status'] == 'observed' else 2
        approval = load_json(args.approval, private=True)
        approval['_contentHash'] = file_hash(args.approval)
        if args.command == 'record-evidence':
            artifact = record_existing_evidence(load_json(args.base), args.result, approval)
            write_new_json(args.output, artifact)
            print(json.dumps({'evidenceId': artifact['evidenceId'], 'sha256': file_hash(args.output), 'gates': {name: item['status'] for name, item in artifact['gates'].items()}}))
        else:
            artifact = deploy_approved(load_json(args.config, private=True), args.service_id, approval, args.api, args.allow_loopback)
            write_new_json(args.output, artifact)
            print(json.dumps(artifact, indent=2))
        return 0
    except FleetError as error:
        print(json.dumps({'status': 'blocked', 'reason': str(error)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
