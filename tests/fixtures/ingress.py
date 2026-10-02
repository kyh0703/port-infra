"""Isolated backends and network clients for the real Caddy ingress regression."""

import base64
import hashlib
import http.client
import json
import os
import socket
import sys
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class Backend(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        # Fixtures must not log the credential-preservation test inputs.
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        path = urlsplit(self.path).path
        if path.endswith("/disconnect"):
            self.close_connection = True
            return
        if path.endswith("/redirect"):
            self.send_response(307)
            self.send_header("Location", "https://" + os.environ["INGRESS_PUBLIC_AUTHORITY"] + "/next?mode=redirect")
            self.send_header("Set-Cookie", "first=fixture; HttpOnly; Secure; SameSite=Lax")
            self.send_header("Set-Cookie", "second=fixture; HttpOnly; Secure; SameSite=Lax")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.endswith("/events"):
            self.server.release_stream.clear()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"data: first\n\n")
            self.wfile.flush()
            if self.server.release_stream.wait(timeout=15):
                self.wfile.write(b"data: second\n\n")
                self.wfile.flush()
            self.close_connection = True
            return
        if path.endswith("/release-stream"):
            self.server.release_stream.set()
        if self.headers.get("Upgrade", "").lower() == "websocket":
            accept = base64.b64encode(hashlib.sha1(
                (self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()).decode()
            self.send_response(101)
            self.send_header("Connection", "Upgrade")
            self.send_header("Upgrade", "websocket")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            frame = read_exact(self.rfile, 2)
            length = frame[1] & 0x7f
            if frame[0] != 0x81 or not frame[1] & 0x80 or length > 125:
                raise ValueError("Fixture expects a short, masked WebSocket text frame")
            mask = read_exact(self.rfile, 4)
            masked = read_exact(self.rfile, length)
            body = bytes(value ^ mask[index % 4] for index, value in enumerate(masked))
            self.wfile.write(bytes([0x81, len(body)]) + body)
            self.wfile.flush()
            self.close_connection = True
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        response = json.dumps({
            "backend": self.server.backend,
            "method": self.command,
            "target": self.path,
            "body": base64.b64encode(body).decode(),
            "peer": self.client_address[0],
            "headers": {key.lower(): self.headers.get_all(key) for key in self.headers.keys()},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def read_exact(stream, size):
    result = bytearray()
    while len(result) < size:
        part = stream.read(size - len(result))
        if not part:
            raise EOFError("Unexpected end of fixture connection")
        result.extend(part)
    return bytes(result)


def connection(request):
    source = (request["source_ip"], 0) if request.get("source_ip") else None
    return http.client.HTTPConnection(request["proxy_ip"], 8088, timeout=5, source_address=source)


def request_http(request):
    body = base64.b64decode(request.get("body", ""))
    with closing(connection(request)) as client:
        client.putrequest(request.get("method", "GET"), request["path"], skip_host=True)
        client.putheader("Host", request.get("host", "attacker.example"))
        for name, values in request.get("headers", {}).items():
            for value in values if isinstance(values, list) else [values]:
                client.putheader(name, value)
        client.putheader("Content-Length", str(len(body)))
        client.endheaders(body)
        response = client.getresponse()
        headers = response.getheaders()
        if request.get("mode") == "stream":
            # The backend cannot finish until the first event reaches this client.
            first = response.readline() + response.readline()
            with closing(connection(request)) as release:
                release.request("GET", request["release_path"])
                released = release.getresponse()
                if released.status != 200:
                    raise RuntimeError("Could not release the fixture stream")
                released.read()
            remaining = response.read()
            return {"status": response.status, "first": first.decode(), "remaining": remaining.decode()}
        return {"status": response.status, "headers": headers, "body": response.read().decode()}


def request_websocket(request):
    source = (request["source_ip"], 0) if request.get("source_ip") else None
    key = base64.b64encode(b"fixture-ws-key-16").decode()
    with socket.create_connection((request["proxy_ip"], 8088), timeout=5, source_address=source) as client:
        client.sendall((
            "GET /ws HTTP/1.1\r\nHost: attacker.example\r\nConnection: Upgrade\r\n"
            "Upgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " + key + "\r\n\r\n"
        ).encode())
        with client.makefile("rb") as stream:
            status = int(stream.readline().split()[1])
            headers = {}
            while True:
                line = stream.readline()
                if line == b"\r\n":
                    break
                name, value = line.decode().split(":", 1)
                headers[name.lower()] = value.strip()
            if status != 101:
                return {"status": status, "headers": headers}
            body = b"websocket-through-ingress"
            mask = b"mask"
            masked = bytes(value ^ mask[index % 4] for index, value in enumerate(body))
            client.sendall(bytes([0x81, 0x80 | len(body)]) + mask + masked)
            frame = read_exact(stream, 2)
            echoed = read_exact(stream, frame[1] & 0x7f)
            return {"status": status, "headers": headers, "opcode": frame[0], "body": echoed.decode()}


def serve():
    for name, port in [("api", 8000), ("web", 3000)]:
        server = ThreadingHTTPServer(("0.0.0.0", port), Backend)
        server.backend = name
        server.release_stream = threading.Event()
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Event().wait()


if __name__ == "__main__":
    if sys.argv[1] == "server":
        serve()
    elif sys.argv[1] == "idle":
        threading.Event().wait()
    elif sys.argv[1] == "request":
        payload = json.load(sys.stdin)
        result = request_websocket(payload) if payload.get("mode") == "websocket" else request_http(payload)
        print(json.dumps(result))
    else:
        raise ValueError("Unknown fixture mode")
