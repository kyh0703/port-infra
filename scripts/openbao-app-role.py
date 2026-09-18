#!/usr/bin/env python3
"""Provision the API AppRole without exposing operator or app credentials."""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
from http.client import HTTPSConnection


ROOT = Path(__file__).resolve().parents[1]
ROLE_NAME = "api-pii-envelope"
POLICY_NAME = "api-pii-envelope"
DEFAULT_IMAGE = "ghcr.io/openbao/openbao:2.6.2"
DEFAULT_VOLUME = "infra_openbao_api_credentials"
VOLUME_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ProvisionError(RuntimeError):
    """A safe, non-secret provisioning failure."""


class OpenBaoHTTPError(ProvisionError):
    def __init__(self, method: str, path: str, status: int):
        super().__init__(f"OpenBao request failed: {method} {path} HTTP {status}")
        self.method = method
        self.path = path
        self.status = status


@dataclass(frozen=True)
class ProvisionConfig:
    addr: str
    ca_cert: Path
    token_file: Path
    policy_file: Path
    volume: str = DEFAULT_VOLUME
    image: str = DEFAULT_IMAGE


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    base_path: str


def _regular_file(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise ProvisionError(f"{label} is not readable: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ProvisionError(f"{label} must be a regular file: {path}")
    return info


def load_operator_token(path: Path) -> str:
    info = _regular_file(path, "operator token file")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ProvisionError("operator token file must be owned by the current user with mode 0600")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ProvisionError(f"operator token file is not readable: {path}") from error
    if not token or any(character.isspace() for character in token):
        raise ProvisionError("operator token file must contain one non-empty token")
    return token


def parse_endpoint(addr: str, ca_cert: Path) -> Endpoint:
    if not addr:
        raise ProvisionError("OPENBAO_ADDR or --addr is required")
    parsed = urlparse(addr)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ProvisionError("OpenBao address must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ProvisionError("OpenBao address must not contain credentials or query parameters")
    if parsed.path not in ("", "/"):
        raise ProvisionError("OpenBao address must not contain a URL path")
    _regular_file(ca_cert, "CA certificate")
    try:
        port = parsed.port or 443
    except ValueError as error:
        raise ProvisionError("OpenBao address has an invalid port") from error
    return Endpoint(parsed.hostname, port, "")


def _single_line_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or any(character.isspace() for character in value):
        raise ProvisionError(f"OpenBao returned an invalid {label}")
    return value


class OpenBaoClient:
    def __init__(self, endpoint: Endpoint, ca_cert: Path, token: str):
        self.endpoint = endpoint
        self.token = token
        try:
            self.context = ssl.create_default_context(cafile=str(ca_cert))
        except (OSError, ssl.SSLError) as error:
            raise ProvisionError("CA certificate could not be loaded") from error

    def request(self, method: str, path: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "X-Vault-Token": self.token,
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = HTTPSConnection(
            self.endpoint.host,
            self.endpoint.port,
            context=self.context,
            timeout=10,
        )
        try:
            connection.request(method, f"/v1{path}", body=body, headers=headers)
            response = connection.getresponse()
            raw_body = response.read()
            status = response.status
        except (OSError, ssl.SSLError) as error:
            raise ProvisionError(f"OpenBao request failed: {method} {path}") from error
        finally:
            connection.close()
        if status < 200 or status >= 300:
            raise OpenBaoHTTPError(method, path, status)
        if not raw_body:
            return {}
        try:
            decoded = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProvisionError(f"OpenBao returned invalid JSON: {method} {path}") from error
        if not isinstance(decoded, dict):
            raise ProvisionError(f"OpenBao returned an invalid response: {method} {path}")
        return decoded

    def get_approle_mount(self) -> Optional[dict[str, Any]]:
        data = self.request("GET", "/sys/auth").get("data", {})
        if not isinstance(data, dict):
            raise ProvisionError("OpenBao auth mount response is invalid")
        mount = data.get("approle/") or data.get("approle")
        if mount is None:
            return None
        if not isinstance(mount, dict) or mount.get("type") != "approle":
            raise ProvisionError("auth/approle exists with a type other than approle")
        return mount

    def enable_approle(self) -> None:
        self.request(
            "POST",
            "/sys/auth/approle",
            {"type": "approle", "description": "Port API PII envelope access"},
        )

    def write_policy(self, name: str, policy: str) -> None:
        self.request("PUT", f"/sys/policies/acl/{name}", {"policy": policy})

    def get_role(self, name: str) -> Optional[dict[str, Any]]:
        try:
            return self.request("GET", f"/auth/approle/role/{name}").get("data", {})
        except OpenBaoHTTPError as error:
            if error.status == 404:
                return None
            raise

    def write_role(self, name: str, payload: dict[str, Any]) -> None:
        self.request("POST", f"/auth/approle/role/{name}", payload)

    def read_role_id(self, name: str) -> str:
        data = self.request("GET", f"/auth/approle/role/{name}/role-id").get("data", {})
        if not isinstance(data, dict):
            raise ProvisionError("OpenBao role-id response is invalid")
        return _single_line_identifier(data.get("role_id"), "role ID")

    def issue_secret_id(self, name: str) -> dict[str, str]:
        data = self.request("POST", f"/auth/approle/role/{name}/secret-id").get("data", {})
        if not isinstance(data, dict):
            raise ProvisionError("OpenBao SecretID response is invalid")
        return {
            "secret_id": _single_line_identifier(data.get("secret_id"), "SecretID"),
            "secret_id_accessor": _single_line_identifier(
                data.get("secret_id_accessor"), "SecretID accessor"
            ),
        }

    def revoke_secret_id(self, name: str, accessor: str) -> None:
        self.request(
            "POST",
            f"/auth/approle/role/{name}/secret-id-accessor/destroy",
            {"secret_id_accessor": accessor},
        )


def _run_docker(args: list[str], input_data: Optional[bytes] = None) -> None:
    result = subprocess.run(
        ["docker", *args],
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise ProvisionError(f"Docker command failed with exit {result.returncode}")


class CredentialVolume:
    def __init__(self, name: str, image: str):
        if not VOLUME_NAME_PATTERN.fullmatch(name):
            raise ProvisionError("credential volume name contains unsupported characters")
        self.name = name
        self.image = image

    def ensure_empty(self) -> None:
        _run_docker(["volume", "create", self.name])
        script = r"""
set -eu
test -d /run/openbao
entries=$(find /run/openbao -mindepth 1 -maxdepth 1 -print -quit)
test -z "$entries"
"""
        _run_docker(
            [
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--user",
                "0:0",
                "--mount",
                f"type=volume,source={self.name},destination=/run/openbao",
                self.image,
                "sh",
                "-ec",
                script,
            ]
        )

    def write(self, role_id: str, secret_id: str) -> None:
        script = r"""
set -eu
umask 077
test -d /run/openbao
tmpdir=$(mktemp -d /run/openbao/.provision.XXXXXXXX)
created_role=0
created_secret=0
cleanup() {
  if [ "$created_secret" -eq 1 ]; then rm -f /run/openbao/api-secret-id; fi
  if [ "$created_role" -eq 1 ]; then rm -f /run/openbao/api-role-id; fi
  rm -rf "$tmpdir"
}
trap cleanup EXIT
IFS= read -r role_id
IFS= read -r secret_id
test -n "$role_id"
test -n "$secret_id"
printf '%s\n' "$role_id" > "$tmpdir/api-role-id"
printf '%s\n' "$secret_id" > "$tmpdir/api-secret-id"
chown 1001:1001 "$tmpdir" "$tmpdir/api-role-id" "$tmpdir/api-secret-id"
chown 1001:1001 /run/openbao
chmod 700 /run/openbao "$tmpdir"
chmod 400 "$tmpdir/api-role-id" "$tmpdir/api-secret-id"
if ! ln "$tmpdir/api-role-id" /run/openbao/api-role-id; then exit 42; fi
created_role=1
if ! ln "$tmpdir/api-secret-id" /run/openbao/api-secret-id; then exit 43; fi
created_secret=1
rm -f "$tmpdir/api-role-id" "$tmpdir/api-secret-id"
rmdir "$tmpdir"
trap - EXIT
"""
        input_data = f"{role_id}\n{secret_id}\n".encode("utf-8")
        _run_docker(
            [
                "run",
                "--rm",
                "--pull",
                "never",
                "--interactive",
                "--network",
                "none",
                "--user",
                "0:0",
                "--mount",
                f"type=volume,source={self.name},destination=/run/openbao",
                self.image,
                "sh",
                "-ec",
                script,
            ],
            input_data=input_data,
        )


def _duration_seconds(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text.endswith("s"):
            text = text[:-1]
        if text.isdigit():
            return int(text)
    return None


def _policies(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item for item in value.split(",") if item]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return []


def desired_role_payload() -> dict[str, Any]:
    return {
        "token_policies": [POLICY_NAME],
        "token_ttl": "300s",
        "token_max_ttl": "300s",
        "token_period": "0s",
        "token_no_default_policy": True,
        "bind_secret_id": True,
        "secret_id_ttl": "0s",
        "secret_id_num_uses": 0,
    }


def validate_existing_role(role: dict[str, Any]) -> None:
    desired = desired_role_payload()
    role_policies = _policies(role.get("token_policies"))
    if role_policies != desired["token_policies"]:
        raise ProvisionError("existing api-pii-envelope role has an unexpected policy set")
    if _duration_seconds(role.get("token_ttl")) != 300:
        raise ProvisionError("existing api-pii-envelope role has an unexpected token TTL")
    if _duration_seconds(role.get("token_max_ttl")) != 300:
        raise ProvisionError("existing api-pii-envelope role has an unexpected token max TTL")
    if _duration_seconds(role.get("token_period")) != 0:
        raise ProvisionError("existing api-pii-envelope role allows periodic tokens")
    if role.get("token_no_default_policy") is not True:
        raise ProvisionError("existing api-pii-envelope role allows the default policy")
    if role.get("bind_secret_id") is not True:
        raise ProvisionError("existing api-pii-envelope role does not bind SecretID")
    if _duration_seconds(role.get("secret_id_ttl")) != 0:
        raise ProvisionError("existing api-pii-envelope role has a non-persistent SecretID TTL")
    if role.get("secret_id_num_uses") != 0:
        raise ProvisionError("existing api-pii-envelope role limits SecretID uses")


def read_policy(path: Path) -> str:
    _regular_file(path, "policy file")
    try:
        policy = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ProvisionError(f"policy file is not readable: {path}") from error
    if not policy.strip() or "\x00" in policy:
        raise ProvisionError("policy file is empty or invalid")
    return policy


def validate_approle_mount(mount: Optional[dict[str, Any]]) -> None:
    if mount is not None and mount.get("type") != "approle":
        raise ProvisionError("auth/approle exists with a type other than approle")


def provision(
    config: ProvisionConfig,
    *,
    client: Optional[OpenBaoClient] = None,
    volume: Optional[CredentialVolume] = None,
) -> None:
    policy = read_policy(config.policy_file)
    token = load_operator_token(config.token_file)
    if client is None:
        endpoint = parse_endpoint(config.addr, config.ca_cert)
        client = OpenBaoClient(endpoint, config.ca_cert, token)
    if volume is None:
        volume = CredentialVolume(config.volume, config.image)

    volume.ensure_empty()
    approle_mount = client.get_approle_mount()
    validate_approle_mount(approle_mount)
    role = client.get_role(ROLE_NAME)
    if role is not None:
        validate_existing_role(role)
    client.write_policy(POLICY_NAME, policy)
    if approle_mount is None:
        client.enable_approle()
        if client.get_approle_mount() is None:
            raise ProvisionError("OpenBao did not expose auth/approle after enabling it")

    if role is None:
        client.write_role(ROLE_NAME, desired_role_payload())

    role_id = client.read_role_id(ROLE_NAME)
    secret = client.issue_secret_id(ROLE_NAME)
    try:
        volume.write(role_id, secret["secret_id"])
    except Exception as error:
        try:
            client.revoke_secret_id(ROLE_NAME, secret["secret_id_accessor"])
        except Exception as revoke_error:
            raise ProvisionError("credential write failed and new SecretID revoke failed") from revoke_error
        raise ProvisionError("credential write failed; new SecretID was revoked") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--addr", default=os.environ.get("OPENBAO_ADDR"))
    parser.add_argument("--ca-cert", type=Path, default=os.environ.get("OPENBAO_CA_CERT_FILE"))
    parser.add_argument("--token-file", type=Path, default=os.environ.get("OPENBAO_OPERATOR_TOKEN_FILE"))
    parser.add_argument(
        "--policy-file",
        type=Path,
        default=ROOT / "openbao" / "policies" / "api-pii-envelope.hcl",
    )
    parser.add_argument("--volume", default=os.environ.get("OPENBAO_CREDENTIALS_VOLUME", DEFAULT_VOLUME))
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.addr or not args.ca_cert or not args.token_file:
        print("OPENBAO_ADDR, OPENBAO_CA_CERT_FILE, and OPENBAO_OPERATOR_TOKEN_FILE are required", file=sys.stderr)
        return 2
    try:
        config = ProvisionConfig(
            addr=args.addr,
            ca_cert=Path(args.ca_cert),
            token_file=Path(args.token_file),
            policy_file=Path(args.policy_file),
            volume=args.volume,
            image=args.image,
        )
        provision(config)
    except ProvisionError as error:
        print(f"OpenBao AppRole provisioning failed: {error}", file=sys.stderr)
        return 1
    print("OpenBao AppRole provisioning completed; credential volume updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
