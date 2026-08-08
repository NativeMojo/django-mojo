"""
Feed the generated configuration to a REAL nginx.

Opt-in (`--extra extended`, or `--full`), because it needs nginx on the host.

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
from unittest import mock

from testit import TestitSkip
from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools,
    cleanup, declare_reserved_names, make_certificate, make_domain, make_group,
    make_upstream, make_vhost,
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
    declare_reserved_names()
    declare_pools()
    opts.group = make_group("edgenginx")
    opts.domain = make_domain(name="edge-nginx.example.com", group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.upstream = make_upstream(
        name="up-nginx-real", host="127.0.0.1", port=8000)


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
        with mock.patch("mojo.apps.edge.services.render.edge_root",
                        return_value=root):
            gen_dir = render.generation_dir(GENERATION)
            conf_d = os.path.join(gen_dir, "conf.d")
            os.makedirs(conf_d)

            # One vhost of every kind — the three builders, all in one config.
            vhosts = [
                make_vhost(opts.domain, opts.certificate, label="www",
                           kind="site", pool="nginxreal"),
                make_vhost(opts.domain, opts.certificate, label="app",
                           kind="site", spa=True, pool="nginxreal"),
                make_vhost(opts.domain, opts.certificate, label="api",
                           kind="api", upstream=opts.upstream,
                           pool="nginxreal"),
            ]

            cert_pem, key_pem = _self_signed(opts.domain.name)
            cert_dir = render.cert_dir(GENERATION, opts.certificate.pk)
            os.makedirs(cert_dir)
            with open(os.path.join(cert_dir, "fullchain.pem"), "w") as handle:
                handle.write(cert_pem)
            with open(os.path.join(cert_dir, "privkey.pem"), "w") as handle:
                handle.write(key_pem)

            for name, text in render.render_generation(vhosts, GENERATION).items():
                with open(os.path.join(conf_d, name), "w") as handle:
                    handle.write(text)
            for vhost in vhosts:
                os.makedirs(render.www_dir(GENERATION, vhost.pk), exist_ok=True)

            # The PRODUCTION harness, byte-for-byte what a node feeds nginx.
            # It is unprivileged-safe by construction (all scratch paths live
            # inside the generation), so there is no test-only wrapper here —
            # a wrapper would have meant this never validated the real thing.
            os.makedirs(os.path.join(gen_dir, "tmp"), exist_ok=True)
            harness_path = os.path.join(gen_dir, "nginx.conf")
            with open(harness_path, "w") as handle:
                handle.write(render.render_nginx_harness(GENERATION))

            result = subprocess.run(
                [binary, "-t", "-c", harness_path],
                capture_output=True, text=True, timeout=60)

        output = f"{result.stdout}{result.stderr}"
        assert result.returncode == 0, (
            f"nginx refused the generated configuration:\n{output}")
        # nginx exits 0 on a duplicate server_name, so the text has to be read.
        assert "conflicting server name" not in output, (
            f"nginx reported a server_name collision:\n{output}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
