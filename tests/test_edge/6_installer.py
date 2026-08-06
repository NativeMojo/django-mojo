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
    cleanup, declare_reserved_names, make_certificate, make_domain, make_group,
    make_upstream, make_vhost, raises,
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
    declare_reserved_names()
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
        opts.vhost.kind = "spa"
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

        installed = json.load(open(installer.installed_path()))
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

        installed = json.load(open(installer.installed_path()))
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

        opts.vhost.kind = "proxy"
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

    assert "nginx" in " ".join(test_argv), f"unexpected test command: {test_argv}"
    assert "reload" in " ".join(reload_argv), \
        f"unexpected reload command: {reload_argv}"

    for argv in (test_argv, reload_argv):
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
