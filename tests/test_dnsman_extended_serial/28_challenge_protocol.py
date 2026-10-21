"""Actual outgoing mutation transport and additive protocol contracts."""
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from testit import helpers as th


@contextmanager
def unavailable_hub():
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append((self.path, json.loads(body)))
            payload = b'{"status":false,"code":503,"error":"unavailable"}'
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@th.django_unit_test("publish and withdraw each perform exactly one actual HTTP attempt")
def test_mutation_transport_never_retries(opts):
    from mojo.apps.dnsman.services import acme_hub_client as client
    with unavailable_hub() as (origin, calls):
        config = client._Config(origin, "test-owned-local-credential", 1, 1, 2)
        for operation in ("publish", "withdraw"):
            before = len(calls)
            with th.assert_raises(client.AcmeHubClientError):
                client._wire(operation, {"client_ref": "test", "challenge_ref": "test"}, config, requests.post)
            assert len(calls) - before == 1, f"{operation} must never hide multiple actual HTTP sends behind one durable intent"


@th.django_unit_test("read-only capability transport retains bounded retries")
def test_status_transport_can_retry_reads(opts):
    from mojo.apps.dnsman.services import acme_hub_client as client
    with unavailable_hub() as (origin, calls):
        config = client._Config(origin, "test-owned-local-credential", 1, 1, 2)
        with th.assert_raises(client.AcmeHubClientError):
            client._wire("capabilities", {}, config, requests.post)
        assert len(calls) == 3, "Read-only capability/status retries must remain supported"
