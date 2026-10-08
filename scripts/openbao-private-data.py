#!/usr/bin/env python3
"""Provision non-exportable private-data keys and the existing API ACL. Default: dry run."""
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
KEYS = {"port-private-data": "chacha20-poly1305", "port-private-lookup": "hmac"}


def read_key(client, name):
    try:
        response = client.request("GET", f"/transit/keys/{name}")
    except ROLE.OpenBaoHTTPError as error:
        if error.status == 404:
            return None
        raise
    key = response.get("data")
    if not isinstance(key, dict) or key.get("type") != KEYS[name] or key.get("exportable") is not False or key.get("allow_plaintext_backup") is not False:
        raise ROLE.ProvisionError("Private-data key configuration is unsafe or incompatible")
    if name == "port-private-lookup" and key.get("min_encryption_version", 1) > 1:
        raise ROLE.ProvisionError("Lookup key version 1 must remain available")
    return key


def provision(client, apply=False):
    observed = {name: read_key(client, name) for name in KEYS}
    # Existing role/credentials are required; this command never rotates or prints them.
    role = client.get_role(ROLE.ROLE_NAME)
    if not role or ROLE.POLICY_NAME not in role.get("token_policies", []):
        raise ROLE.ProvisionError("Existing API AppRole is required")
    if apply:
        for name, kind in KEYS.items():
            if observed[name] is None:
                payload = {"type": kind, "exportable": False, "allow_plaintext_backup": False}
                if kind == "hmac":
                    payload["key_size"] = 32
                client.request("POST", f"/transit/keys/{name}", payload)
                read_key(client, name)
        client.write_policy(ROLE.POLICY_NAME, (ROOT / "openbao/policies/api-pii-envelope.hcl").read_text())
    return {"mode": "apply" if apply else "dry-run", "keys": {name: "present" if key else "create" for name, key in observed.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--addr", required=True)
    parser.add_argument("--ca-cert", type=Path, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    token = ROLE.load_operator_token(args.token_file)
    endpoint = ROLE.parse_endpoint(args.addr, args.ca_cert)
    result = provision(ROLE.OpenBaoClient(endpoint, args.ca_cert, token), args.apply)
    import json
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except ROLE.ProvisionError:
        print("OpenBao private-data provisioning failed; credentials and server responses are not logged", file=sys.stderr)
        sys.exit(1)
