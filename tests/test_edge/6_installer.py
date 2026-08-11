"""
The node-side installer.

Every test here runs the installer against a TEMPORARY EDGE_ROOT with the
subprocess boundary replaced, so nothing touches a real nginx, a real
`/etc`, or a real systemd. That is safe to `mock.patch` because the installer
runs IN THIS PROCESS — it is a job handler, not something reached through
`opts.client`. (See `.claude/rules/testing.md`: patching only fails to reach
the separate server process, which nothing here uses.)

The properties under test are mostly ABSENCES — "the swap did not happen",
"`current` still points where it did", "no reload was issued". Those are
exactly the ones that rot silently, which is why each gets its own test rather
than a comment.
"""

import json
import os
import shutil
import tempfile
from unittest import mock

from testit import helpers as th

from tests.test_edge._helpers import (
    declare_pools,
    cleanup, make_certificate, make_domain, make_group,
    make_upstream, make_vhost, raises, with_setting,
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
    opts.group = make_group("edgeinstall")
    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.vhost = make_vhost(opts.domain, opts.certificate, label="www",
                            pool=_pool("happy"))

    # A HOUSE vhost (group-less domain) for the abort-vs-exclude distinction.
    opts.house_domain = make_domain(group=None)
    opts.house_cert = make_certificate(opts.house_domain)


def _root(opts):
    """A throwaway EDGE_ROOT for one test."""
    return tempfile.mkdtemp(prefix="edge-test-")


def _pool(name):
    """A pool nobody else in this module uses.

    Desired state is fleet-wide within a pool, so without this a vhost created
    by one test lands in another test's generation — the house vhost in
    `test_house_material_failure_aborts` aborted two unrelated installs before
    this existed.
    """
    return f"itest{name}"


def _same_path(a, b):
    """Compare paths through realpath.

    `tempfile.mkdtemp` returns /var/... on macOS, which is a symlink to
    /private/var/..., so a raw string compare of a symlink target against a
    constructed path fails for reasons that have nothing to do with the code
    under test. (The same confusion was a real bug in prune_generations.)
    """
    return os.path.realpath(a) == os.path.realpath(b)


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


@th.django_unit_test("a clean install stages, validates, swaps and reloads")
def test_install_happy_path(opts):
    from mojo.apps.edge.services import installer, render
    from mojo.apps.dnsman.models import Certificate

    root = _root(opts)
    patches = _with_root(root)
    _enter(patches)
    try:
        Certificate.objects.filter(pk=opts.certificate.pk).update(
            cert_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
        recorder = Recorder()
        with mock.patch.object(installer, "_run", recorder):
            result = installer.install(pool=_pool("happy"))

        assert result.changed, "a first install reported no change"
        assert recorder.reloaded, "nginx was never reloaded"
        assert len(recorder.tested) == 2, (
            "expected two nginx -t runs (staged pre-filter, then the real "
            f"config), saw {len(recorder.tested)}")

        current = os.path.realpath(render.current_link())
        assert _same_path(current, render.generation_dir(result.generation)), \
            "current does not point at the installed generation"

        conf = os.path.join(current, "conf.d", f"{opts.vhost.pk}.conf")
        assert os.path.exists(conf), "the vhost config was not written"

        key = os.path.join(
            current, "certs", str(opts.certificate.pk), "privkey.pem")
        assert os.path.exists(key), "certificate material was not written"
        assert oct(os.stat(key).st_mode)[-3:] == "600", \
            f"private key is not 0600: {oct(os.stat(key).st_mode)}"
    finally:
        _exit(patches, root)


@th.django_unit_test("two assigned pools are served by one combined atomic generation")
def test_install_two_pools_combines_serving_state(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.edge.services import installer, readiness, render

    declare_pools(["alpha", "beta"])
    alpha_domain = make_domain(group=opts.group)
    alpha_cert = make_certificate(alpha_domain)
    make_vhost(alpha_domain, alpha_cert, label="alpha", pool="alpha")
    beta_domain = make_domain(group=opts.group)
    beta_cert = make_certificate(beta_domain)
    make_vhost(beta_domain, beta_cert, label="beta", pool="beta")
    Certificate.objects.filter(pk__in=[alpha_cert.pk, beta_cert.pk]).update(
        cert_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")

    root = _root(opts)
    patches = _with_root(root)
    _enter(patches)
    try:
        with mock.patch.object(installer, "_run", Recorder()):
            result = installer.install_pools(["alpha", "beta"])
        alpha = installer.read_installed("alpha")
        beta = installer.read_installed("beta")
        live_generation = os.path.basename(os.path.realpath(render.current_link()))
        assert result.pools == ["alpha", "beta"], \
            f"combined install lost an assigned pool: {result}"
        assert alpha["generation"] == result.pool_generations["alpha"], \
            "alpha evidence does not prove alpha's desired state"
        assert beta["generation"] == result.pool_generations["beta"], \
            "beta evidence does not prove beta's desired state"
        assert alpha["serving_generation"] == beta["serving_generation"] \
               == result.generation == live_generation, \
            "per-pool proof is green against different/non-serving generations"
        current = os.path.realpath(render.current_link())
        assert os.path.exists(os.path.join(current, "conf.d")), \
            "combined serving generation was not staged"
        assert len([name for name in os.listdir(os.path.join(current, "conf.d"))
                    if name.endswith(".conf")]) == 2, \
            "the live combined generation omitted one pool's vhost"
        proof = with_setting(
            "EDGE_NODE_ID", "edge-two-pool",
            lambda: readiness.local_node_proof({"pools": ["alpha", "beta"]}))
        assert all(
            evidence["serving_generation"] == evidence["current_generation"]
            == result.generation
            for evidence in proof["pools"].values()), \
            f"two-pool local proof did not prove the common live union: {proof}"
    finally:
        _exit(patches, root)
        declare_pools()


@th.django_unit_test("an unchanged generation is a no-op")
def test_idempotent(opts):
    from mojo.apps.edge.services import installer
    from mojo.apps.dnsman.models import Certificate

    root = _root(opts)
    patches = _with_root(root)
    _enter(patches)
    try:
        Certificate.objects.filter(pk=opts.certificate.pk).update(
            cert_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
        with mock.patch.object(installer, "_run", Recorder()):
            first = installer.install(pool=_pool("happy"))

        second_recorder = Recorder()
        with mock.patch.object(installer, "_run", second_recorder):
            second = installer.install(pool=_pool("happy"))

        assert not second.changed, "an unchanged generation was reinstalled"
        assert second.generation == first.generation, "the generation id moved"
        assert not second_recorder.calls, (
            "a no-op install still ran nginx — every node would reload on "
            "every poll")
    finally:
        _exit(patches, root)


@th.django_unit_test("same-row certificate renewal moves the generation and installs new material")
def test_certificate_renewal_converges(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.edge.services import installer, render

    root = _root(opts)
    patches = _with_root(root, material=False)
    _enter(patches)
    try:
        certificate_a = "-----BEGIN CERTIFICATE-----\nmaterial-a\n-----END CERTIFICATE-----\n"
        certificate_b = "-----BEGIN CERTIFICATE-----\nmaterial-b\n-----END CERTIFICATE-----\n"
        Certificate.objects.filter(pk=opts.certificate.pk).update(
            serial="a1", cert_pem=certificate_a, chain_pem="")

        with mock.patch.object(
                Certificate, "private_key_pem",
                new_callable=mock.PropertyMock) as private_key:
            private_key.return_value = (
                "-----BEGIN PRIVATE KEY-----\nkey-a\n-----END PRIVATE KEY-----\n")
            with mock.patch.object(installer, "_run", Recorder()):
                first = installer.install(pool=_pool("happy"))

            Certificate.objects.filter(pk=opts.certificate.pk).update(
                serial="b2", cert_pem=certificate_b, chain_pem="")
            private_key.return_value = (
                "-----BEGIN PRIVATE KEY-----\nkey-b\n-----END PRIVATE KEY-----\n")
            recorder = Recorder()
            with mock.patch.object(installer, "_run", recorder):
                renewed = installer.install(pool=_pool("happy"))

        assert renewed.generation != first.generation, (
            "renewing a Certificate row in place did not move the edge generation")
        assert renewed.changed, (
            "ordinary install treated renewed same-pk certificate material as unchanged")
        assert recorder.reloaded, (
            "ordinary renewal convergence staged no nginx reload")
        fullchain_path = os.path.join(
            render.generation_dir(renewed.generation), "certs",
            str(opts.certificate.pk), "fullchain.pem")
        with open(fullchain_path) as handle:
            fullchain = handle.read()
        assert "material-b" in fullchain, (
            f"the live renewed fullchain does not contain material B: {fullchain!r}")
        assert "material-a" not in fullchain, (
            f"the live renewed fullchain still contains material A: {fullchain!r}")
    finally:
        _exit(patches, root)


@th.django_unit_test("a generation failing the STAGED nginx -t never touches current")
def test_staged_failure_does_not_swap(opts):
    from mojo.apps.edge.services import installer, render
    from mojo.apps.dnsman.models import Certificate

    root = _root(opts)
    patches = _with_root(root)
    _enter(patches)
    try:
        Certificate.objects.filter(pk=opts.certificate.pk).update(
            cert_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")

        recorder = Recorder(
            lambda argv: FakeProc(1, stderr="nginx: [emerg] unknown directive\n"))
        with mock.patch.object(installer, "_run", recorder):
            err = raises(installer.install, pool=_pool("happy"))

        assert err is not None, "a broken generation installed cleanly"
        assert not os.path.islink(render.current_link()), \
            "current was created despite a failed staged validation"
        assert not recorder.reloaded, "nginx was reloaded after a failed test"
    finally:
        _exit(patches, root)


@th.django_unit_test("a generation failing the REAL nginx -t is reverted, unreloaded")
def test_real_config_failure_reverts(opts):
    """The property the whole swap-then-validate order exists for.

    nginx keeps serving the running configuration until something reloads it,
    so a bad `current` is harmless as long as it is reverted before any reload.
    This asserts both halves.
    """
    from mojo.apps.edge.services import installer, render
    from mojo.apps.dnsman.models import Certificate

    root = _root(opts)
    patches = _with_root(root)
    _enter(patches)
    try:
        Certificate.objects.filter(pk=opts.certificate.pk).update(
            cert_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")

        with mock.patch.object(installer, "_run", Recorder()):
            first = installer.install(pool=_pool("happy"))
        good_generation = os.path.realpath(render.current_link())

        # Change the desired state so a second install is attempted, and make
        # the REAL config test (the second -t call) fail.
        opts.vhost.spa = True
        opts.vhost.save()

        state = {"tests": 0}

        def only_real_fails(argv):
            if "-t" in argv:
                state["tests"] += 1
                if state["tests"] == 2:
                    return FakeProc(1, stderr="nginx: [emerg] cannot load cert\n")
            return _ok(argv)

        recorder = Recorder(only_real_fails)
        with mock.patch.object(installer, "_run", recorder):
            err = raises(installer.install, pool=_pool("happy"))

        assert err is not None, "a generation failing the real config installed"
        assert _same_path(render.current_link(), good_generation), (
            "current was NOT reverted — the node is pointing at a generation "
            "nginx refused")
        assert not recorder.reloaded, (
            "nginx was reloaded despite the real-config test failing — the "
            "node would now be serving broken config")

        installed = json.load(open(installer.installed_path(_pool("happy"))))
        assert installed["generation"] == first.generation, \
            "installed.json advanced past a generation that never loaded"
    finally:
        _exit(patches, root)


@th.django_unit_test("a server_name collision fails the install even though nginx exits 0")
def test_conflicting_server_name_is_a_failure(opts):
    """nginx reports a duplicate as a WARNING and exits 0, having silently
    dropped one server block. Treating that as success is how a site stops
    being served with no error anywhere."""
    from mojo.apps.edge.services import installer, render
    from mojo.apps.dnsman.models import Certificate

    root = _root(opts)
    patches = _with_root(root)
    _enter(patches)
    try:
        Certificate.objects.filter(pk=opts.certificate.pk).update(
            cert_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")

        recorder = Recorder(lambda argv: FakeProc(
            0, stderr='nginx: [warn] conflicting server name "x" on 0.0.0.0:443, ignored\n'))
        with mock.patch.object(installer, "_run", recorder):
            err = raises(installer.install, pool=_pool("happy"))

        assert err is not None, (
            "a server_name collision installed cleanly because nginx exited 0")
        assert not recorder.reloaded, "nginx was reloaded despite a collision"
    finally:
        _exit(patches, root)


@th.django_unit_test("a tenant vhost with unreadable material is EXCLUDED, not fatal")
def test_tenant_material_failure_excludes(opts):
    """One tenant's broken certificate must not freeze the whole pool."""
    from mojo.apps.edge.services import installer
    from mojo.apps.dnsman.models import Certificate

    root = _root(opts)
    # material=False: the fixture certificates have no readable key, which IS
    # the KMS-unavailable branch.
    patches = _with_root(root, material=False)
    _enter(patches)
    try:
        good_domain = make_domain(group=opts.group)
        good_cert = make_certificate(good_domain)
        good_vhost = make_vhost(good_domain, good_cert, label="ok",
                                pool=_pool("exclude"))

        # Only the good one gets material.
        real_property = Certificate.private_key_pem

        def selective(self):
            if self.pk == good_cert.pk:
                return "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n"
            return None

        Certificate.objects.filter(pk=good_cert.pk).update(
            cert_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
        # The excluded one must share this test's pool, or there is nothing to
        # exclude.
        from mojo.apps.edge.models import Vhost
        Vhost.objects.filter(pk=opts.vhost.pk).update(pool=_pool("exclude"))

        with mock.patch.object(Certificate, "private_key_pem",
                               property(selective)):
            with mock.patch.object(installer, "_run", Recorder()):
                result = installer.install(pool=_pool("exclude"))

        assert result.changed, "the install did not converge"
        assert opts.vhost.pk in result.excluded, \
            "the vhost with unreadable material was not excluded"
        assert good_vhost.pk not in result.excluded, \
            "a healthy vhost was excluded"

        installed = json.load(open(installer.installed_path(_pool("exclude"))))
        assert opts.vhost.pk in installed["excluded"], \
            "installed.json does not record the exclusion — it is invisible"
    finally:
        from mojo.apps.edge.models import Vhost
        Vhost.objects.filter(pk=opts.vhost.pk).update(pool=_pool("happy"))
        _exit(patches, root)


@th.django_unit_test("a HOUSE vhost with unreadable material aborts the install")
def test_house_material_failure_aborts(opts):
    """The platform's own serving path. Converging without it is not a partial
    success — it is the API going dark."""
    from mojo.apps.edge.services import installer, render

    root = _root(opts)
    patches = _with_root(root, material=False)
    _enter(patches)
    try:
        make_vhost(opts.house_domain, opts.house_cert, label="www",
                   pool=_pool("house"))

        recorder = Recorder()
        with mock.patch.object(installer, "_run", recorder):
            err = raises(installer.install, pool=_pool("house"))

        assert err is not None, \
            "a house vhost with no readable certificate installed anyway"
        assert not os.path.islink(render.current_link()), \
            "current was swapped despite a house vhost failing to stage"
        assert not recorder.reloaded, "nginx was reloaded after a house failure"
    finally:
        _exit(patches, root)


@th.django_unit_test("a failed install leaves the previous generation on disk")
def test_stale_config_still_serves(opts):
    """Stale config serves; missing config does not.

    An absence-of-behaviour property: no code path deletes or repoints
    `current` on a failure, so a node whose DB is unreachable at boot starts
    nginx from the retained generation.
    """
    from mojo.apps.edge.services import installer, render
    from mojo.apps.dnsman.models import Certificate

    root = _root(opts)
    patches = _with_root(root)
    _enter(patches)
    try:
        Certificate.objects.filter(pk=opts.certificate.pk).update(
            cert_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
        with mock.patch.object(installer, "_run", Recorder()):
            installer.install(pool=_pool("happy"))

        good = os.path.realpath(render.current_link())
        conf_count = len(os.listdir(os.path.join(good, "conf.d")))

        opts.vhost.kind = "api"
        opts.vhost.spa = False   # an earlier test flipped it; api forbids it
        opts.vhost.upstream = make_upstream(host="127.0.0.1", port=9999)
        opts.vhost.save()

        with mock.patch.object(installer, "_run", Recorder(
                lambda argv: FakeProc(1, stderr="boom"))):
            raises(installer.install, pool=_pool("happy"))

        assert _same_path(render.current_link(), good), \
            "current moved after a failed install"
        assert len(os.listdir(os.path.join(good, "conf.d"))) == conf_count, \
            "the previous generation's config was modified in place"
    finally:
        _exit(patches, root)


@th.django_unit_test("a generation stages the full include graph")
def test_include_graph_staged(opts):
    """http.d base + upstreams land beside conf.d, no vhost file carries an
    `upstream {` block or a literal target, and the log directory the base's
    access_log points at exists before the staged nginx -t would open it."""
    from mojo.apps.edge.services import installer, render
    from mojo.apps.dnsman.models import Certificate

    root = _root(opts)
    patches = _with_root(root)
    _enter(patches)
    try:
        graph_domain = make_domain(group=opts.group)
        graph_cert = make_certificate(graph_domain)
        upstream = make_upstream(host="127.0.0.1", port=9911)
        make_vhost(graph_domain, graph_cert, label="api", kind="api",
                   upstream=upstream, pool=_pool("graph"))
        Certificate.objects.filter(pk=graph_cert.pk).update(
            cert_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")

        with mock.patch.object(installer, "_run", Recorder()):
            result = installer.install(pool=_pool("graph"))
        assert result.changed, "the graph install did not converge"

        current = os.path.realpath(render.current_link())
        base_path = os.path.join(current, "http.d", "00_base.conf")
        ups_path = os.path.join(current, "http.d", "10_upstreams.conf")
        assert os.path.exists(base_path), "http.d/00_base.conf was not staged"
        assert os.path.exists(ups_path), \
            "http.d/10_upstreams.conf was not staged"

        base = open(base_path).read()
        assert "map $http_upgrade $connection_upgrade {" in base, \
            "the upgrade map is missing from the base"
        assert "access_log " in base and "$loggable" in base, \
            "the base carries no health-filtered access log"

        ups = open(ups_path).read()
        assert f"upstream edge_up_{upstream.pk} {{" in ups, \
            "the referenced upstream has no named block"
        assert "127.0.0.1:9911" in ups, \
            "the upstream block does not carry the literal target"

        conf_d = os.path.join(current, "conf.d")
        for name in os.listdir(conf_d):
            text = open(os.path.join(conf_d, name)).read()
            assert "upstream " not in text, \
                f"{name} defines an upstream block — targets belong in http.d"
            assert "9911" not in text, \
                f"{name} carries a literal upstream target"

        assert os.path.isdir(render.log_dir()), \
            "EDGE_LOG_DIR was not created — the staged nginx -t would fail " \
            "opening the access log"

        # The staged tree: a listen-remapped copy of every rendered file,
        # because the unprivileged staged `nginx -t` DOES attempt bind() and
        # EACCES on 443/80 is fatal (item 1623 — froze converge fleet-wide).
        staged_base = os.path.join(current, "staging", "http.d", "00_base.conf")
        staged_ups = os.path.join(
            current, "staging", "http.d", "10_upstreams.conf")
        assert os.path.exists(staged_base), \
            "staging/http.d/00_base.conf was not staged — the staged nginx -t " \
            "has nothing unprivileged to validate"
        assert os.path.exists(staged_ups), \
            "staging/http.d/10_upstreams.conf was not staged"

        staged_conf_d = os.path.join(current, "staging", "conf.d")
        assert os.path.isdir(staged_conf_d) and os.listdir(staged_conf_d), \
            "staging/conf.d/ is empty — no vhost reaches the staged check"

        staged_ports = []
        for dirpath in (os.path.join(current, "staging", "http.d"),
                        staged_conf_d):
            for name in os.listdir(dirpath):
                staged_ports += _listen_ports(
                    open(os.path.join(dirpath, name)).read())
        assert staged_ports, "the staged copies carry no listen directives"
        privileged = [p for p in staged_ports if p < 1024]
        assert not privileged, (
            f"staged copies still listen on privileged ports {privileged} — "
            "the unprivileged staged nginx -t would fail bind() with EACCES")
        expected = {render.staged_http_port(), render.staged_https_port()}
        assert set(staged_ports) == expected, (
            f"staged listen ports {sorted(set(staged_ports))} are not the "
            f"configured staged ports {sorted(expected)}")

        # And the real conf.d must be untouched by the remap.
        real_ports = []
        for name in os.listdir(conf_d):
            real_ports += _listen_ports(open(os.path.join(conf_d, name)).read())
        assert set(real_ports) == {443, 80}, (
            f"the REAL conf.d listen ports moved: {sorted(set(real_ports))} — "
            "the remap belongs only in staging/")

        harness = open(os.path.join(current, "nginx.conf")).read()
        from mojo.deploy.nginx_runtime import TEMP_PATHS
        declared_generation = render.generation_dir(os.path.basename(current))
        staged_base_text = open(staged_base).read()
        for directive, leaf in TEMP_PATHS:
            scratch = os.path.join(current, "tmp", leaf)
            assert os.path.isdir(scratch), (
                f"staged nginx scratch leaf is missing for {directive}: {scratch}")
            declaration = f"{directive} {declared_generation}/tmp/{leaf};"
            assert harness.count(directive) == 0, (
                f"staging harness duplicated the scratch owner for {directive}")
            assert staged_base_text.count(declaration) == 1, (
                f"staged http base must declare {declaration!r} exactly once")
        assert "/staging/http.d/*.conf" in harness, \
            "the harness does not include staging/http.d — the staged check " \
            "would still parse the privileged listens"
        assert "/staging/conf.d/*.conf" in harness, \
            "the harness does not include staging/conf.d"
    finally:
        _exit(patches, root)


@th.django_unit_test("a tenant vhost proxying a RETIRED upstream is excluded, not fatal")
def test_retired_upstream_excludes(opts):
    """The retire contract: the vhost stops being served (excluded, with an
    incident) rather than the fleet freezing or traffic being repointed."""
    from mojo.apps.edge.models import Upstream
    from mojo.apps.edge.services import installer
    from mojo.apps.dnsman.models import Certificate

    root = _root(opts)
    patches = _with_root(root)
    _enter(patches)
    try:
        retired = make_upstream(host="127.0.0.1", port=9921)
        bad_domain = make_domain(group=opts.group)
        bad_cert = make_certificate(bad_domain)
        bad_vhost = make_vhost(bad_domain, bad_cert, label="dead", kind="api",
                               upstream=retired, pool=_pool("retired"))

        good_domain = make_domain(group=opts.group)
        good_cert = make_certificate(good_domain)
        good_vhost = make_vhost(good_domain, good_cert, label="ok",
                                pool=_pool("retired"))

        Certificate.objects.filter(pk__in=[bad_cert.pk, good_cert.pk]).update(
            cert_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
        Upstream.objects.filter(pk=retired.pk).update(is_enabled=False)

        with mock.patch.object(installer, "_run", Recorder()):
            result = installer.install(pool=_pool("retired"))

        assert result.changed, "the install did not converge"
        assert bad_vhost.pk in result.excluded, \
            "the vhost proxying a retired upstream was not excluded"
        assert good_vhost.pk not in result.excluded, \
            "a healthy vhost was excluded alongside the retired one"
    finally:
        _exit(patches, root)


@th.django_unit_test("a HOUSE vhost proxying a RETIRED upstream aborts the install")
def test_retired_upstream_house_aborts(opts):
    from mojo.apps.edge.models import Upstream
    from mojo.apps.edge.services import installer, render
    from mojo.apps.dnsman.models import Certificate

    root = _root(opts)
    patches = _with_root(root)
    _enter(patches)
    try:
        retired = make_upstream(host="127.0.0.1", port=9931)
        house_domain = make_domain(group=None)
        house_cert = make_certificate(house_domain)
        make_vhost(house_domain, house_cert, label="api", kind="api",
                   upstream=retired, pool=_pool("houseup"))
        Certificate.objects.filter(pk=house_cert.pk).update(
            cert_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
        Upstream.objects.filter(pk=retired.pk).update(is_enabled=False)

        recorder = Recorder()
        with mock.patch.object(installer, "_run", recorder):
            err = raises(installer.install, pool=_pool("houseup"))

        assert err is not None, \
            "a house vhost proxying a retired upstream installed anyway"
        assert not os.path.islink(render.current_link()), \
            "current was swapped despite the house abort"
        assert not recorder.reloaded, "nginx was reloaded after a house abort"
    finally:
        _exit(patches, root)


@th.django_unit_test("old generations are pruned but the live one never is")
def test_prune_keeps_current(opts):
    from mojo.apps.edge.services import installer, render

    root = _root(opts)
    patches = _with_root(root)
    _enter(patches)
    try:
        gen_root = os.path.join(root, "generations")
        os.makedirs(gen_root)
        made = []
        for index in range(8):
            path = os.path.join(gen_root, f"gen{index}")
            os.makedirs(path)
            made.append(path)

        installer._symlink_swap(render.current_link(), made[0])
        installer.prune_generations(keep=3)

        assert os.path.isdir(made[0]), \
            "the LIVE generation was pruned — rollback target destroyed"
        surviving = [p for p in made if os.path.isdir(p)]
        assert len(surviving) == 3, \
            f"expected 3 generations to survive keep=3, found {len(surviving)}"
    finally:
        _exit(patches, root)


@th.django_unit_test("the privileged commands are constants, never built from row data")
def test_commands_are_constant(opts):
    """A vhost label, an upstream host and a domain name are all validated —
    and none of them belongs anywhere near an argv list."""
    from mojo.apps.edge.services import installer

    test_argv = installer._nginx_test_argv()
    reload_argv = installer._nginx_reload_argv()
    staged_argv = installer._nginx_staged_test_argv("/tmp/gen/nginx.conf")

    assert "nginx" in " ".join(test_argv), f"unexpected test command: {test_argv}"
    assert "reload" in " ".join(reload_argv), \
        f"unexpected reload command: {reload_argv}"

    # The authoritative check takes NO arguments, so the sudoers entry that
    # permits it can be an exact command with no wildcard.
    assert test_argv[-1] == "-t", (
        "the root nginx check grew an argument — the sudoers rule permitting "
        f"it now needs a wildcard: {test_argv}")

    # And the staged check, which DOES take an app-writable path, must never be
    # privileged: `nginx -t` dlopen()s load_module targets as whatever user it
    # runs as, so `sudo nginx -t -c <app-writable file>` is a root escalation.
    assert "sudo" not in staged_argv, (
        "the staged nginx check runs under sudo against a file the app user "
        f"writes — that is root code execution via load_module: {staged_argv}")

    # The staged default routes nginx's pre-config log to stderr (`-e stderr`),
    # so the unprivileged check never emits the misleading "could not open
    # error log file" alert for the root-owned compiled-in default path.
    assert staged_argv[-2:] == ["-c", "/tmp/gen/nginx.conf"], (
        f"the staged argv no longer ends with the config path: {staged_argv}")
    assert "-e" in staged_argv and \
        staged_argv[staged_argv.index("-e") + 1] == "stderr", (
        "the staged argv lost `-e stderr` — every staged failure will again "
        f"lead with the harmless default-error-log alert: {staged_argv}")

    for argv in (test_argv, reload_argv, staged_argv):
        for token in argv:
            assert opts.domain.name not in token, \
                f"row data leaked into a privileged command: {argv}"
        assert all(isinstance(token, str) for token in argv), \
            "a command argument is not a string"


@th.django_unit_test("the installer and the REST endpoint agree on the generation")
def test_installer_matches_endpoint(opts):
    """They share render.desired_state() precisely so they cannot drift.

    If these ever disagree, a node installs one thing while the fleet's
    desired-state answer describes another — the two-convergence-loops failure
    the shared spine exists to prevent.
    """
    from mojo.apps.edge.rest.node import enabled_vhosts
    from mojo.apps.edge.services import render

    vhosts = enabled_vhosts(_pool("happy"))
    from_endpoint = render.desired_state(vhosts)["generation"]
    from_installer = render.desired_state(enabled_vhosts(_pool("happy")))["generation"]

    assert from_endpoint == from_installer, \
        "the endpoint and the installer computed different generation ids"
