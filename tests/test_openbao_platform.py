import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TLS_SCRIPT = ROOT / "scripts" / "openbao-tls.sh"
OPENBAO_SCRIPT = ROOT / "scripts" / "openbao.sh"


class OpenBaoTlsPreparationTests(unittest.TestCase):
    def run_tls(self, directory):
        environment = os.environ.copy()
        environment["OPENBAO_TLS_DIR"] = str(directory)
        return subprocess.run(
            ["bash", str(TLS_SCRIPT)],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )

    def test_generates_server_certificate_with_required_sans_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "tls"
            first = self.run_tls(directory)
            self.assertEqual(first.returncode, 0, first.stderr)

            expected = {"ca.crt", "server.crt", "server.key"}
            self.assertEqual({path.name for path in directory.iterdir()}, expected)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual((directory / "server.key").stat().st_mode & 0o777, 0o600)
            self.assertEqual((directory / "ca.crt").stat().st_mode & 0o777, 0o644)
            self.assertEqual((directory / "server.crt").stat().st_mode & 0o777, 0o644)

            certificate = subprocess.run(
                [
                    "openssl",
                    "x509",
                    "-in",
                    str(directory / "server.crt"),
                    "-noout",
                    "-text",
                ],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            self.assertRegex(certificate, r"DNS:openbao")
            self.assertRegex(certificate, r"DNS:localhost")
            self.assertRegex(certificate, r"IP Address:127\.0\.0\.1")

            before = {
                path.name: path.read_bytes()
                for path in directory.iterdir()
            }
            second = self.run_tls(directory)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                before,
                {path.name: path.read_bytes() for path in directory.iterdir()},
            )

    def test_refuses_to_replace_a_partial_pki_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "tls"
            directory.mkdir(parents=True)
            (directory / "server.key").write_text("test-only-placeholder\n")

            result = self.run_tls(directory)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("incomplete", (result.stdout + result.stderr).lower())
            self.assertEqual(
                (directory / "server.key").read_text(),
                "test-only-placeholder\n",
            )


class OpenBaoHelperContractTests(unittest.TestCase):
    def test_snapshot_directory_override_is_exported_to_compose(self):
        with tempfile.TemporaryDirectory() as folder:
            folder_path = Path(folder)
            fake_compose = folder_path / "compose"
            captured = folder_path / "compose-env.txt"
            fake_compose.write_text(
                "#!/bin/sh\n"
                "printf '%s' \"$OPENBAO_SNAPSHOT_DIR\" > \"$OPENBAO_CAPTURE\"\n"
            )
            fake_compose.chmod(0o755)
            snapshot_directory = folder_path / "snapshots"
            environment = os.environ.copy()
            environment.update(
                {
                    "COMPOSE": str(fake_compose),
                    "OPENBAO_CAPTURE": str(captured),
                    "OPENBAO_SNAPSHOT_DIR": str(snapshot_directory),
                    "OPENBAO_TLS_DIR": str(folder_path / "tls"),
                }
            )

            result = subprocess.run(
                ["bash", str(OPENBAO_SCRIPT), "up"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(captured.read_text(), str(snapshot_directory.resolve()))
            self.assertEqual(snapshot_directory.stat().st_mode & 0o777, 0o700)

    def test_init_requires_external_owner_only_output_and_does_not_print_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            folder_path = Path(folder)
            fake_compose = Path(folder) / "compose"
            fake_compose.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'Unseal Key 1: disposable-share' 'Initial Root Token: disposable-token'\n"
            )
            fake_compose.chmod(0o755)
            output = Path(folder) / "operator-output.txt"
            environment = os.environ.copy()
            environment["COMPOSE"] = str(fake_compose)
            environment["OPENBAO_INIT_OUTPUT"] = str(output)

            result = subprocess.run(
                ["bash", str(OPENBAO_SCRIPT), "init"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("disposable-share", result.stdout + result.stderr)
            self.assertNotIn("disposable-token", result.stdout + result.stderr)
            self.assertIn(str(output), result.stdout)
            self.assertEqual(output.read_text(), "Unseal Key 1: disposable-share\nInitial Root Token: disposable-token\n")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(folder_path.glob("operator-output.txt.tmp.*")), [])

            again = subprocess.run(
                ["bash", str(OPENBAO_SCRIPT), "init"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(again.returncode, 0)
            self.assertEqual(output.read_text(), "Unseal Key 1: disposable-share\nInitial Root Token: disposable-token\n")

            race_compose = Path(folder) / "compose-race"
            race_compose.write_text(
                "#!/bin/sh\n"
                "printf '%s' 'concurrent-writer' > \"$OPENBAO_INIT_OUTPUT\"\n"
                "printf '%s\\n' 'Unseal Key 1: disposable-share'\n"
            )
            race_compose.chmod(0o755)
            race_output = Path(folder) / "race-output.txt"
            race_environment = environment.copy()
            race_environment["COMPOSE"] = str(race_compose)
            race_environment["OPENBAO_INIT_OUTPUT"] = str(race_output)
            raced = subprocess.run(
                ["bash", str(OPENBAO_SCRIPT), "init"],
                cwd=ROOT,
                env=race_environment,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(raced.returncode, 0)
            self.assertEqual(race_output.read_text(), "concurrent-writer")
            race_recovery_files = list(folder_path.glob("race-output.txt.tmp.*"))
            self.assertEqual(len(race_recovery_files), 1)
            self.assertEqual(
                race_recovery_files[0].read_text(),
                "Unseal Key 1: disposable-share\n",
            )
            self.assertEqual(race_recovery_files[0].stat().st_mode & 0o777, 0o600)
            self.assertIn(str(race_recovery_files[0]), raced.stderr)

            publication_failure_compose = Path(folder) / "compose-publication-failure"
            publication_failure_compose.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'Unseal Key 1: disposable-share'\n"
            )
            publication_failure_compose.chmod(0o755)
            fake_bin = folder_path / "bin"
            fake_bin.mkdir()
            fake_ln = fake_bin / "ln"
            fake_ln.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'simulated hard-link publication failure' >&2\n"
                "exit 23\n"
            )
            fake_ln.chmod(0o755)
            publication_output = folder_path / "publication-output.txt"
            publication_environment = environment.copy()
            publication_environment["COMPOSE"] = str(publication_failure_compose)
            publication_environment["OPENBAO_INIT_OUTPUT"] = str(publication_output)
            publication_environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
            publication_failed = subprocess.run(
                ["bash", str(OPENBAO_SCRIPT), "init"],
                cwd=ROOT,
                env=publication_environment,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(publication_failed.returncode, 0)
            self.assertFalse(publication_output.exists())
            publication_recovery_files = list(
                folder_path.glob("publication-output.txt.tmp.*")
            )
            self.assertEqual(len(publication_recovery_files), 1)
            self.assertEqual(
                publication_recovery_files[0].read_text(),
                "Unseal Key 1: disposable-share\n",
            )
            self.assertEqual(
                publication_recovery_files[0].stat().st_mode & 0o777,
                0o600,
            )
            self.assertIn(str(publication_recovery_files[0]), publication_failed.stderr)

            failed_compose = Path(folder) / "compose-failed"
            failed_compose.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' 'partial unseal secret'\n"
                "printf '%s\\n' 'partial root token' >&2\n"
                "exit 17\n"
            )
            failed_compose.chmod(0o755)
            failed_output = folder_path / "failed-output.txt"
            failed_environment = environment.copy()
            failed_environment["COMPOSE"] = str(failed_compose)
            failed_environment["OPENBAO_INIT_OUTPUT"] = str(failed_output)
            command_failed = subprocess.run(
                ["bash", str(OPENBAO_SCRIPT), "init"],
                cwd=ROOT,
                env=failed_environment,
                text=True,
                capture_output=True,
            )
            self.assertEqual(command_failed.returncode, 17)
            self.assertNotIn(
                "partial unseal secret",
                command_failed.stdout + command_failed.stderr,
            )
            self.assertNotIn(
                "partial root token",
                command_failed.stdout + command_failed.stderr,
            )
            self.assertFalse(failed_output.exists())
            failed_recovery_files = list(folder_path.glob("failed-output.txt.tmp.*"))
            self.assertEqual(len(failed_recovery_files), 1)
            self.assertEqual(
                failed_recovery_files[0].read_text(),
                "partial unseal secret\npartial root token\n",
            )
            self.assertEqual(failed_recovery_files[0].stat().st_mode & 0o777, 0o600)
            self.assertIn(str(failed_recovery_files[0]), command_failed.stderr)

            inside_repository = environment.copy()
            inside_repository["OPENBAO_INIT_OUTPUT"] = str(ROOT / "tests" / "init-output.txt")
            rejected = subprocess.run(
                ["bash", str(OPENBAO_SCRIPT), "init"],
                cwd=ROOT,
                env=inside_repository,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("outside the repository", rejected.stderr)

            primary_root = Path(
                subprocess.run(
                    ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.strip()
            ).resolve().parent
            primary_output = primary_root / ".openbao-init-test.txt"
            primary_environment = environment.copy()
            primary_environment["OPENBAO_INIT_OUTPUT"] = str(primary_output)
            primary_result = subprocess.run(
                ["bash", str(OPENBAO_SCRIPT), "init"],
                cwd=ROOT,
                env=primary_environment,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(primary_result.returncode, 0)
            self.assertIn("outside the repository", primary_result.stderr)

            worktrees = [
                Path(line.removeprefix("worktree ")).resolve()
                for line in subprocess.run(
                    ["git", "-C", str(ROOT), "worktree", "list", "--porcelain"],
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.splitlines()
                if line.startswith("worktree ")
            ]
            other_worktree = next((path for path in worktrees if path != ROOT.resolve()), None)
            if other_worktree is not None:
                worktree_environment = environment.copy()
                worktree_environment["OPENBAO_INIT_OUTPUT"] = str(other_worktree / ".openbao-init-test.txt")
                worktree_result = subprocess.run(
                    ["bash", str(OPENBAO_SCRIPT), "init"],
                    cwd=ROOT,
                    env=worktree_environment,
                    text=True,
                    capture_output=True,
                )
                self.assertNotEqual(worktree_result.returncode, 0)
                self.assertIn("outside the repository", worktree_result.stderr)

            symlink_parent = Path(folder) / "repo-link"
            symlink_parent.symlink_to(primary_root, target_is_directory=True)
            symlink_environment = environment.copy()
            symlink_environment["OPENBAO_INIT_OUTPUT"] = str(symlink_parent / ".openbao-init-test.txt")
            symlink_result = subprocess.run(
                ["bash", str(OPENBAO_SCRIPT), "init"],
                cwd=ROOT,
                env=symlink_environment,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(symlink_result.returncode, 0)
            self.assertIn("outside the repository", symlink_result.stderr)

    def test_manual_helpers_do_not_put_secret_material_in_command_arguments(self):
        script = OPENBAO_SCRIPT.read_text()

        self.assertIn("operator init", script)
        self.assertIn("OPENBAO_INIT_OUTPUT", script)
        self.assertIn("operator unseal", script)
        self.assertIn("raft snapshot save", script)
        self.assertIn("--user 0:0", script)
        self.assertRegex(script, r"read -r[s]? .*token")
        self.assertNotRegex(script, r"operator unseal.*\$[A-Za-z_{][A-Za-z0-9_}]*")
        self.assertNotRegex(script, r"-e\s+BAO_TOKEN=")


if __name__ == "__main__":
    unittest.main()
