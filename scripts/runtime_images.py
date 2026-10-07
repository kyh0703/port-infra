#!/usr/bin/env python3
"""Build isolated HA images and inventory their immutable, baked compatibility."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

from runtime_fleet import FleetError, require_inventory, write_new_json
from runtime_fixtures import FixtureError, require_local_docker_context


def docker(*args: str, timeout: int = 30) -> bytes:
    try:
        environment = require_local_docker_context()
        result = subprocess.run(['docker', *args], capture_output=True, timeout=timeout, check=False, env=environment)
    except FixtureError as error:
        raise FleetError(str(error)) from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FleetError('Docker image operation unavailable or timed out') from error
    if result.returncode:
        raise FleetError('Docker image operation failed; no image compatibility proof')
    return result.stdout


def inspect_image(reference: str, *, worker: bool = True) -> dict:
    try:
        items = json.loads(docker('image', 'inspect', reference))
        image = items[0]
        image_id = image['Id']
        digests = sorted(image.get('RepoDigests') or [])
        immutable_reference = digests[0] if digests else image_id
        digest = immutable_reference.rsplit('@', 1)[-1]
    except (ValueError, IndexError, KeyError, TypeError) as error:
        raise FleetError('Docker returned no immutable image identity') from error
    if not worker:
        return {'imageReference': immutable_reference, 'imageDigest': digest, 'imageId': image_id}
    # cat is the sole process; no worker startup, network, env credentials or call.
    raw = docker('run', '--rm', '--network', 'none', '--read-only',
        '--label', 'io.port.runtime-ha.image-inspection=' + uuid.uuid4().hex,
        '--entrypoint', 'cat', image_id, '/app/runtime-build-inventory.json')
    try:
        inventory = json.loads(raw)
    except ValueError as error:
        raise FleetError('worker image has no valid baked build inventory') from error
    if not isinstance(inventory, dict):
        raise FleetError('worker image inventory must be an object')
    inventory['imageDigest'] = digest
    require_inventory(inventory)
    return {**inventory, 'imageReference': immutable_reference, 'imageId': image_id}


def build_image(source: Path, tag: str, kind: str, build_id: str | None) -> dict:
    if not source.is_dir() or not (source / 'Dockerfile').is_file():
        raise FleetError('an existing explicit Dockerfile source is required')
    if not tag.startswith('port-runtime-ha-'):
        raise FleetError('local build tag must use the isolated port-runtime-ha- prefix')
    if kind == 'worker' and not build_id:
        raise FleetError('worker build requires a unique immutable --build-id')
    command = ['docker', 'build', '--tag', tag]
    if kind == 'worker':
        command += ['--build-arg', 'RUNTIME_BUILD_ID=' + build_id]
    elif kind == 'migrator':
        command += ['--target', 'migrator']
    command.append(str(source.resolve()))
    try:
        # Preserve actionable non-secret build logs; no runtime secrets are sent.
        environment = require_local_docker_context()
        result = subprocess.run(command, stdin=subprocess.DEVNULL, check=False, env=environment)
    except FixtureError as error:
        raise FleetError(str(error)) from error
    except OSError as error:
        raise FleetError('Docker build is unavailable') from error
    if result.returncode:
        raise FleetError('HA image build failed; deployment is blocked')
    return inspect_image(tag, worker=kind == 'worker')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    inspect = sub.add_parser('inspect')
    inspect.add_argument('--image', required=True)
    inspect.add_argument('--kind', choices=('worker', 'api', 'migrator'), default='worker')
    inspect.add_argument('--output', type=Path)
    build = sub.add_parser('build')
    build.add_argument('--source', type=Path, required=True)
    build.add_argument('--tag', required=True)
    build.add_argument('--kind', choices=('worker', 'api', 'migrator'), required=True)
    build.add_argument('--build-id')
    build.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        value = inspect_image(args.image, worker=args.kind == 'worker') if args.command == 'inspect' else build_image(args.source, args.tag, args.kind, args.build_id)
        if args.output:
            write_new_json(args.output, value)
        print(json.dumps(value, indent=2))
        return 0
    except FleetError as error:
        print(json.dumps({'status': 'blocked', 'reason': str(error)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
