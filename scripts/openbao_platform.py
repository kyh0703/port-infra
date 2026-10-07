#!/usr/bin/env python3
"""Provider HTTP health/signal owner for the original TLS/static-sealed Bao.

No init, unseal, migration, snapshot restore, key generation or data deletion is
implemented. PORT health is independent of Bao's TLS listener and validates the
explicit original cluster ID. Actual envelope bootstrap remains the API's gate.
"""
from __future__ import annotations

import http.client
import json
import os
import signal
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class PlatformError(RuntimeError):
    """A non-secret platform failure."""


def original_cluster_ready(health: dict, expected_cluster_id: str) -> bool:
    return bool(expected_cluster_id) and health.get('initialized') is True and health.get('sealed') is False and health.get('cluster_id') == expected_cluster_id


def probe_original_cluster(ca_file: Path, expected_cluster_id: str) -> bool:
    connection = None
    try:
        context = ssl.create_default_context(cafile=str(ca_file))
        connection = http.client.HTTPSConnection('127.0.0.1', 8200, context=context, timeout=2)
        connection.request('GET', '/v1/sys/health')
        response = connection.getresponse()
        data = response.read(65537)
        if response.status != 200 or len(data) > 65536:
            return False
        health = json.loads(data)
        return isinstance(health, dict) and original_cluster_ready(health, expected_cluster_id)
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        if connection is not None:
            connection.close()


def shutdown_owned_child(child: subprocess.Popen, grace_seconds: float) -> int:
    if child.poll() is not None:
        return child.returncode
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        return child.wait()
    try:
        return child.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return child.wait()


def main() -> int:
    expected_cluster_id = os.environ.get('OPENBAO_EXPECTED_CLUSTER_ID', '')
    if not expected_cluster_id:
        raise PlatformError('OPENBAO_EXPECTED_CLUSTER_ID must bind the original initialized raft cluster; no empty substitute cluster')
    try:
        port = int(os.environ.get('PORT', '8000'))
        grace = int(os.environ.get('OPENBAO_SHUTDOWN_MS', '30000')) / 1000
    except ValueError as error:
        raise PlatformError('invalid health port or shutdown grace') from error
    if not 1 <= port <= 65535 or port in (8200, 8201) or not 1 <= grace <= 35:
        raise PlatformError('health PORT must differ from TLS/cluster ports; shutdown grace must be 1..35 seconds inside provider45s budget')
    stopping = threading.Event()
    child = None
    ca_file = Path('/bao/tls-runtime/ca.crt')

    class HealthHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            live = child is not None and child.poll() is None
            if self.path == '/livez':
                ready = live
            elif self.path in ('/startupz', '/readyz'):
                ready = live and not stopping.is_set() and probe_original_cluster(ca_file, expected_cluster_id)
            else:
                self.send_response(404)
                self.end_headers()
                return
            scope = 'process' if self.path == '/livez' else 'original-cluster-tls'
            payload = json.dumps({
                'status': ('live' if scope == 'process' else 'ready') if ready else 'unavailable',
                'processLive': live, 'probeScope': scope,
                'originalClusterReadiness': 'not-probed' if scope == 'process' else ('observed' if ready else 'unavailable'),
            }).encode()
            self.send_response(200 if ready else 503)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('0.0.0.0', port), HealthHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    try:
        child_env = os.environ.copy()
        child = subprocess.Popen(['sh', '/usr/local/bin/platform-entrypoint', 'sh', '/usr/local/bin/platform-server'], env=child_env, start_new_session=True)
        # Static secret is consumed only by the Bao key loader, not health helpers.
        child_env.pop('OPENBAO_STATIC_SEAL_KEY_B64', None)
        os.environ.pop('OPENBAO_STATIC_SEAL_KEY_B64', None)
        for event in (signal.SIGTERM, signal.SIGINT):
            signal.signal(event, lambda *_: stopping.set())
        thread.start()
        while child.poll() is None and not stopping.wait(0.1):
            pass
        stopping.set()
        return shutdown_owned_child(child, grace)
    except OSError as error:
        raise PlatformError('original Bao process could not start; no automatic initialization/fallback') from error
    finally:
        stopping.set()
        if thread.is_alive():
            server.shutdown()
            thread.join(timeout=3)
        server.server_close()
        if child is not None and child.poll() is None:
            shutdown_owned_child(child, grace)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (PlatformError, OSError) as error:
        message = str(error) if isinstance(error, PlatformError) else 'platform health listener unavailable'
        print(message, file=sys.stderr)
        raise SystemExit(2)
