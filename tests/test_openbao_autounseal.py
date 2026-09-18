import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/openbao-autounseal.py'


def load_module():
    spec = importlib.util.spec_from_file_location('openbao_autounseal', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AutoUnsealControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def state(self):
        return {'State': {'Running': True}, 'Config': {
            'Env': ['OPENBAO_SEAL_MODE=static'],
            'Labels': {'com.docker.compose.project': 'infra', 'com.docker.compose.service': 'openbao'},
        }, 'Mounts': [{'Type': 'tmpfs', 'Destination': '/bao/seal-runtime'}]}

    def controller(self, state=None, existing=False, key=b'x' * 32):
        calls = []
        def run(command, *, input=None, timeout=15):
            calls.append((command, input))
            if 'inspect' in command:
                return subprocess.CompletedProcess(command, 0, json.dumps([state or self.state()]).encode(), b'')
            if command[-1] == 'emit':
                return subprocess.CompletedProcess(command, 0 if key is not None else 1, key or b'', b'')
            if 'exec' in command:
                return subprocess.CompletedProcess(command, 0 if input is not None or existing else 1, b'', b'')
            raise AssertionError('unexpected command')
        config = self.module.Config(Path('/infra'), Path('/docker'), Path('/colima'), Path('/keychain-helper'))
        return self.module.Controller(config, run=run), calls

    def test_key_is_sent_only_on_stdin_to_the_validated_tmpfs(self):
        controller, calls = self.controller()
        self.assertEqual(controller.step(), 'injected')
        deliveries = [(cmd, data) for cmd, data in calls if data is not None]
        self.assertEqual(len(deliveries), 1)
        command, data = deliveries[0]
        self.assertEqual(data, b'x' * 32)
        self.assertIn('--interactive', command)
        self.assertIn('colima', command)
        self.assertNotIn('x' * 32, ' '.join(command))
        self.assertIn('/bao/seal-runtime/current.key', command[-1])

    def test_existing_key_does_not_access_keychain_or_unseal_a_manually_sealed_server(self):
        controller, calls = self.controller(existing=True)
        self.assertEqual(controller.step(), 'ready')
        self.assertFalse(any(cmd[-1] == 'emit' for cmd, _ in calls))
        self.assertFalse(any(data is not None for _, data in calls))

    def test_compose_tmpfs_is_recognized_from_host_config(self):
        state = self.state()
        state['Mounts'] = []
        state['HostConfig'] = {'Tmpfs': {'/bao/seal-runtime': 'rw,noexec,nosuid,size=1m'}}
        controller, calls = self.controller(state=state)
        self.assertEqual(controller.step(), 'injected')
        self.assertTrue(any(data is not None for _, data in calls))

    def test_wrong_container_identity_or_persistent_mount_fails_before_key_retrieval(self):
        for mismatch in ['project', 'mode', 'mount']:
            with self.subTest(mismatch=mismatch):
                state = self.state()
                if mismatch == 'project': state['Config']['Labels']['com.docker.compose.project'] = 'another-project'
                if mismatch == 'mode': state['Config']['Env'] = ['OPENBAO_SEAL_MODE=shamir']
                if mismatch == 'mount': state['Mounts'][0]['Type'] = 'volume'
                controller, calls = self.controller(state=state)
                with self.assertRaises(self.module.AutoUnsealError): controller.step()
                self.assertFalse(any(cmd[-1] == 'emit' for cmd, _ in calls))

    def test_missing_locked_or_malformed_key_never_reaches_container(self):
        for value in [None, b'', b'x' * 31, b'x' * 33]:
            with self.subTest(size=None if value is None else len(value)):
                controller, calls = self.controller(key=value)
                with self.assertRaises(self.module.AutoUnsealError) as caught: controller.step()
                self.assertNotIn('x' * 31, str(caught.exception))
                self.assertFalse(any(data is not None for _, data in calls))

    def test_stopped_container_is_left_stopped(self):
        state = self.state(); state['State']['Running'] = False
        controller, calls = self.controller(state=state)
        self.assertEqual(controller.step(), 'stopped')
        self.assertEqual(len(calls), 1)

    def test_launch_agent_uses_stable_paths_and_no_secret_environment(self):
        module = self.module
        config = module.Config(Path('/infra'), Path('/docker'), Path('/colima'), Path('/private/keychain-helper'))
        agent = module.launch_agent(config, Path('/private/openbao-autounseal.py'), Path('/python'), Path('/private/logs'))
        self.assertTrue(agent['RunAtLoad'])
        self.assertTrue(agent['KeepAlive'])
        self.assertIn('serve', agent['ProgramArguments'])
        self.assertEqual(set(agent['EnvironmentVariables']), {'PATH'})
        self.assertIn('/opt/homebrew/bin', agent['EnvironmentVariables']['PATH'])
        self.assertNotIn('init', agent['ProgramArguments'])

    def test_login_bootstrap_starts_colima_without_changing_existing_vm_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').write_text('OPENBAO_SEAL_MODE=static\n')
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 1 if 'info' in command else 0, b'', b'')
            config = self.module.Config(root, Path('/docker'), Path('/colima'), Path('/helper'))
            self.module.Controller(config, run=run).ensure_runtime()
            self.assertEqual(calls[1], ['/colima', 'start', '--activate=false'])
            self.assertIn('--no-deps', calls[2])
            self.assertIn('openbao', calls[2])

    def test_emit_cannot_be_redirected_to_control_job_logs(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(self.module.AutoUnsealError):
                self.module.keychain_control(Path('/helper'), 'emit', Path(folder))
            self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
