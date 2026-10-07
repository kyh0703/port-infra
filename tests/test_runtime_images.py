import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import runtime_images as images


class ImageDockerSafetyTests(unittest.TestCase):
    """Exercise the real image CLI against a Docker process double, not native image proof."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='port-ha-image-safety-unit-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.source = self.folder / 'source'
        self.source.mkdir()
        (self.source / 'Dockerfile').write_text('FROM scratch\n')
        self.output = self.folder / 'inventory.json'
        self.endpoint = 'unix:///isolated-unit-docker.sock'
        self.image_activity = []
        self.digest = 'sha256:' + '1' * 64
        self.reference = 'port-runtime-ha-isolated-worker@' + self.digest
        self.inventory = {
            'protocolRevision': 'runtime-recovery-v1', 'buildId': 'isolated-unit-build',
            'compatibilityFingerprint': 'a' * 64, 'sdkPatchDigest': '2' * 64,
            'builtinInventoryDigest': '3' * 64, 'modelCacheSelectionDigest': '4' * 64,
            'checkpointCodec': 'port-runtime-checkpoint-v1',
        }
        environment = patch.dict(os.environ, {'DOCKER_HOST': '', 'DOCKER_CONTEXT': 'isolated-unit-local'})
        environment.start()
        self.addCleanup(environment.stop)
        process = patch.object(subprocess, 'run', side_effect=self.docker_process)
        process.start()
        self.addCleanup(process.stop)

    def docker_process(self, command, **kwargs):
        self.assertEqual(command[0], 'docker', 'only the isolated Docker process boundary is doubled')
        if command[1:3] == ['context', 'inspect']:
            data = [{'Endpoints': {'docker': {'Host': self.endpoint}}}]
        elif command[1:3] == ['image', 'inspect']:
            self.image_activity.append(tuple(command[1:]))
            data = [{'Id': self.digest, 'RepoDigests': [self.reference]}]
        elif command[1] == 'run':
            self.image_activity.append(tuple(command[1:]))
            data = self.inventory
        elif command[1] == 'build':
            self.image_activity.append(tuple(command[1:]))
            return subprocess.CompletedProcess(command, 0, b'', b'')
        else:
            self.fail('unexpected Docker process outside the isolated image boundary')
        return subprocess.CompletedProcess(command, 0, json.dumps(data).encode(), b'')

    def arguments(self, operation):
        if operation == 'inspect':
            return ['inspect', '--image', 'port-runtime-ha-isolated-worker', '--output', str(self.output)]
        return ['build', '--source', str(self.source), '--tag', 'port-runtime-ha-isolated-worker',
            '--kind', 'worker', '--build-id', self.inventory['buildId'], '--output', str(self.output)]

    def assert_blocked_before_image_activity(self, operation):
        output = io.StringIO()
        error = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            result = images.main(self.arguments(operation))
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(error.getvalue())['status'], 'blocked')
        self.assertEqual(output.getvalue(), '')
        self.assertFalse(self.output.exists(), 'rejected daemon must not publish an inventory')
        self.assertEqual(self.image_activity, [], 'guard must run before any image/container activity')

    def test_ambient_docker_host_blocks_build_and_inspection_even_with_local_context(self):
        for operation in ('build', 'inspect'):
            for host in ('tcp://remote-unit.invalid:2375', 'ssh://remote-unit.invalid', 'unix:///ambient-unit.sock'):
                with self.subTest(operation=operation, host=host):
                    self.image_activity.clear()
                    self.output.unlink(missing_ok=True)
                    with patch.dict(os.environ, {'DOCKER_HOST': host}):
                        self.assert_blocked_before_image_activity(operation)

    def test_non_unix_context_blocks_build_and_inspection_before_image_activity(self):
        for operation in ('build', 'inspect'):
            for endpoint in ('tcp://remote-unit.invalid:2375', 'ssh://remote-unit.invalid', 'npipe:////./pipe/docker_engine'):
                with self.subTest(operation=operation, endpoint=endpoint):
                    self.endpoint = endpoint
                    self.image_activity.clear()
                    self.output.unlink(missing_ok=True)
                    self.assert_blocked_before_image_activity(operation)

    def test_local_unix_context_still_builds_and_inspects_immutable_worker_inventory(self):
        for operation in ('build', 'inspect'):
            with self.subTest(operation=operation):
                self.image_activity.clear()
                self.output.unlink(missing_ok=True)
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                    result = images.main(self.arguments(operation))
                self.assertEqual(result, 0)
                recorded = json.loads(self.output.read_text())
                self.assertEqual(recorded['imageReference'], self.reference)
                self.assertEqual(recorded['imageDigest'], self.digest)
                self.assertEqual(recorded['buildId'], self.inventory['buildId'])
                self.assertEqual(json.loads(output.getvalue()), recorded)
                actions = [command[0] for command in self.image_activity]
                self.assertIn('image', actions)
                self.assertIn('run', actions)
                self.assertEqual(actions.count('build'), 1 if operation == 'build' else 0)


if __name__ == '__main__':
    unittest.main()
