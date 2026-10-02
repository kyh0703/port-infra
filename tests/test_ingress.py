"""Exercise the production Caddyfile without touching live Compose or Serve."""

import base64
import hashlib
import json
import subprocess
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/ingress.py"
AUTHORITY = "ingress.example.test:8443"
CADDY_IMAGE = "caddy:2.10.2-alpine"
PYTHON_IMAGE = "python:3.12-alpine"


class IngressTests(unittest.TestCase):
    @staticmethod
    def docker(*arguments, input=None):
        result = subprocess.run(
            ["docker", *arguments], input=input, text=True,
            capture_output=True, check=True, timeout=120,
        )
        # Docker carries Caddy's structured logs on stderr as well as stdout.
        if arguments[0] == "logs":
            return result.stdout + result.stderr
        return result.stdout.strip()

    @classmethod
    def start_container(cls, name, *arguments):
        cls.docker("create", "--name", name, *arguments)
        cls.addClassCleanup(cls.docker, "rm", "-f", name)
        cls.docker("start", name)

    @classmethod
    def container_ip(cls, name):
        networks = json.loads(cls.docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", name))
        return networks[cls.network]["IPAddress"]

    @classmethod
    def setUpClass(cls):
        identity = "ingress-test-" + uuid.uuid4().hex[:12]
        cls.network = identity + "-network"
        cls.backends = identity + "-backends"
        cls.proxy = identity + "-proxy"
        cls.edge = identity + "-edge"
        # Docker chooses a free subnet, independent of the deployed fixed subnet.
        cls.docker("network", "create", cls.network)
        cls.addClassCleanup(cls.docker, "network", "rm", cls.network)
        network = json.loads(cls.docker("network", "inspect", cls.network))[0]
        cls.gateway = network["IPAM"]["Config"][0]["Gateway"]
        fixture_mount = "type=bind,source=" + str(FIXTURE) + ",target=/fixture.py,readonly"
        cls.start_container(
            cls.backends, "--network", cls.network, "--network-alias", "api", "--network-alias", "web",
            "--mount", fixture_mount, "--env", "INGRESS_PUBLIC_AUTHORITY=" + AUTHORITY,
            PYTHON_IMAGE, "python", "/fixture.py", "server",
        )
        cls.start_container(
            cls.proxy, "--network", cls.network, "--tmpfs", "/data", "--tmpfs", "/config",
            "--mount", "type=bind,source=" + str(ROOT / "ingress/Caddyfile") + ",target=/etc/caddy/Caddyfile,readonly",
            "--env", "INGRESS_PUBLIC_AUTHORITY=" + AUTHORITY,
            "--env", "INGRESS_TRUSTED_EDGE_CIDR=" + cls.gateway + "/32",
            CADDY_IMAGE, "caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile",
        )
        # Linux host networking binds the exact gateway, not a macOS/Colima peer
        # assumption. The live edge must still be checked separately before Serve.
        cls.start_container(
            cls.edge, "--network", "host", "--mount", fixture_mount,
            PYTHON_IMAGE, "python", "/fixture.py", "idle",
        )
        cls.proxy_ip = cls.container_ip(cls.proxy)
        cls.backend_ip = cls.container_ip(cls.backends)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                response = cls.request("untrusted", "/api/v1/health")
                if response["status"] == 200:
                    return
            except (subprocess.CalledProcessError, json.JSONDecodeError):
                pass
            time.sleep(0.2)
        raise RuntimeError("Ingress fixture did not become ready:\n" + cls.docker("logs", cls.proxy))

    @classmethod
    def request(cls, peer, path, **options):
        payload = {"proxy_ip": cls.proxy_ip, "path": path, **options}
        if peer == "trusted":
            payload["source_ip"] = cls.gateway
            client = cls.edge
        elif peer == "untrusted":
            client = cls.backends
        else:
            raise ValueError("Unknown fixture peer")
        result = cls.docker("exec", "-i", client, "python", "/fixture.py", "request", input=json.dumps(payload))
        return json.loads(result)

    def test_api_namespace_is_not_stripped_and_neighbor_paths_stay_on_web(self):
        cases = [
            ("/api/v1", "api"),
            ("/api/v1/records?value=a%2Fb&value=c", "api"),
            ("/", "web"),
            ("/api/v10/records?value=a%2Fb", "web"),
            ("/api/v1extra", "web"),
        ]
        for path, backend in cases:
            with self.subTest(path=path):
                response = self.request("trusted", path)
                self.assertEqual(response["status"], 200)
                actual = json.loads(response["body"])
                self.assertEqual(actual["backend"], backend)
                self.assertEqual(actual["target"], path)
                self.assertEqual(actual["peer"], self.proxy_ip)

    def test_api_mutation_keeps_body_cookie_and_authorization(self):
        body = b'\x00{"message":"ingress body"}\xff'
        response = self.request(
            "trusted", "/api/v1/records?mode=write", method="POST",
            body=base64.b64encode(body).decode(),
            headers={"Cookie": "session=fixture-session", "Authorization": "Bearer fixture-access-token"},
        )
        self.assertEqual(response["status"], 200)
        actual = json.loads(response["body"])
        self.assertEqual(actual["backend"], "api")
        self.assertEqual(actual["method"], "POST")
        self.assertEqual(base64.b64decode(actual["body"]), body)
        self.assertEqual(actual["headers"]["cookie"], ["session=fixture-session"])
        self.assertEqual(actual["headers"]["authorization"], ["Bearer fixture-access-token"])

    def assert_canonical_headers(self, headers, client_ip):
        self.assertEqual(headers["host"], [AUTHORITY])
        self.assertEqual(headers["x-forwarded-host"], [AUTHORITY])
        self.assertEqual(headers["x-forwarded-proto"], ["https"])
        self.assertEqual(headers["x-forwarded-for"], [client_ip])
        self.assertNotIn("forwarded", headers)
        self.assertNotIn("x-real-ip", headers)

    def test_untrusted_private_peer_cannot_supply_forwarded_identity(self):
        response = self.request("untrusted", "/account", headers={
            "X-Forwarded-For": "203.0.113.9, 100.70.80.90",
            "X-Forwarded-Host": "attacker.example:80",
            "X-Forwarded-Proto": "http",
            "Forwarded": "for=203.0.113.9;host=attacker.example;proto=http",
            "X-Real-IP": "203.0.113.9",
        })
        self.assertEqual(response["status"], 200)
        actual = json.loads(response["body"])
        self.assertEqual(actual["backend"], "web")
        self.assert_canonical_headers(actual["headers"], self.backend_ip)

    def test_trusted_edge_uses_rightmost_untrusted_address_not_spoofed_prefix(self):
        for client_ip in ["100.70.80.90", "2001:db8::90", self.backend_ip]:
            with self.subTest(client_ip=client_ip):
                response = self.request("trusted", "/api/v1/records", headers={
                    "X-Forwarded-For": "203.0.113.9, " + client_ip + ", " + self.gateway,
                    "X-Forwarded-Host": "attacker.example",
                    "X-Forwarded-Proto": "http",
                    "Forwarded": "for=203.0.113.9;host=attacker.example;proto=http",
                    "X-Real-IP": "203.0.113.9",
                })
                self.assertEqual(response["status"], 200)
                self.assert_canonical_headers(json.loads(response["body"])["headers"], client_ip)

    def test_alternate_client_headers_are_not_a_fallback(self):
        response = self.request("trusted", "/api/v1/records", headers={
            "Forwarded": "for=203.0.113.9", "X-Real-IP": "203.0.113.9",
        })
        self.assertEqual(response["status"], 200)
        self.assert_canonical_headers(json.loads(response["body"])["headers"], self.gateway)

    def test_redirect_and_multiple_set_cookie_headers_reach_client_unchanged(self):
        response = self.request("trusted", "/api/v1/redirect")
        self.assertEqual(response["status"], 307)
        headers = [(name.lower(), value) for name, value in response["headers"]]
        self.assertIn(("location", "https://" + AUTHORITY + "/next?mode=redirect"), headers)
        self.assertEqual([value for name, value in headers if name == "set-cookie"], [
            "first=fixture; HttpOnly; Secure; SameSite=Lax",
            "second=fixture; HttpOnly; Secure; SameSite=Lax",
        ])

    def test_sse_first_event_arrives_before_backend_can_finish(self):
        response = self.request(
            "trusted", "/api/v1/events", mode="stream", release_path="/api/v1/release-stream",
        )
        self.assertEqual(response, {"status": 200, "first": "data: first\n\n", "remaining": "data: second\n\n"})

    def test_websocket_upgrade_and_bidirectional_payload(self):
        response = self.request("trusted", "/ws", mode="websocket")
        self.assertEqual(response["status"], 101)
        key = base64.b64encode(b"fixture-ws-key-16").decode()
        expected_accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()).decode()
        self.assertEqual(response["headers"]["sec-websocket-accept"], expected_accept)
        self.assertEqual(response["opcode"], 0x81)
        self.assertEqual(response["body"], "websocket-through-ingress")

    def test_upstream_failure_does_not_log_credentials(self):
        token = "fixture-private-bearer-do-not-log"
        cookie = "fixture-private-session-do-not-log"
        response = self.request("trusted", "/api/v1/disconnect", headers={
            "Authorization": "Bearer " + token, "Cookie": "session=" + cookie,
        })
        self.assertEqual(response["status"], 502)
        logs = self.docker("logs", self.proxy)
        self.assertNotIn(token, logs)
        self.assertNotIn(cookie, logs)


if __name__ == "__main__":
    unittest.main()
