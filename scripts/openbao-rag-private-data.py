#!/usr/bin/env python3
"""Provision the RAG-only OpenBao role, non-exportable keys and credential volume."""
from __future__ import annotations
import argparse
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("openbao_app_role", ROOT / "scripts/openbao-app-role.py")
ROLE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ROLE
SPEC.loader.exec_module(ROLE)
ROLE_NAME = "rag-private-data"
KEYS = {"port-rag-private-data": "chacha20-poly1305", "port-rag-private-lookup": "hmac"}


def provision(client, volume=None, apply=False):
    observed = {}
    for name, kind in KEYS.items():
        try:
            key = client.request("GET", f"/transit/keys/{name}").get("data")
        except ROLE.OpenBaoHTTPError as error:
            if error.status != 404:
                raise
            key = None
        if key is not None and (not isinstance(key, dict) or key.get("type") != kind or key.get("exportable") is not False or key.get("allow_plaintext_backup") is not False or (kind == "hmac" and key.get("min_encryption_version", 1) > 1)):
            raise ROLE.ProvisionError("RAG key configuration is unsafe or incompatible")
        observed[name] = key
    mount = client.get_approle_mount()
    ROLE.validate_approle_mount(mount)
    role = client.get_role(ROLE_NAME) if mount else None
    expected = ROLE.desired_role_payload(ROLE_NAME)
    if role:
        ROLE.validate_existing_role(role, policy_name=ROLE_NAME, role_name=ROLE_NAME)
    if not apply:
        return {"mode": "dry-run", "role": "present" if role else "create", "keys": {name: "present" if key else "create" for name, key in observed.items()}}
    if volume is None:
        raise ROLE.ProvisionError("RAG credential volume is required")
    volume.ensure_empty()  # Never overwrite a live role credential volume.
    for name, kind in KEYS.items():
        if observed[name] is None:
            payload = {"type": kind, "exportable": False, "allow_plaintext_backup": False}
            if kind == "hmac":
                payload["key_size"] = 32
            client.request("POST", f"/transit/keys/{name}", payload)
    client.write_policy(ROLE_NAME, (ROOT / "openbao/policies/rag-private-data.hcl").read_text())
    if not mount:
        client.enable_approle()
    if not role:
        client.write_role(ROLE_NAME, expected)
    role_id = client.read_role_id(ROLE_NAME)
    secret = client.issue_secret_id(ROLE_NAME)
    try:
        volume.write(role_id, secret["secret_id"])
    except Exception:
        client.revoke_secret_id(ROLE_NAME, secret["secret_id_accessor"])
        raise ROLE.ProvisionError("RAG credential write failed; new SecretID revoked") from None
    return {"mode": "apply", "role": ROLE_NAME}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--addr", required=True)
    parser.add_argument("--ca-cert", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--volume", default="infra_openbao_rag_credentials")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    token = ROLE.load_operator_token(args.token_file)
    endpoint = ROLE.parse_endpoint(args.addr, args.ca_cert)
    client = ROLE.OpenBaoClient(endpoint, args.ca_cert, token)
    volume = ROLE.CredentialVolume(args.volume, ROLE.DEFAULT_IMAGE, credential_prefix="rag", owner_uid=1000)
    import json
    print(json.dumps(provision(client, volume, args.apply)))


if __name__ == "__main__":
    try:
        main()
    except ROLE.ProvisionError:
        print("RAG OpenBao provisioning failed; credentials and response bodies are not logged", file=sys.stderr)
        sys.exit(1)
