"""
Feed the generated configuration to a REAL nginx.

Opt-in (`--extra extended`, or `--all`), because it needs nginx on the host.

**Skips when nginx is absent.** django-mojo's suite runs inside every project
that uses the framework, so a test that fails on a missing binary turns those
projects red on the next release. nginx is not a dependency of this package and
is not installed on a typical laptop.

Install nginx to run it:  `brew install nginx`  /  `apt-get install nginx`

The certificate is a real, self-signed one generated here, because nginx opens
and parses `ssl_certificate` during `-t` — a placeholder file fails for the
wrong reason.
"""

import os
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from unittest import mock

from testit import TestitSkip
from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools,
    cleanup, ensure_blocklist_seed, make_certificate,
    make_domain, make_group, make_route, make_upstream, make_vhost,
)


GENERATION = "b" * 64


def _nginx_binary():
    return shutil.which("nginx")


def _self_signed(common_name):
    """A real certificate and key, in memory. nginx parses both during -t."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(common_name)]),
            critical=False)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM).decode(),
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()).decode(),
    )


@th.django_unit_setup()
def setup_nginx_real(opts):
    cleanup()
    # Seed the blocklist so the real nginx parses the FULL rendered maps —
    # all 260+ imported patterns, not empty scaffolding.
    ensure_blocklist_seed()
    declare_pools()
    opts.group = make_group("edgenginx")
    opts.domain = make_domain(name="edge-nginx.example.com", group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.upstream = make_upstream(
        name="up-nginx-real", host="127.0.0.1", port=8000)
    opts.unix_upstream = make_upstream(
        name="up-nginx-unix", kind="unix", socket_path="/run/mojo/nginx.sock")


@th.requires_extra("extended")
@th.django_unit_test("the generated configuration parses under a real nginx -t")
def test_real_nginx_accepts_every_kind(opts):
    from mojo.apps.edge.services import render

    binary = _nginx_binary()
    if not binary:
        raise TestitSkip(
            "nginx is not installed — skipping the real nginx -t check "
            "(brew install nginx / apt-get install nginx to run it)")

    root = tempfile.mkdtemp(prefix="edge-nginx-")
    try:
        # The default mime.types path is the deployment's (/etc/nginx/...),
        # which this host may not have (Homebrew keeps it elsewhere) — write
        # a real one into the generation so the include mechanism is
        # genuinely exercised. default_server=True so the flag-gated
        # catch-alls (ssl_reject_handshake) parse under a real nginx too.
        mime_path = os.path.join(root, "mime.types")
        with open(mime_path, "w") as handle:
            handle.write("types {\n"
                         "    text/html html;\n"
                         "    text/css css;\n"
                         "    application/javascript js;\n"
                         "}\n")
        with mock.patch("mojo.apps.edge.services.render.edge_root",
                        return_value=root), \
                mock.patch("mojo.apps.edge.services.render.mime_types_path",
                           return_value=mime_path), \
                mock.patch("mojo.apps.edge.services.render.mojosec_mode",
                           return_value="observe"), \
                mock.patch(
                    "mojo.apps.edge.services.render.default_server_enabled",
                    return_value=True):
            gen_dir = render.generation_dir(GENERATION)
            os.makedirs(gen_dir)

            # One vhost of every kind and every knob — the FULL include
            # graph (http base + upstreams + conf.d), exactly what a node
            # stages, fed to a real nginx.
            site_api = make_vhost(opts.domain, opts.certificate, label="mix",
                                  kind="site_api", pool="nginxreal")
            make_route(site_api, "/api", opts.upstream)
            make_route(site_api, "/ws", opts.unix_upstream)
            site_api.quiet_paths = ["/api/health"]
            site_api.save()
            vhosts = [
                make_vhost(opts.domain, opts.certificate, label="www",
                           kind="site", pool="nginxreal"),
                make_vhost(opts.domain, opts.certificate, label="app",
                           kind="site", spa=True, pool="nginxreal"),
                make_vhost(opts.domain, opts.certificate, label="api",
                           kind="api", upstream=opts.upstream,
                           quiet_paths=["/healthz"], serve_static=True,
                           pool="nginxreal"),
                make_vhost(opts.domain, opts.certificate, label="old",
                           kind="redirect",
                           redirect_to=f"www.{opts.domain.name}",
                           pool="nginxreal"),
                site_api,
            ]

            cert_pem, key_pem = _self_signed(opts.domain.name)
            cert_dir = render.cert_dir(GENERATION, opts.certificate.pk)
            os.makedirs(cert_dir)
            with open(os.path.join(cert_dir, "fullchain.pem"), "w") as handle:
                handle.write(cert_pem)
            with open(os.path.join(cert_dir, "privkey.pem"), "w") as handle:
                handle.write(key_pem)

            # The base's access_log lives under EDGE_LOG_DIR, and nginx -t
            # opens log files while validating — stage_generation creates
            # this on a node; this test writes files itself, so it does too.
            os.makedirs(render.log_dir(), exist_ok=True)

            # Both trees, exactly as stage_generation writes them: the real
            # files plus the staging/ listen-remapped copies the harness
            # includes. nginx -t binds every listen it parses, so the staged
            # copies are what make this check genuinely unprivileged-safe on
            # Linux (macOS allows low-port binds unprivileged, which is how
            # the original privileged-bind bug shipped — item 1623).
            for name, text in render.render_generation(vhosts, GENERATION).items():
                if name == "http.d/00_base.conf":
                    assert '"request_length":"$request_length"' in text, (
                        "the real nginx harness did not include enriched MojoSec observe logging")
                for target, body in ((name, text),
                                     (f"staging/{name}",
                                      render.render_staged_variant(
                                          text,
                                          temp_root=os.path.join(gen_dir, "tmp")
                                          if name == "http.d/00_base.conf" else None))):
                    path = os.path.join(gen_dir, target)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w") as handle:
                        handle.write(body)
            for vhost in vhosts:
                os.makedirs(render.www_dir(GENERATION, vhost.pk), exist_ok=True)

            # The PRODUCTION harness, byte-for-byte what a node feeds nginx
            # for the staged pre-filter (the real trees' privileged listen
            # lines are validated on-node by the post-swap root check). There
            # is no test-only wrapper here — a wrapper would have meant this
            # never validated the real thing.
            from mojo.deploy.nginx_runtime import TEMP_PATHS
            for _directive, leaf in TEMP_PATHS:
                os.makedirs(os.path.join(gen_dir, "tmp", leaf), exist_ok=True)
            harness_path = os.path.join(gen_dir, "nginx.conf")
            with open(harness_path, "w") as handle:
                handle.write(render.render_nginx_harness(GENERATION))

            # The installer's own staged argv (nginx -e stderr -t -c <path>),
            # not a hand-built one — this run should be the node's run.
            from mojo.apps.edge.services import installer
            result = subprocess.run(
                installer._nginx_staged_test_argv(harness_path),
                capture_output=True, text=True, timeout=60)

        output = f"{result.stdout}{result.stderr}"
        assert result.returncode == 0, (
            f"nginx refused the generated configuration:\n{output}")
        # nginx exits 0 on a duplicate server_name, so the text has to be read.
        assert "conflicting server name" not in output, (
            f"nginx reported a server_name collision:\n{output}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.requires_extra("extended")
@th.django_unit_test("the production spill contract accepts a body larger than memory")
def test_production_temp_contract_spills_request_body(opts):
    """Exercise the permanent HTTP-context paths, not Edge's staging paths.

    The production outage passed every staged ``nginx -t`` because only the
    generation-local harness declared writable temp paths.  This test forces
    nginx to spill a real proxied request body through the production mapping.
    """
    import http.server
    import socket

    from mojo.deploy import nginx_runtime

    binary = _nginx_binary()
    if not binary:
        raise TestitSkip(
            "nginx is not installed — skipping the forced request-body spill")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            received = self.rfile.read(size)
            body = str(len(received)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    def free_port():
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    root = tempfile.mkdtemp(prefix="edge-spill-")
    upstream_port = free_port()
    nginx_port = free_port()
    upstream = http.server.ThreadingHTTPServer(
        ("127.0.0.1", upstream_port), Handler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    process = None
    try:
        runtime_root = os.path.join(root, "runtime")
        for _directive, leaf in nginx_runtime.TEMP_PATHS:
            os.makedirs(os.path.join(runtime_root, leaf), mode=0o700,
                        exist_ok=True)
        config = os.path.join(root, "nginx.conf")
        error_log = os.path.join(root, "error.log")
        with open(config, "w") as handle:
            handle.write("\n".join([
                f"pid {root}/nginx.pid;",
                f"error_log {error_log} notice;",
                "events { worker_connections 32; }",
                "http {",
                "    client_body_buffer_size 1;",
                nginx_runtime.render_http_fragment(runtime_root, indent="    ").rstrip(),
                "    server {",
                f"        listen 127.0.0.1:{nginx_port};",
                "        location / {",
                f"            proxy_pass http://127.0.0.1:{upstream_port};",
                "        }",
                "    }",
                "}",
                "",
            ]))
        process = subprocess.Popen(
            [binary, "-c", config, "-p", root, "-g", "daemon off;"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _attempt in range(50):
            if process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{nginx_port}/", timeout=0.1):
                    pass
            except Exception:
                pass
            try:
                with socket.create_connection(
                        ("127.0.0.1", nginx_port), timeout=0.1):
                    break
            except OSError:
                import time
                time.sleep(0.05)

        payload = b"x" * (128 * 1024)
        request = urllib.request.Request(
            f"http://127.0.0.1:{nginx_port}/", data=payload, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            received = response.read().decode()
        assert received == str(len(payload)), (
            f"upstream received {received!r}, expected {len(payload)} bytes")
        errors = open(error_log).read() if os.path.exists(error_log) else ""
        assert "Permission denied" not in errors, (
            f"nginx could not spill the request body through the runtime tree:\n{errors}")
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)
        shutil.rmtree(root, ignore_errors=True)
