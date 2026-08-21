"""Split out of tests/test_edge/6_installer.py (maestro #2558).

The DNS-01-only posture test drives a FULL installer run under overridden
file-only settings, which is only reachable by patching the shared settings
singleton as seen through mojo.apps.edge.services.render — process-global,
so unsafe under the parallel default tier. The scaffolding below mirrors the
source module; the assertions are verbatim.
"""

import os
import shutil
import tempfile
from unittest import mock

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools,
    cleanup, make_certificate, make_domain, make_group, make_vhost,
)


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ok(argv):
    return FakeProc(0, stderr="nginx: configuration file test is successful\n")


class Recorder:
    """Records every argv the installer would have run."""

    def __init__(self, behaviour=None):
        self.calls = []
        self.behaviour = behaviour or _ok

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.behaviour(argv)

    @property
    def reloaded(self):
        return any("reload" in " ".join(argv) for argv in self.calls)

    @property
    def tested(self):
        return [argv for argv in self.calls if "-t" in argv]


@th.django_unit_setup()
def setup_installer(opts):
    cleanup()
    declare_pools()
    opts.group = make_group("edgeinstallx")


def _root(opts):
    """A throwaway EDGE_ROOT for one test."""
    return tempfile.mkdtemp(prefix="edge-test-")


def _pool(name):
    """A pool nobody else uses — desired state is fleet-wide within a pool."""
    return f"itestx{name}"


def _listen_ports(text):
    """Every `listen` directive's port in `text`, as ints.

    Parses the port TOKEN out of the address field — substring checks are a
    trap here (`61443` contains `443`, `61080` contains `80`, and the
    upstreams file legitimately carries `127.0.0.1:8000`).
    """
    ports = []
    for line in text.splitlines():
        tokens = line.strip().rstrip(";").split()
        if not tokens or tokens[0] != "listen":
            continue
        address = tokens[1]
        port = address.rsplit(":", 1)[-1] if ":" in address else address
        ports.append(int(port))
    return ports


def _with_root(root, material=True):
    """Patch EDGE_ROOT and, optionally, readable certificate material.

    The fixture certificates deliberately have NO secrets (KMS is absent in
    tests), which is the custody-unavailable branch — so a test that wants a
    successful install has to supply material.
    """
    patches = [
        mock.patch("mojo.apps.edge.services.render.edge_root", return_value=root),
    ]
    if material:
        patches.append(mock.patch(
            "mojo.apps.dnsman.models.certificate.Certificate.private_key_pem",
            new_callable=mock.PropertyMock,
            return_value="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n"))
    return patches


def _enter(patches):
    for patch in patches:
        patch.start()


def _exit(patches, root):
    for patch in reversed(patches):
        patch.stop()
    shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test("DNS-01-only install ignores the unused staged HTTP port")
def test_https_only_generation_staged(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.edge.services import installer, render
    from tests.test_edge._helpers import TEST_POOLS

    root = _root(opts)
    patches = _with_root(root)
    _enter(patches)
    pool = _pool("httpsonly")
    declare_pools([*TEST_POOLS, pool])
    try:
        domain = make_domain(group=opts.group)
        certificate = make_certificate(domain)
        vhost = make_vhost(
            domain, certificate, label="https-only", pool=pool)
        Certificate.objects.filter(pk=certificate.pk).update(
            cert_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")

        def static(name, default=None, kind=None):
            if name == "EDGE_HTTP_ENABLED":
                return False
            if name == "EDGE_STAGED_HTTP_PORT":
                return 80
            if name == "EDGE_ACME_WEBROOT":
                raise AssertionError("HTTPS-only install read the ACME webroot")
            return default

        with mock.patch.object(
                render.settings, "get_static", side_effect=static), \
                mock.patch.object(installer, "_run", Recorder()):
            result = installer.install(pool=pool)

        assert result.changed, "the HTTPS-only graph did not converge"
        current = os.path.realpath(render.current_link())
        assert os.path.exists(os.path.join(current, "http.d", "00_base.conf")), \
            "HTTPS-only convergence dropped the shared nginx http-context base"
        assert os.path.exists(os.path.join(current, "http.d", "10_upstreams.conf")), \
            "HTTPS-only convergence dropped the shared upstream fragment"

        real = open(os.path.join(
            current, "conf.d", f"{vhost.pk}.conf")).read()
        staged = open(os.path.join(
            current, "staging", "conf.d", f"{vhost.pk}.conf")).read()
        assert set(_listen_ports(real)) == {443}, \
            f"HTTPS-only real vhost has unexpected listeners: {_listen_ports(real)}"
        assert set(_listen_ports(staged)) == {render.staged_https_port()}, \
            f"HTTPS-only staged vhost has unexpected listeners: {_listen_ports(staged)}"
        assert "/.well-known/acme-challenge/" not in real + staged, \
            "HTTPS-only install retained an HTTP-01 challenge location"
    finally:
        declare_pools()
        _exit(patches, root)
