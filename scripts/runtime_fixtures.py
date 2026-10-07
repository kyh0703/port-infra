#!/usr/bin/env python3
"""Own isolated real PostgreSQL/Redis/LiveKit/OpenBao and A/B runtime fixtures.

All state is new, external and owner-only. Every Docker mutation/deletion verifies
session labels. No default compose, .env, production volume, Keychain or old key is
read. The only automatic Bao initialization is a new, positively owned fixture.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import secrets
import ssl
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIMARY_ROOT = ROOT.parents[1] if ROOT.parent.name == '.worktrees' else ROOT
PROJECT_ROOT = PRIMARY_ROOT.parent
LABEL = 'io.port.runtime-ha.fixture'
OWNER_LABEL = 'io.port.runtime-ha.owner'
DATA_SERVICES = ('postgres', 'redis', 'livekit', 'openbao')
RUNTIME_SERVICES = ('api-migrator', 'api', 'pool-a', 'pool-b')
VOLUMES = ('postgres_data', 'redis_data', 'openbao_data', 'openbao_audit', 'api_credentials')
# The server allows 90s requests; initialization includes the bounded raft election.
BAO_INIT_REQUEST_TIMEOUT_SECONDS = 95


class FixtureError(RuntimeError):
    """A safe fixture failure with no secret contents."""


def validate_project(project: str) -> None:
    if not re.fullmatch(r'port-ha-fixture-[a-z0-9][a-z0-9-]{0,48}', project):
        raise FixtureError('project must use a UNIQUE port-ha-fixture- prefix, never infra or parent verification names')


def validate_state_path(path: Path) -> Path:
    if not path.is_absolute():
        raise FixtureError('fixture state must be an absolute external path')
    try:
        info = path.lstat()
    except FileNotFoundError:
        info = None
    except OSError as error:
        raise FixtureError('fixture state directory is unavailable') from error
    if info is not None and (
        not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
    ):
        raise FixtureError('fixture state must be an owned, private, non-symlink directory')
    resolved = path.resolve()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents or path.is_symlink():
        raise FixtureError('fixture state must be outside every repository and existing server mount')
    return resolved


def require_owned_labels(labels: dict, owner: str) -> None:
    if labels.get(LABEL) != 'true' or labels.get(OWNER_LABEL) != owner:
        raise FixtureError('resource lacks this session ownership labels; mutation/deletion refused')


def run(command: list[str], *, data: bytes | None = None, environment: dict | None = None, timeout: float = 60) -> bytes:
    try:
        result = subprocess.run(command, input=data, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FixtureError('fixture command unavailable or timed out; no automatic destructive retry') from error
    if result.returncode:
        # Captured output may contain fixture tokens/env; never dump raw stderr.
        raise FixtureError('fixture command failed: ' + ' '.join(command[:3]) + ' (exit ' + str(result.returncode) + ')')
    return result.stdout


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if not remaining > 0:
        raise FixtureError('fixture operation budget exhausted; no late success or further activity permitted')
    return remaining


def require_local_docker_context(environment: dict[str, str] | None = None, *, timeout: float = 30) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    _remaining_timeout(deadline)
    if environment is None:
        environment = {name: value for name, value in os.environ.items()
            if not name.startswith('RUNTIME_HA_') and not name.startswith('COMPOSE_')}
        environment['COMPOSE_DISABLE_ENV_FILE'] = '1'
    if environment.get('DOCKER_HOST'):
        raise FixtureError('fixture tool requires the local Unix-socket Docker context; no ambient DOCKER_HOST')
    try:
        raw = run(['docker', 'context', 'inspect'], environment=environment, timeout=_remaining_timeout(deadline))
        _remaining_timeout(deadline)
        context = json.loads(raw)
        if not isinstance(context, list) or len(context) != 1:
            raise FixtureError('Docker context must identify one local Unix-socket endpoint')
        endpoint = context[0]['Endpoints']['docker']['Host']
        if not isinstance(endpoint, str) or not endpoint.startswith('unix:///'):
            raise FixtureError('fixture tool requires the local Unix-socket Docker context; no remote/production daemon')
    except (ValueError, KeyError, TypeError) as error:
        raise FixtureError('local Docker context unavailable; no image/resource activity permitted') from error
    _remaining_timeout(deadline)
    return environment


def new_file(path: Path, content: bytes, mode: int = 0o600) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except OSError as error:
        raise FixtureError('refusing existing/unsafe fixture output: ' + path.name) from error


def _read_private_file(path: Path, *, max_bytes: int = 65536) -> bytes:
    if max_bytes <= 0:
        raise FixtureError('private fixture file requires a positive read bound')
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parent = os.fstat(directory)
            if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
                raise FixtureError('fixture secret directory must be private, owned and non-symlink')
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(descriptor, 'rb') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise FixtureError('fixture secret must be an owner-only regular non-symlink file')
                if info.st_size > max_bytes:
                    raise FixtureError('fixture secret file exceeds its bounded read')
                content = source.read(max_bytes + 1)
        finally:
            os.close(directory)
    except OSError as error:
        raise FixtureError('private fixture file unavailable: ' + path.name) from error
    if not content or len(content) > max_bytes:
        raise FixtureError('fixture secret file is empty or exceeds its bounded read')
    return content


def read_private_secret(path: Path, *, max_bytes: int = 65536) -> str:
    try:
        value = _read_private_file(path, max_bytes=max_bytes).decode('utf-8')
    except UnicodeError:
        raise FixtureError('fixture secret file is not valid UTF-8') from None
    if not value.strip():
        raise FixtureError('fixture secret file is empty')
    return value


def read_owned_json(path: Path) -> dict:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise FixtureError('fixture state must be an owner-only regular file')
            value = json.load(source)
    except (OSError, ValueError) as error:
        raise FixtureError('fixture ownership manifest unavailable') from error
    if not isinstance(value, dict):
        raise FixtureError('invalid fixture ownership manifest')
    return value


def read_owned_env(path: Path) -> dict[str, str]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'r', encoding='utf-8') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise FixtureError('fixture env must be an owner-only regular non-symlink file')
            values = {}
            for line in source.read().splitlines():
                match = re.fullmatch(r"(RUNTIME_HA_[A-Z0-9_]+)='([^'\r\n]*)'", line)
                if match is None or match[2].endswith('\\'):
                    raise FixtureError('fixture env is not the generated safe contract')
                values[match[1]] = match[2]
    except (OSError, UnicodeError) as error:
        raise FixtureError('fixture env unavailable: ' + path.name) from error
    return values


def prepare(state: Path, project: str) -> dict:
    validate_project(project)
    state = validate_state_path(state)
    if state.exists():
        raise FixtureError('fixture state already exists; never overwrite seal keys or unrelated bytes')
    if not state.parent.is_dir():
        raise FixtureError('fixture parent directory must already exist')
    try:
        state.mkdir(mode=0o700)
    except OSError as error:
        raise FixtureError('unable to create new fixture state directory') from error
    owner = str(uuid.uuid4())
    manifest = {'schemaVersion': 1, 'fixtureOnly': True, 'project': project, 'owner': owner, 'stateDir': str(state)}
    new_file(state / 'owner.json', json.dumps(manifest).encode())
    new_file(state / 'seal.key', secrets.token_bytes(32), 0o400)
    values = {
        'RUNTIME_HA_OWNER': owner, 'RUNTIME_HA_STATE_DIR': str(state),
        'RUNTIME_HA_POSTGRES_PASSWORD': secrets.token_hex(24),
        'RUNTIME_HA_LIVEKIT_SECRET': secrets.token_hex(32),
        'RUNTIME_HA_INTERNAL_SERVER_KEY': secrets.token_hex(32),
        'RUNTIME_HA_LAUNCHER_KEY': secrets.token_hex(32),
        'RUNTIME_HA_CONTROL_KEY': secrets.token_hex(32),
        'RUNTIME_HA_AUTH_SECRET': secrets.token_hex(32),
        'RUNTIME_HA_RAG_SECRET': secrets.token_hex(32),
        'RUNTIME_HA_CREDENTIAL_KEY': base64.b64encode(secrets.token_bytes(32)).decode(),
    }
    # dotenv quoting is controlled here; no user env or existing .env is copied.
    if any("'" in value or '\n' in value for value in values.values()):
        raise FixtureError('fixture path cannot be represented safely in compose environment')
    new_file(state / 'compose.env', ''.join(name + "='" + value + "'\n" for name, value in values.items()).encode())
    new_file(state / 'livekit.yaml', (
        'port: 7880\nbind_addresses: ["0.0.0.0"]\n'
        'rtc:\n  tcp_port: 7881\n  port_range_start: 50000\n  port_range_end: 50020\n'
        'keys:\n  fixture-runtime-ha: ' + values['RUNTIME_HA_LIVEKIT_SECRET'] + '\n'
    ).encode())
    environment = os.environ.copy()
    environment['OPENBAO_TLS_DIR'] = str(state / 'tls')
    run(['bash', str(ROOT / 'scripts/openbao-tls.sh')], environment=environment)
    return manifest


class Fixtures:
    def __init__(self, state: Path, *, runtime: bool = False):
        self.state = validate_state_path(state)
        self.manifest = read_owned_json(self.state / 'owner.json')
        if self.manifest.get('fixtureOnly') is not True or self.manifest.get('stateDir') != str(self.state):
            raise FixtureError('fixture state is not bound to this external directory')
        self.project = self.manifest.get('project', '')
        self.owner = self.manifest.get('owner', '')
        validate_project(self.project)
        if not self.owner:
            raise FixtureError('fixture owner ID is missing')
        self.runtime = runtime
        self.platform = (self.state / 'platform.env').is_file()
        self.environment = {name: value for name, value in os.environ.items() if not name.startswith('RUNTIME_HA_') and not name.startswith('COMPOSE_')}
        self.environment['COMPOSE_DISABLE_ENV_FILE'] = '1'
        self.assert_state_files()

    def assert_state_files(self) -> dict[str, str]:
        if validate_state_path(self.state) != self.state or read_owned_json(self.state / 'owner.json') != self.manifest:
            raise FixtureError('fixture ownership manifest or external directory changed')
        values = read_owned_env(self.state / 'compose.env')
        if self.platform:
            values.update(read_owned_env(self.state / 'platform.env'))
        if self.runtime:
            values.update(read_owned_env(self.state / 'runtime.env'))
        values.update({name: value for name, value in self.environment.items() if name.startswith('RUNTIME_HA_')})
        if values.get('RUNTIME_HA_OWNER') != self.owner or values.get('RUNTIME_HA_STATE_DIR') != str(self.state):
            raise FixtureError('effective fixture env owner/state does not match the ownership manifest')
        _read_private_file(self.state / 'seal.key')
        _read_private_file(self.state / 'tls/server.key')
        return values

    def docker(self, *args: str, data: bytes | None = None, timeout: float = 60) -> bytes:
        deadline = time.monotonic() + timeout
        _remaining_timeout(deadline)
        read_only = args[:1] in (('ps',), ('inspect',), ('logs',), ('port',), ('images',), ('info',), ('version',)) or args[:2] in (
            ('context', 'inspect'), ('volume', 'ls'), ('volume', 'inspect'),
            ('network', 'ls'), ('network', 'inspect'), ('image', 'ls'), ('image', 'inspect'),
        )
        if not read_only:
            self.assert_owned_resources(timeout=_remaining_timeout(deadline))
        result = run(['docker', *args], data=data, environment=self.environment, timeout=_remaining_timeout(deadline))
        _remaining_timeout(deadline)
        return result

    def compose(self, *args: str, data: bytes | None = None, timeout: float = 180) -> bytes:
        deadline = time.monotonic() + timeout
        _remaining_timeout(deadline)
        if args[:1] in (('ps',), ('config',), ('logs',), ('images',), ('port',), ('top',), ('version',)):
            self.assert_state_files()
        else:
            self.assert_owned_resources(timeout=_remaining_timeout(deadline))
        command = ['docker', 'compose', '--project-directory', str(ROOT), '--env-file', str(self.state / 'compose.env'),
            '-p', self.project, '-f', str(ROOT / 'compose.runtime-ha.yml')]
        if self.platform:
            command += ['--env-file', str(self.state / 'platform.env'), '-f', str(ROOT / 'compose.runtime-platform.yml')]
        if self.runtime:
            command += ['--env-file', str(self.state / 'runtime.env'), '-f', str(ROOT / 'compose.runtime-fleet.yml')]
        result = run([*command, *args], data=data, environment=self.environment, timeout=_remaining_timeout(deadline))
        _remaining_timeout(deadline)
        return result

    def assert_owned_resources(self, *, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        _remaining_timeout(deadline)
        self.assert_state_files()
        require_local_docker_context(self.environment, timeout=_remaining_timeout(deadline))
        ids = self.docker('ps', '--all', '--filter', 'label=com.docker.compose.project=' + self.project,
            '--format', '{{.ID}}', timeout=_remaining_timeout(deadline)).decode().split()
        if ids:
            for item in json.loads(self.docker('inspect', *ids, timeout=_remaining_timeout(deadline))):
                require_owned_labels(item.get('Config', {}).get('Labels') or {}, self.owner)
        existing_volumes = set(self.docker('volume', 'ls', '--format', '{{.Name}}',
            timeout=_remaining_timeout(deadline)).decode().split())
        for suffix in VOLUMES:
            name = self.project + '_' + suffix
            if name in existing_volumes:
                item = json.loads(self.docker('volume', 'inspect', name, timeout=_remaining_timeout(deadline)))[0]
                require_owned_labels(item.get('Labels') or {}, self.owner)
        networks = set(self.docker('network', 'ls', '--format', '{{.Name}}',
            timeout=_remaining_timeout(deadline)).decode().split())
        if self.project + '_default' in networks:
            item = json.loads(self.docker('network', 'inspect', self.project + '_default',
                timeout=_remaining_timeout(deadline)))[0]
            require_owned_labels(item.get('Labels') or {}, self.owner)
        _remaining_timeout(deadline)

    def containers(self, service: str, *, timeout: float = 60) -> list[str]:
        deadline = time.monotonic() + timeout
        _remaining_timeout(deadline)
        if service not in DATA_SERVICES + RUNTIME_SERVICES:
            raise FixtureError('unsupported fixture service')
        ids = self.compose('ps', '--all', '--quiet', service, timeout=_remaining_timeout(deadline)).decode().split()
        if not ids:
            raise FixtureError('owned fixture service has no containers: ' + service)
        for item in json.loads(self.docker('inspect', *ids, timeout=_remaining_timeout(deadline))):
            require_owned_labels(item.get('Config', {}).get('Labels') or {}, self.owner)
        _remaining_timeout(deadline)
        return ids

    def port(self, service: str, internal: int, *, timeout: float = 60, allow_stopped: bool = False) -> list[int]:
        deadline = time.monotonic() + timeout
        _remaining_timeout(deadline)
        ports = []
        for container in self.containers(service, timeout=_remaining_timeout(deadline)):
            item = json.loads(self.docker('inspect', container, timeout=_remaining_timeout(deadline)))[0]
            mappings = item.get('NetworkSettings', {}).get('Ports', {}).get(str(internal) + '/tcp') or []
            local = [mapping for mapping in mappings if mapping.get('HostIp') == '127.0.0.1']
            if allow_stopped and not local:
                state = item.get('State', {})
                # Docker keeps Running=true between restart-policy attempts without a process.
                if state.get('Running') is False or (
                        state.get('Restarting') is True and type(state.get('Pid')) is int and state['Pid'] == 0):
                    continue
            if len(local) != 1:
                raise FixtureError('fixture port is not uniquely bound to loopback')
            ports.append(int(local[0]['HostPort']))
        _remaining_timeout(deadline)
        return ports

    def bao(self, method: str, path: str, payload: dict | None = None, token: str | None = None, *,
            timeout: float | None = None) -> dict:
        deadline = None
        if timeout is not None:
            if timeout <= 0:
                raise FixtureError('isolated OpenBao request budget exhausted')
            deadline = time.monotonic() + timeout
        if deadline is None:
            port = self.port('openbao', 8200)[0]
        else:
            port = self.port('openbao', 8200, timeout=_remaining_timeout(deadline))[0]
            _remaining_timeout(deadline)
        context = ssl.create_default_context(cafile=str(self.state / 'tls/ca.crt'))
        request_timeout = BAO_INIT_REQUEST_TIMEOUT_SECONDS if method == 'POST' and path == '/sys/init' else 5
        if deadline is not None:
            request_timeout = _remaining_timeout(deadline)
        connection = http.client.HTTPSConnection('127.0.0.1', port, context=context, timeout=request_timeout)
        headers = {'content-type': 'application/json'}
        if token:
            headers['x-vault-token'] = token
        response = None
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            if deadline is not None:
                connection.timeout = _remaining_timeout(deadline)
            connection.request(method, '/v1' + path, body=body, headers=headers)
            transport = connection.sock
            if deadline is not None:
                remaining = _remaining_timeout(deadline)
                if transport is not None:
                    transport.settimeout(remaining)
            response = connection.getresponse()
            if deadline is not None:
                remaining = _remaining_timeout(deadline)
                if transport is not None:
                    transport.settimeout(remaining)
            data = response.read(1048577)
            if deadline is not None:
                _remaining_timeout(deadline)
            if not 200 <= response.status < 300 or len(data) > 1048576:
                raise FixtureError('isolated OpenBao request failed: ' + path + ' HTTP ' + str(response.status))
            result = json.loads(data) if data else {}
            if deadline is not None:
                _remaining_timeout(deadline)
            return result
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise FixtureError('isolated OpenBao HTTPS request unavailable') from error
        finally:
            if response is not None:
                response.close()
            connection.close()

    def bootstrap_bao(self, *, timeout: float | None = None) -> None:
        budget_deadline = None
        if timeout is not None:
            if timeout <= 0:
                raise FixtureError('fixture Bao bootstrap budget exhausted')
            budget_deadline = time.monotonic() + timeout

        def request_bao(method: str, path: str, payload: dict | None = None, token: str | None = None) -> dict:
            if budget_deadline is None:
                return self.bao(method, path, payload, token)
            result = self.bao(method, path, payload, token, timeout=_remaining_timeout(budget_deadline))
            _remaining_timeout(budget_deadline)
            return result

        if budget_deadline is None:
            self.assert_owned_resources()
        else:
            self.assert_owned_resources(timeout=_remaining_timeout(budget_deadline))
        deadline = time.monotonic() + 60
        if budget_deadline is not None:
            deadline = min(deadline, budget_deadline)
        while True:
            try:
                initialized = request_bao('GET', '/sys/init').get('initialized')
                break
            except FixtureError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FixtureError('fixture Bao listener did not start; no initialization was attempted') from None
                time.sleep(min(0.5, remaining))
        token_file = self.state / 'bao-root.token'
        if initialized is False:
            if token_file.exists() or token_file.is_symlink() or (self.state / 'bao-init-attempted.json').exists():
                raise FixtureError('fixture initialization result is uncertain; never retry/reinitialize automatically')
            new_file(self.state / 'bao-init-attempted.json', b'{"fixtureOnly":true}')
            result = request_bao('POST', '/sys/init', {'recovery_shares': 1, 'recovery_threshold': 1})
            token = result.get('root_token')
            if not isinstance(token, str) or not token:
                raise FixtureError('isolated Bao initialization response is unknown; preserve state for recovery')
            new_file(self.state / 'bao-init.json', json.dumps(result).encode())
            new_file(token_file, token.encode())
        elif initialized is not True:
            raise FixtureError('fixture Bao initialization state is unknown')
        token = read_private_secret(token_file).strip()
        health = request_bao('GET', '/sys/health')
        if health.get('initialized') is not True or health.get('sealed') is not False or not health.get('cluster_id'):
            raise FixtureError('fixture Bao is not initialized/unsealed; no API plaintext fallback')
        marker = self.state / 'bao-bootstrap.json'
        if marker.exists():
            recorded = read_owned_json(marker)
            if recorded.get('clusterId') != health['cluster_id']:
                raise FixtureError('fixture Bao cluster changed; refuse substitute envelope/keyring')
            if budget_deadline is not None:
                _remaining_timeout(budget_deadline)
            return
        # New disposable KEK/DEKs are valid ONLY in this isolated new fixture.
        request_bao('POST', '/sys/mounts/transit', {'type': 'transit'}, token)
        request_bao('POST', '/transit/keys/port-pii-kek', {'type': 'aes256-gcm96'}, token)
        request_bao('POST', '/sys/mounts/secret', {'type': 'kv', 'options': {'version': '2'}}, token)
        key_names = ('userPii', 'emailLookup', 'phoneLookup')
        encrypted = request_bao('POST', '/transit/encrypt/port-pii-kek', {'batch_input': [
            {'plaintext': base64.b64encode(secrets.token_bytes(32)).decode(),
             'associated_data': base64.b64encode(('port/api/pii-envelope/v1:' + name).encode()).decode()}
            for name in key_names
        ]}, token)
        results = encrypted.get('data', {}).get('batch_results')
        if not isinstance(results, list) or len(results) != 3 or any(not isinstance(result.get('ciphertext'), str) for result in results):
            raise FixtureError('fixture encrypted envelope generation failed; no plaintext fallback')
        request_bao('POST', '/secret/data/port/api/pii-envelope', {'data': {
            'schemaVersion': 1, 'kek': 'port-pii-kek',
            'keys': {name: result['ciphertext'] for name, result in zip(key_names, results)},
        }}, token)
        policy = (ROOT / 'openbao/policies/api-pii-envelope.hcl').read_text()
        request_bao('PUT', '/sys/policies/acl/api-pii-envelope', {'policy': policy}, token)
        request_bao('POST', '/sys/auth/approle', {'type': 'approle'}, token)
        request_bao('POST', '/auth/approle/role/api-pii-envelope', {
            'token_policies': ['api-pii-envelope'], 'token_ttl': '5m', 'token_max_ttl': '10m',
            'secret_id_ttl': '0s', 'secret_id_num_uses': 0,
        }, token)
        role = request_bao('GET', '/auth/approle/role/api-pii-envelope/role-id', token=token)['data']['role_id']
        secret = request_bao('POST', '/auth/approle/role/api-pii-envelope/secret-id', {}, token)['data']['secret_id']
        if any('\n' in value or not value for value in (role, secret)):
            raise FixtureError('fixture AppRole returned unsafe identifiers')
        # Named volume files are UID1001/mode0400, readable by actual API user.
        publish = '''set -eu
umask 077
test -d /fixture-api-credentials
test -z "$(find /fixture-api-credentials -mindepth 1 -maxdepth 1 -print -quit)"
read -r role
read -r secret
printf '%s' "$role" > /fixture-api-credentials/api-role-id
printf '%s' "$secret" > /fixture-api-credentials/api-secret-id
chown -R 1001:1001 /fixture-api-credentials
chmod 700 /fixture-api-credentials
chmod 400 /fixture-api-credentials/api-role-id /fixture-api-credentials/api-secret-id
'''
        container = self.containers('openbao',
            timeout=60 if budget_deadline is None else _remaining_timeout(budget_deadline))[0]
        self.docker('exec', '--interactive', '--user', '0:0', container, 'sh', '-ec', publish,
            data=(role + '\n' + secret + '\n').encode(),
            timeout=60 if budget_deadline is None else _remaining_timeout(budget_deadline))
        if budget_deadline is not None:
            _remaining_timeout(budget_deadline)
        new_file(marker, json.dumps({'fixtureOnly': True, 'clusterId': health['cluster_id']}).encode())
        if budget_deadline is not None:
            _remaining_timeout(budget_deadline)

    def configure_platform(self, reference: str) -> None:
        from runtime_fleet import FleetError
        from runtime_images import inspect_image
        self.assert_owned_resources()
        health = self.bao('GET', '/sys/health')
        recorded = read_owned_json(self.state / 'bao-bootstrap.json')
        if health.get('cluster_id') != recorded.get('clusterId') or health.get('sealed') is not False:
            raise FixtureError('portable fixture must use the SAME initialized cluster and static key')
        try:
            image = inspect_image(reference, worker=False)
        except FleetError as error:
            raise FixtureError(str(error)) from error
        content = ("RUNTIME_HA_PLATFORM_BAO_IMAGE='" + image['imageReference'] +
            "'\nRUNTIME_HA_EXPECTED_CLUSTER_ID='" + recorded['clusterId'] + "'\n").encode()
        destination = self.state / 'platform.env'
        if destination.exists():
            read_owned_env(destination)
            if destination.is_symlink() or destination.read_bytes() != content:
                raise FixtureError('portable Bao image/cluster already pinned; never replace with substitute data/key')
        else:
            new_file(destination, content)
        # Stop the original writer BEFORE another server touches its raft data.
        self.compose('stop', 'openbao')
        self.platform = True
        self.compose('up', '--detach', '--no-deps', '--force-recreate', '--pull', 'never',
            '--wait', '--wait-timeout', '90', 'openbao', timeout=120)
        self.bootstrap_bao()

    def configure_runtime(self, api_image: str, migrator_image: str, a_path: Path, b_path: Path, replicas: int) -> None:
        from runtime_fleet import FleetError, load_json, require_compatible, require_inventory
        from runtime_images import inspect_image
        self.assert_state_files()
        if replicas < 2:
            raise FixtureError('real fleet fixture requires at least two incarnations per pool')
        values = {'RUNTIME_HA_POOL_REPLICAS': str(replicas)}
        inventories: dict[str, dict] = {}
        try:
            for label, reference in (('API', api_image), ('MIGRATOR', migrator_image)):
                inspected = inspect_image(reference, worker=False)
                values['RUNTIME_HA_' + label + '_IMAGE'] = inspected['imageReference']
            for label, path in (('A', a_path), ('B', b_path)):
                inventory = load_json(path)
                require_inventory(inventory)
                actual = inspect_image(inventory.get('imageReference', inventory['imageDigest']))
                if any(actual.get(name) != inventory.get(name) for name in ('protocolRevision', 'imageDigest', 'buildId', 'compatibilityFingerprint', 'sdkPatchDigest', 'builtinInventoryDigest', 'modelCacheSelectionDigest', 'checkpointCodec')):
                    raise FixtureError('runtime fixture image differs from recorded immutable inventory')
                inventories[label] = actual
            require_compatible(inventories['A'], inventories['B'])
        except FleetError as error:
            raise FixtureError(str(error)) from error
        publications: list[tuple[Path, bytes]] = []
        for label, actual in inventories.items():
            pinned = self.state / ('worker-' + label.lower() + '.inventory.json')
            if pinned.exists() or pinned.is_symlink():
                if read_owned_json(pinned) != actual:
                    raise FixtureError('owned baked inventory already pinned; never silently replace it')
            else:
                publications.append((pinned, json.dumps(actual, sort_keys=True, separators=(',', ':')).encode()))
            prefix = 'RUNTIME_HA_WORKER_' + label + '_'
            values.update({prefix + 'IMAGE': actual['imageReference'], prefix + 'BUILD_ID': actual['buildId'],
                prefix + 'FINGERPRINT': actual['compatibilityFingerprint'], prefix + 'DIGEST': actual['imageDigest']})
        destination = self.state / 'runtime.env'
        content = ''.join(name + "='" + value + "'\n" for name, value in values.items()).encode()
        if destination.exists() or destination.is_symlink():
            read_owned_env(destination)
            if destination.read_bytes() != content:
                raise FixtureError('runtime fixture inventory already pinned; never silently overwrite')
        else:
            publications.append((destination, content))
        for path, encoded in publications:
            new_file(path, encoded)
        self.runtime = True

    def connections(self) -> dict:
        value = {'fixtureOnly': True, 'project': self.project, 'owner': self.owner,
            'postgresPort': self.port('postgres', 5432)[0], 'redisPort': self.port('redis', 6379)[0],
            'livekitPort': self.port('livekit', 7880)[0], 'openbaoPort': self.port('openbao', 8200)[0],
            'openbaoCaFile': str(self.state / 'tls/ca.crt'), 'secretsFile': str(self.state / 'compose.env'),
            'openbaoBootstrapFile': str(self.state / 'bao-bootstrap.json')}
        if self.platform:
            value['openbaoHealthPort'] = self.port('openbao', 8000)[0]
        if self.runtime:
            value.update({'apiPorts': self.port('api', 8000), 'grpcPorts': self.port('api', 8080),
                'poolAPorts': self.port('pool-a', 8000), 'poolBPorts': self.port('pool-b', 8000)})
        return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--project', required=True)
    up = sub.add_parser('up')
    up.add_argument('--api-image')
    up.add_argument('--migrator-image')
    up.add_argument('--inventory-a', type=Path)
    up.add_argument('--inventory-b', type=Path)
    up.add_argument('--replicas', type=int, default=2)
    up.add_argument('--platform-image', help='same fixture raft/key with production /dev/shm+PORT image')
    sub.add_parser('status')
    fault = sub.add_parser('fault')
    fault.add_argument('--service', choices=DATA_SERVICES + RUNTIME_SERVICES, required=True)
    fault.add_argument('--mode', choices=('stop', 'pause', 'sigterm', 'sigkill'), default='stop')
    restore = sub.add_parser('restore')
    restore.add_argument('--service', choices=DATA_SERVICES + RUNTIME_SERVICES, required=True)
    restore.add_argument('--paused', action='store_true')
    restore.add_argument('--restart', action='store_true', help='explicit same-key Bao restart after sealing')
    sub.add_parser('redis-loss')
    sub.add_parser('bao-seal')
    down = sub.add_parser('down')
    down.add_argument('--remove-owned-volumes', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            manifest = prepare(args.state_dir, args.project)
            print(json.dumps({'fixtureOnly': True, 'project': manifest['project'], 'stateDir': manifest['stateDir'], 'next': 'up'}))
            return 0
        state = validate_state_path(args.state_dir)
        fixtures = Fixtures(state, runtime=(state / 'runtime.env').is_file())
        fixtures.assert_owned_resources()
        if args.command == 'up':
            supplied = (args.api_image, args.migrator_image, args.inventory_a, args.inventory_b)
            if any(supplied) and not all(supplied):
                raise FixtureError('full runtime requires API/migrator images and both pool inventories together')
            # Start data without a sealed-ready dependency. Initialization belongs
            # only to positively owned empty fixture raft data.
            was_runtime = fixtures.runtime
            fixtures.runtime = False
            fixtures.compose('up', '--detach', '--pull', 'never', *DATA_SERVICES)
            fixtures.bootstrap_bao()
            fixtures.runtime = was_runtime
            if args.platform_image:
                fixtures.configure_platform(args.platform_image)
            if all(supplied):
                fixtures.configure_runtime(*supplied, args.replicas)
            if fixtures.runtime:
                fixtures.compose('up', '--detach', '--pull', 'never', '--wait', '--wait-timeout', '180', 'api', 'pool-a', 'pool-b', timeout=240)
            print(json.dumps(fixtures.connections(), indent=2))
        elif args.command == 'status':
            print(json.dumps(fixtures.connections(), indent=2))
        elif args.command == 'fault':
            ids = fixtures.containers(args.service)
            if args.mode in ('sigterm', 'sigkill'):
                fixtures.docker('kill', '--signal', 'TERM' if args.mode == 'sigterm' else 'KILL', *ids)
            else:
                fixtures.compose(args.mode, args.service)
            print(json.dumps({'fixtureOnly': True, 'fault': args.mode, 'service': args.service}))
        elif args.command == 'restore':
            fixtures.containers(args.service)
            if args.paused and args.restart:
                raise FixtureError('choose unpause or explicit restart, not both')
            fixtures.compose('unpause' if args.paused else ('restart' if args.restart else 'start'), args.service)
            print(json.dumps({'fixtureOnly': True, 'restored': args.service}))
        elif args.command == 'redis-loss':
            fixtures.containers('redis')
            fixtures.compose('exec', '-T', 'redis', 'redis-cli', 'FLUSHALL', 'SYNC')
            print(json.dumps({'fixtureOnly': True, 'redisState': 'intentionally lost', 'providerDurability': 'unproven'}))
        elif args.command == 'bao-seal':
            fixtures.containers('openbao')
            token = read_private_secret(state / 'bao-root.token').strip()
            fixtures.bao('PUT', '/sys/seal', {}, token)
            print(json.dumps({'fixtureOnly': True, 'openbao': 'sealed', 'restore': 'explicit restart of THIS fixture only'}))
        elif args.command == 'down':
            command = ['down', '--remove-orphans']
            if args.remove_owned_volumes:
                command.append('--volumes')
            fixtures.compose(*command)
            print(json.dumps({'fixtureOnly': True, 'stopped': fixtures.project, 'ownedVolumesRemoved': args.remove_owned_volumes, 'externalStateRetained': str(state)}))
        return 0
    except (FixtureError, OSError, ValueError, KeyError, TypeError) as error:
        reason = str(error) if isinstance(error, FixtureError) else 'fixture state/provider response unavailable; no mutation proof'
        print(json.dumps({'status': 'blocked', 'reason': reason}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
