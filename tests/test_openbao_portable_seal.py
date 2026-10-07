import base64
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLATFORM_ENTRYPOINT = ROOT / 'openbao/platform-entrypoint.sh'
IMAGE = 'ghcr.io/openbao/openbao:2.6.2'


class PortableExistingSealTests(unittest.TestCase):
    def run_portable(self, *, key=b'fixture-seal-key-for-smoke-only!', use_env=False, preexisting=None, symlink=False):
        self.assertTrue(PLATFORM_ENTRYPOINT.is_file(), 'portable same-key static seal entrypoint is missing')
        if shutil.which('docker') is None:
            self.skipTest('docker is unavailable')
        image = subprocess.run(['docker', 'image', 'inspect', IMAGE], capture_output=True)
        if image.returncode:
            self.skipTest('OpenBao fixture image is unavailable')
        # Colima shares the checkout's home directory, not macOS /var/folders.
        # Keep synthetic fixtures on the same shared root as the script mount.
        with tempfile.TemporaryDirectory(prefix='port-ha-seal-test-', dir=ROOT / 'tests') as folder:
            directory = Path(folder)
            (directory / 'seal').write_bytes(key)
            (directory / 'seal').chmod(0o400)
            if symlink:
                (directory / 'source').symlink_to('seal')
            source_name = 'source' if symlink else 'seal'
            environment = os.environ.copy()
            arguments = [
                'docker', 'run', '--rm', '--network', 'none', '--user', '0:0',
                '--entrypoint', 'sh', '--tmpfs', '/bao/seal-runtime',
                '--volume', str(PLATFORM_ENTRYPOINT) + ':/portable-entrypoint:ro',
                '--volume', str(directory) + ':/run/seal-source:ro',
            ]
            if use_env:
                environment['OPENBAO_STATIC_SEAL_KEY_B64'] = base64.b64encode(key).decode()
                arguments.extend(['--env', 'OPENBAO_STATIC_SEAL_KEY_B64'])
            else:
                arguments.extend(['--env', 'OPENBAO_STATIC_SEAL_FILE=/run/seal-source/' + source_name])
            prepare = ''
            if preexisting is not None:
                (directory / 'existing').write_bytes(preexisting)
                prepare = 'cp /run/seal-source/existing /bao/seal-runtime/current.key; '
            command = prepare + '''sh /portable-entrypoint sh -ec '
                cmp -s /bao/seal-runtime/current.key /run/seal-source/seal
                test "$(stat -c %a /bao/seal-runtime/current.key)" = 400
                test "$(stat -c %U /bao/seal-runtime/current.key)" = openbao
                test -z "${OPENBAO_STATIC_SEAL_KEY_B64:-}"
                printf "same-existing-key-ready\\n"
            '\n'''
            return subprocess.run([*arguments, IMAGE, '-ec', command], env=environment, text=True, capture_output=True)

    def test_file_and_platform_secret_preserve_the_exact_existing_32_byte_key(self):
        for use_env in (False, True):
            with self.subTest(use_env=use_env):
                result = self.run_portable(use_env=use_env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'same-existing-key-ready')
                self.assertNotIn('fixture-seal-key', result.stdout + result.stderr)
                self.assertNotIn(base64.b64encode(b'fixture-seal-key-for-smoke-only!').decode(), result.stdout + result.stderr)

    def test_wrong_length_and_symlink_fail_without_starting_bao(self):
        for values in ({'key': b'x' * 31}, {'symlink': True}):
            with self.subTest(values=values):
                result = self.run_portable(**values)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('same-existing-key-ready', result.stdout)

    def test_existing_tmpfs_key_is_never_replaced_by_a_new_key(self):
        same = self.run_portable(preexisting=b'fixture-seal-key-for-smoke-only!')
        self.assertEqual(same.returncode, 0, same.stderr)
        different = self.run_portable(preexisting=b'z' * 32)
        self.assertNotEqual(different.returncode, 0)
        self.assertIn('does not match', different.stderr)
        self.assertNotIn('same-existing-key-ready', different.stdout)


if __name__ == '__main__':
    unittest.main()
