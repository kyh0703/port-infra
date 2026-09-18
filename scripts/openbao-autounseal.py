#!/usr/bin/env python3
"""Deliver a Keychain seal key to OpenBao tmpfs after macOS login."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

LABEL = 'com.port.infra.openbao-autounseal'
DEFAULT_INSTALL_DIR = Path.home() / '.local/share/port-openbao-autounseal'
DEFAULT_LOG_DIR = Path.home() / 'Library/Logs/port-openbao-autounseal'
RUNTIME_DIR = '/bao/seal-runtime'
KEY_FILE = RUNTIME_DIR + '/current.key'


class AutoUnsealError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    infra_root: Path
    docker: Path
    colima: Path
    helper: Path
    context: str = 'colima'
    project: str = 'infra'

    @property
    def container(self):
        return self.project + '-openbao-1'


def run_command(command, *, input=None, timeout=15):
    try:
        return subprocess.run(command, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise AutoUnsealError('local helper command unavailable or timed out') from None


KEY_READY = 'test -f /bao/seal-runtime/current.key && test ! -L /bao/seal-runtime/current.key && test "$(wc -c < /bao/seal-runtime/current.key)" -eq 32'
DELIVER_KEY = r'''set -eu
umask 077
test -d /bao/seal-runtime
test ! -L /bao/seal-runtime
temporary=$(mktemp /bao/seal-runtime/.incoming.XXXXXXXX)
trap 'rm -f "$temporary"' EXIT
cat > "$temporary"
test "$(wc -c < "$temporary")" -eq 32
chown openbao:openbao "$temporary"
chmod 400 "$temporary"
if ! ln "$temporary" /bao/seal-runtime/current.key 2>/dev/null; then
  test -f /bao/seal-runtime/current.key
  test ! -L /bao/seal-runtime/current.key
  test "$(wc -c < /bao/seal-runtime/current.key)" -eq 32
fi
'''


class Controller:
    def __init__(self, config: Config, run=run_command):
        self.config = config
        self.run = run

    def docker_command(self, *args):
        return [str(self.config.docker), '--context', self.config.context, *args]

    def ensure_runtime(self):
        """At agent launch only: start Colima if needed, then the configured Bao."""
        if not static_mode_enabled(self.config.infra_root):
            raise AutoUnsealError('OPENBAO_SEAL_MODE=static must be configured before starting the agent')
        result = self.run(self.docker_command('info', '--format', '{{.ServerVersion}}'))
        if result.returncode:
            result = self.run([str(self.config.colima), 'start', '--activate=false'], timeout=180)
            if result.returncode:
                raise AutoUnsealError('Colima startup failed')
        command = self.docker_command('compose', '--project-directory', str(self.config.infra_root),
            '-f', str(self.config.infra_root / 'compose.yml'), '-p', self.config.project,
            'up', '-d', '--no-deps', '--pull', 'never', 'openbao')
        if self.run(command, timeout=90).returncode:
            raise AutoUnsealError('OpenBao container startup failed')

    def step(self):
        result = self.run(self.docker_command('inspect', self.config.container))
        if result.returncode:
            raise AutoUnsealError('OpenBao container is unavailable')
        try:
            items = json.loads(result.stdout)
            item = items[0]
            labels = item['Config'].get('Labels') or {}
            environment = dict(value.split('=', 1) for value in item['Config'].get('Env', []) if '=' in value)
            mounts = item.get('Mounts', [])
            target_mounts = [mount for mount in mounts if mount.get('Destination') == RUNTIME_DIR]
            memory_mount = (RUNTIME_DIR in (item.get('HostConfig', {}).get('Tmpfs') or {})
                or any(mount.get('Type') == 'tmpfs' for mount in target_mounts))
            if (labels.get('com.docker.compose.project') != self.config.project
                or labels.get('com.docker.compose.service') != 'openbao'
                or environment.get('OPENBAO_SEAL_MODE') != 'static'
                or not memory_mount
                or any(mount.get('Type') != 'tmpfs' for mount in target_mounts)):
                raise AutoUnsealError('refusing key delivery to an unexpected container or non-tmpfs mount')
            running = item['State']['Running']
        except (ValueError, KeyError, IndexError, TypeError):
            raise AutoUnsealError('OpenBao container inspection failed') from None
        if not running:
            return 'stopped'
        prefix = self.docker_command('exec', '--user', '0:0', self.config.container, 'sh', '-ec')
        if self.run([*prefix, KEY_READY]).returncode == 0:
            return 'ready'
        result = self.run([str(self.config.helper), 'emit'])
        if result.returncode or len(result.stdout) != 32:
            raise AutoUnsealError('Keychain seal key unavailable; automatic key generation is disabled')
        key = bytearray(result.stdout)
        del result
        try:
            command = self.docker_command('exec', '--interactive', '--user', '0:0', self.config.container, 'sh', '-ec', DELIVER_KEY)
            if self.run(command, input=bytes(key)).returncode:
                raise AutoUnsealError('OpenBao tmpfs key delivery failed')
        finally:
            key[:] = b'\0' * len(key)
        return 'injected'


def static_mode_enabled(root: Path):
    try:
        lines = (root / '.env').read_text().splitlines()
    except OSError:
        return False
    value = None
    for line in lines:
        name, separator, raw = line.removeprefix('export ').partition('=')
        if separator and name.strip() == 'OPENBAO_SEAL_MODE':
            value = raw.strip().strip('\"\'')
    return value == 'static'


def secure_directory(path: Path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise AutoUnsealError('installation directory must be a real directory owned by this user')
    path.chmod(0o700)


def atomic_write(path: Path, data: bytes, mode=0o600):
    if path.is_symlink():
        raise AutoUnsealError('refusing to replace a symbolic link')
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def prepare_helper(root: Path, install_dir: Path):
    if sys.platform != 'darwin':
        raise AutoUnsealError('macOS is required for Keychain integration')
    secure_directory(install_dir)
    source = root / 'openbao/macos/keychain-helper.swift'
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = install_dir / 'keychain-helper'
    recorded = install_dir / 'keychain-helper.source.sha256'
    if destination.exists():
        if destination.is_symlink() or not recorded.exists() or recorded.read_text().strip() != fingerprint:
            raise AutoUnsealError('installed Keychain helper differs; migrate its trust before replacing it')
        return destination
    temporary = install_dir / '.keychain-helper.build'
    try:
        if run_command(['/usr/bin/swiftc', '-O', str(source), '-o', str(temporary)], timeout=120).returncode:
            raise AutoUnsealError('Keychain helper compilation failed')
        temporary.chmod(0o700)
        os.replace(temporary, destination)
        if run_command(['/usr/bin/codesign', '--force', '--sign', '-', '--identifier', 'com.port.infra.openbao-keychain-helper', str(destination)]).returncode:
            destination.unlink()
            raise AutoUnsealError('Keychain helper signing failed')
        atomic_write(recorded, (fingerprint + '\n').encode())
        return destination
    finally:
        if temporary.exists(): temporary.unlink()


def launch_agent(config: Config, script: Path, python: Path, logs: Path):
    return {
        'Label': LABEL,
        'ProgramArguments': [str(python), str(script), 'serve', '--infra-root', str(config.infra_root),
            '--helper', str(config.helper), '--docker', str(config.docker), '--colima', str(config.colima)],
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 15,
        'ProcessType': 'Background',
        'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
        'StandardOutPath': str(logs / 'supervisor.log'),
        'StandardErrorPath': str(logs / 'supervisor-error.log'),
    }


def keychain_control(helper: Path, command: str, install_dir: Path):
    """Only status-producing operations may run through temporary GUI job logs."""
    if command not in {'init', 'check'}:
        raise AutoUnsealError('only init/check may use the Keychain control job')
    secure_directory(install_dir)
    with tempfile.TemporaryDirectory(prefix='.keychain-control-', dir=install_dir) as folder:
        directory = Path(folder)
        label = LABEL + '.control.' + uuid.uuid4().hex
        domain = 'gui/' + str(os.getuid())
        stdout, stderr = directory / 'status.out', directory / 'status.err'
        atomic_write(stdout, b'')
        atomic_write(stderr, b'')
        job = {'Label': label, 'ProgramArguments': [str(helper), command], 'RunAtLoad': True,
            'StandardOutPath': str(stdout), 'StandardErrorPath': str(stderr)}
        plist = directory / 'control.plist'
        atomic_write(plist, plistlib.dumps(job))
        if run_command(['/bin/launchctl', 'bootstrap', domain, str(plist)]).returncode:
            raise AutoUnsealError('Keychain control requires an active macOS login session')
        try:
            for _ in range(60):
                status = run_command(['/bin/launchctl', 'print', domain + '/' + label])
                text = status.stdout.decode(errors='replace')
                if 'state = not running' in text:
                    matched = re.search(r'last exit code = ([0-9]+)', text)
                    if matched:
                        if int(matched.group(1)) == 0:
                            return
                        raise AutoUnsealError('Keychain access failed in the login session; no key was replaced')
                time.sleep(0.25)
            raise AutoUnsealError('Keychain control job did not complete')
        finally:
            run_command(['/bin/launchctl', 'bootout', domain + '/' + label])


def install(config: Config, install_dir: Path, logs: Path):
    if config.project != 'infra' or config.context != 'colima':
        raise AutoUnsealError('LaunchAgent installation is limited to the infra Colima stack')
    if not static_mode_enabled(config.infra_root):
        raise AutoUnsealError('enable static seal mode after preparing the migration')
    keychain_control(config.helper, 'check', install_dir)
    secure_directory(install_dir)
    secure_directory(logs)
    script = install_dir / 'openbao-autounseal.py'
    atomic_write(script, Path(__file__).read_bytes(), 0o700)
    for name in ['supervisor.log', 'supervisor-error.log']:
        path = logs / name
        if not path.exists(): atomic_write(path, b'')
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            raise AutoUnsealError('log files must belong to the current user')
        path.chmod(0o600)
    agents = Path.home() / 'Library/LaunchAgents'
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / (LABEL + '.plist')
    python = Path(shutil.which('python3') or sys.executable)
    atomic_write(plist, plistlib.dumps(launch_agent(config, script, python, logs)))
    domain = 'gui/' + str(os.getuid())
    run_command(['/bin/launchctl', 'bootout', domain + '/' + LABEL])
    if run_command(['/bin/launchctl', 'bootstrap', domain, str(plist)]).returncode:
        raise AutoUnsealError('LaunchAgent bootstrap failed')
    print('OpenBao Keychain LaunchAgent installed')


def serve(config: Config):
    lock_path = config.helper.parent / 'supervisor.lock'
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AutoUnsealError('another auto-unseal supervisor is running') from None
        controller = Controller(config)
        started = False
        previous = None
        while True:
            try:
                if not started:
                    controller.ensure_runtime()
                    started = True
                state = controller.step()
            except AutoUnsealError as error:
                state = str(error)
            if state != previous:
                print('OpenBao auto-unseal: ' + state, flush=True)
                previous = state
            time.sleep(3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'setup-key', 'once', 'install', 'serve', 'stop'])
    parser.add_argument('--infra-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--install-dir', type=Path, default=DEFAULT_INSTALL_DIR)
    parser.add_argument('--logs', type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument('--helper', type=Path)
    parser.add_argument('--docker', type=Path, default=Path(shutil.which('docker') or '/opt/homebrew/bin/docker'))
    parser.add_argument('--colima', type=Path, default=Path(shutil.which('colima') or '/opt/homebrew/bin/colima'))
    parser.add_argument('--context', default='colima')
    parser.add_argument('--project', default='infra')
    args = parser.parse_args()
    try:
        if args.command == 'stop':
            run_command(['/bin/launchctl', 'bootout', 'gui/' + str(os.getuid()) + '/' + LABEL])
            print('OpenBao supervisor stopped; Keychain item preserved')
            return 0
        helper = args.helper or args.install_dir / 'keychain-helper'
        if args.command in ['prepare', 'setup-key', 'install']:
            helper = prepare_helper(args.infra_root, args.install_dir)
        config = Config(args.infra_root.resolve(), args.docker, args.colima, helper, args.context, args.project)
        if args.command == 'prepare':
            print('Keychain helper prepared at ' + str(helper))
        elif args.command == 'setup-key':
            # After migration, never generate a replacement for a lost seal key.
            command = 'check' if static_mode_enabled(args.infra_root) else 'init'
            keychain_control(helper, command, args.install_dir)
            print('Keychain seal key verified')
        elif args.command == 'once':
            print('OpenBao auto-unseal: ' + Controller(config).step())
        elif args.command == 'install':
            install(config, args.install_dir, args.logs)
        elif args.command == 'serve':
            serve(config)
        return 0
    except (AutoUnsealError, OSError) as error:
        message = str(error) if isinstance(error, AutoUnsealError) else 'local filesystem or process operation failed'
        print('OpenBao auto-unseal failed: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
