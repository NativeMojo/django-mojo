"""
Node-side release install: staging, verification, and rollback without a
re-download.

The property that carries the pair: **the web-root pointer lives inside the
generation**, so one `os.replace` of `current` swaps configuration and content
together. An earlier design put a `current` symlink next to the release,
outside the atomic swap — a failed `nginx -t` then abandoned the config change
but left the content already moved, and the node served a new bundle under old
config, a state neither generation describes.
"""

import os
import shutil
import tempfile
from unittest import mock

from testit import helpers as th

from tests.test_edge._helpers import (
    RELEASE_BUCKET, declare_pools,
    cleanup, declare_release_buckets, declare_reserved_names, make_certificate,
    make_domain, make_group, make_release, make_vhost, make_webapp, raises,
)


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ok(argv):
    return FakeProc(0, stderr="nginx: configuration file test is successful\n")


class Recorder:
    def __init__(self, behaviour=None):
        self.calls = []
        self.behaviour = behaviour or _ok

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.behaviour(argv)

    @property
    def reloaded(self):
        return any("reload" in " ".join(argv) for argv in self.calls)


POOL = "reltest"


@th.django_unit_setup()
def setup_release_installer(opts):
    cleanup()
    declare_reserved_names()
    declare_pools()
    declare_release_buckets()
    opts.group = make_group("edgerelinstall")
    opts.domain = make_domain(group=opts.group)
    opts.certificate = make_certificate(opts.domain)
    opts.vhost = make_vhost(opts.domain, opts.certificate, label="www",
                            kind="site", pool=POOL)
    opts.webapp = make_webapp(opts.group, slug="relsite", vhost=opts.vhost)


def _stage_release_on_disk(www_base, vhost_id, version, files=("index.html",)):
    """Pretend the node already downloaded and verified this release."""
    target = os.path.join(www_base, str(vhost_id), "releases", version)
    os.makedirs(target, exist_ok=True)
    for name in files:
        with open(os.path.join(target, name), "w") as handle:
            handle.write(f"{version}:{name}")
    return target


def _patches(root, www_base):
    return [
        mock.patch("mojo.apps.edge.services.render.edge_root", return_value=root),
        mock.patch("mojo.apps.edge.validators.www_base", return_value=www_base),
        # This module stages release bytes by hand, deliberately NOT matching
        # the manifests its fixtures declare — so a real fetch would try to
        # pull every file from S3. The fetch itself is covered in
        # `20_www_sync.py`; here it is stubbed to "everything is already on
        # disk" so these tests keep testing the installer.
        mock.patch("mojo.apps.edge.services.www_sync.fetch_webapps",
                   return_value={}),
        mock.patch(
            "mojo.apps.dnsman.models.certificate.Certificate.private_key_pem",
            new_callable=mock.PropertyMock,
            return_value="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n"),
    ]


def _enter(patches):
    for patch in patches:
        patch.start()


def _exit(patches, *dirs):
    for patch in reversed(patches):
        patch.stop()
    for path in dirs:
        shutil.rmtree(path, ignore_errors=True)


def _seed_cert(opts, certificate=None):
    """Give a certificate readable material, or the installer excludes its
    vhost before any release logic runs."""
    from mojo.apps.dnsman.models import Certificate

    pk = (certificate or opts.certificate).pk
    Certificate.objects.filter(pk=pk).update(
        cert_pem="-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")


@th.django_unit_test("the web root points at the promoted release, inside the generation")
def test_web_root_links_into_the_generation(opts):
    from mojo.apps.edge.services import installer, releases, render

    root = tempfile.mkdtemp(prefix="edge-rel-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    try:
        _seed_cert(opts)
        release = make_release(opts.webapp, "root1", status="uploaded")
        releases.promote(opts.webapp, release)
        target = _stage_release_on_disk(www, opts.vhost.pk, "root1")

        with mock.patch.object(installer, "_run", Recorder()):
            result = installer.install(pool=POOL)

        link = render.www_dir(result.generation, opts.vhost.pk)
        assert os.path.islink(link), \
            f"the web root is not a symlink into the generation: {link}"
        assert os.path.realpath(link) == os.path.realpath(target), \
            f"the web root points at {os.path.realpath(link)}, not {target}"
        assert os.path.exists(os.path.join(link, "index.html")), \
            "the release content is not reachable through the web root"
    finally:
        _exit(patches, root, www)


@th.django_unit_test("an unfetchable release on a never-served vhost darkens THAT vhost only")
def test_unfetchable_release_excludes_one_vhost(opts):
    """The pool still converges. A vhost with no previous release has nothing
    to keep serving, so it goes dark rather than pointing at nothing — and the
    version is recorded so the next converge retries the fetch."""
    from mojo.apps.edge.services import installer, releases, render

    root = tempfile.mkdtemp(prefix="edge-rel-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    other = None
    try:
        _seed_cert(opts)
        # A second, healthy vhost in the same pool: the point of the degrade is
        # that IT still installs.
        other_domain = make_domain(group=opts.group)
        other_cert = make_certificate(other_domain)
        _seed_cert(opts, other_cert)
        other = make_vhost(other_domain, other_cert, label="ok", kind="site",
                           pool=POOL)

        release = make_release(opts.webapp, "absent1", status="uploaded")
        releases.promote(opts.webapp, release)
        # deliberately NOT staged on disk, and the fetch cannot get it either

        recorder = Recorder()
        failures = {opts.vhost.pk: "relsite release absent1 could not be "
                                   "fetched: index.html: no such object"}
        with mock.patch("mojo.apps.edge.services.www_sync.fetch_webapps",
                        return_value=failures):
            with mock.patch.object(installer, "_run", recorder):
                result = installer.install(pool=POOL)

        assert result.changed, \
            "one unfetchable release stopped the whole pool converging"
        assert opts.vhost.pk in result.excluded, \
            "the vhost whose release could not be fetched was not excluded"
        assert other.pk not in result.excluded, \
            "a healthy vhost was excluded over another vhost's release"

        installed = installer.read_installed()
        assert opts.vhost.pk in installed["excluded"], \
            "installed.json does not record the exclusion — it is invisible"
        assert installed.get("www_pending") == {str(opts.vhost.pk): "absent1"}, \
            (f"the pending fetch was not recorded, so the next converge would "
             f"short-circuit: {installed.get('www_pending')}")
        assert os.path.islink(render.current_link()), \
            "current was not swapped — the healthy vhost never went live"
        assert recorder.reloaded, "nginx was not reloaded for the healthy vhost"
    finally:
        # Leave no promoted-but-unstaged release behind: it would degrade every
        # later install in this module. The second vhost goes too — every other
        # test here reasons about a single-vhost pool.
        from mojo.apps.edge.models import Vhost, WebApp
        WebApp.objects.filter(pk=opts.webapp.pk).update(current_release=None)
        if other is not None:
            Vhost.objects.filter(pk=other.pk).delete()
        _exit(patches, root, www)


@th.django_unit_test("an unfetchable release degrades a HOUSE vhost too, never the pool")
def test_unfetchable_release_on_a_house_vhost_degrades(opts):
    """The degrade forks on the REASON, not on who owns the vhost.

    Point web apps at house-owned vhosts — one domain, house-owned, teams
    below it — and the ownership fork would send every first-promote fetch
    failure down the abort branch: the fleet-wide freeze the fetch degrade
    exists to remove, reintroduced for every web app.
    """
    from mojo.apps.edge.services import installer, render

    root = tempfile.mkdtemp(prefix="edge-rel-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    house = None
    try:
        _seed_cert(opts)
        house_domain = make_domain(group=None)
        house_cert = make_certificate(house_domain)
        _seed_cert(opts, house_cert)
        house = make_vhost(house_domain, house_cert, label="app", kind="site",
                           pool=POOL)

        recorder = Recorder()
        failures = {house.pk: "houseapp release h1 could not be fetched: "
                              "index.html: no such object"}
        with mock.patch("mojo.apps.edge.services.www_sync.fetch_webapps",
                        return_value=failures):
            with mock.patch.object(installer, "_run", recorder):
                result = installer.install(pool=POOL)

        assert result.changed, \
            ("a house vhost's missing release aborted the whole install — the "
             "pool-wide freeze the degrade path exists to prevent")
        assert house.pk in result.excluded, \
            "the house vhost whose release could not be fetched was not excluded"
        assert opts.vhost.pk not in result.excluded, \
            "a healthy vhost was excluded over the house vhost's release"
        assert os.path.islink(render.current_link()), \
            "current was not swapped — the rest of the pool never went live"
    finally:
        from mojo.apps.edge.models import Vhost
        if house is not None:
            Vhost.objects.filter(pk=house.pk).delete()
        _exit(patches, root, www)


@th.django_unit_test("a vhost whose key material will not read is retried, not left excluded")
def test_cert_failure_is_retried_next_converge(opts):
    """KMS comes back. Nothing about the desired state changes when it does,
    so without a recorded pending marker the generation short-circuits and the
    vhost stays excluded until something unrelated moves it."""
    from mojo.apps.edge.services import installer

    root = tempfile.mkdtemp(prefix="edge-rel-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    broken = None
    try:
        _seed_cert(opts)
        # A second vhost whose certificate has NO material: _write_material
        # fails for it and it is excluded.
        broken_domain = make_domain(group=opts.group)
        broken_cert = make_certificate(broken_domain)
        broken = make_vhost(broken_domain, broken_cert, label="nokey",
                            kind="site", pool=POOL)

        with mock.patch.object(installer, "_run", Recorder()):
            first = installer.install(pool=POOL)

        assert broken.pk in first.excluded, \
            "a vhost with unreadable key material was not excluded"
        installed = installer.read_installed()
        assert installed.get("cert_pending") == [broken.pk], \
            (f"the certificate failure was not recorded, so the next converge "
             f"would short-circuit: {installed.get('cert_pending')}")

        # Same desired state, same generation: the retry must still run.
        with mock.patch.object(installer, "_run", Recorder()):
            second = installer.install(pool=POOL)
        assert second.changed, \
            ("the converge short-circuited on an unchanged generation, so the "
             "excluded vhost would never retry once KMS recovered")

        # Now the material reads, and the vhost installs without anything else
        # about the desired state changing.
        _seed_cert(opts, broken_cert)
        with mock.patch.object(installer, "_run", Recorder()):
            third = installer.install(pool=POOL)
        assert broken.pk not in third.excluded, \
            "the vhost stayed excluded after its key material became readable"
        assert not installer.read_installed().get("cert_pending"), \
            "a healed node still reports a pending certificate"
    finally:
        from mojo.apps.edge.models import Vhost
        if broken is not None:
            Vhost.objects.filter(pk=broken.pk).delete()
        _exit(patches, root, www)


@th.django_unit_test("stage_web_roots still refuses a release that is not on disk")
def test_stage_web_roots_requires_the_release_on_disk(opts):
    """The safety net BEHIND the fetch. www_sync degrades what it cannot pull,
    so reaching here means the directory went missing some other way — and a
    live vhost pointing at nothing is worse than a refused generation."""
    from mojo.apps.edge.services import installer, render

    root = tempfile.mkdtemp(prefix="edge-rel-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    try:
        generation = "safetynet"
        os.makedirs(os.path.join(render.generation_dir(generation), "www"))
        webapps = [dict(vhost=opts.vhost.pk, slug="relsite",
                        bucket=RELEASE_BUCKET,
                        release=dict(id=1, version="ghost1",
                                     prefix="webapps/1/1/releases/ghost1",
                                     manifest=[]))]

        # No fallback: nothing was ever served, and the directory is absent.
        err = raises(installer.stage_web_roots, generation, [opts.vhost],
                     webapps)
        assert isinstance(err, installer.InstallError), \
            f"an absent release directory did not raise InstallError: {err!r}"
        assert "ghost1" in str(err), \
            f"the error does not name the missing release: {err}"
        assert not os.path.islink(render.www_dir(generation, opts.vhost.pk)), \
            "a web root was linked at a release that is not on disk"
    finally:
        _exit(patches, root, www)


@th.django_unit_test("rollback flips the symlink without re-downloading")
def test_rollback_is_a_symlink_flip(opts):
    """Retained releases are the reason rollback is cheap. This asserts the
    old files were never removed and are reused as-is."""
    from mojo.apps.edge.services import installer, releases, render

    root = tempfile.mkdtemp(prefix="edge-rel-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    try:
        _seed_cert(opts)
        v1 = make_release(opts.webapp, "back1", status="uploaded")
        v2 = make_release(opts.webapp, "back2", status="uploaded")
        v1_dir = _stage_release_on_disk(www, opts.vhost.pk, "back1")
        v2_dir = _stage_release_on_disk(www, opts.vhost.pk, "back2")

        releases.promote(opts.webapp, v1)
        with mock.patch.object(installer, "_run", Recorder()):
            first = installer.install(pool=POOL)

        releases.promote(opts.webapp, v2)
        with mock.patch.object(installer, "_run", Recorder()):
            second = installer.install(pool=POOL)

        link = render.www_dir(second.generation, opts.vhost.pk)
        assert os.path.realpath(link) == os.path.realpath(v2_dir), \
            "the web root did not follow the promote"
        assert os.path.isdir(v1_dir), \
            "the previous release was deleted — rollback would need a re-download"

        # Roll back.
        releases.promote(opts.webapp, v1)
        with mock.patch.object(installer, "_run", Recorder()):
            third = installer.install(pool=POOL)

        link = render.www_dir(third.generation, opts.vhost.pk)
        assert os.path.realpath(link) == os.path.realpath(v1_dir), \
            "rollback did not restore the previous release"
        with open(os.path.join(link, "index.html")) as handle:
            assert handle.read() == "back1:index.html", \
                "the rolled-back content is not the original bytes"
        assert third.generation == first.generation, (
            "rolling back to an identical desired state produced a different "
            "generation id — the hash is not a function of state alone")
    finally:
        _exit(patches, root, www)


@th.django_unit_test("a promote changes the generation, so the node reinstalls")
def test_promote_triggers_reinstall(opts):
    from mojo.apps.edge.services import installer, releases

    root = tempfile.mkdtemp(prefix="edge-rel-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    try:
        _seed_cert(opts)
        v1 = make_release(opts.webapp, "trig1", status="uploaded")
        v2 = make_release(opts.webapp, "trig2", status="uploaded")
        _stage_release_on_disk(www, opts.vhost.pk, "trig1")
        _stage_release_on_disk(www, opts.vhost.pk, "trig2")

        releases.promote(opts.webapp, v1)
        with mock.patch.object(installer, "_run", Recorder()):
            installer.install(pool=POOL)
            # Same state twice: nothing to do.
            again = installer.install(pool=POOL)
        assert not again.changed, "an unchanged promote state reinstalled"

        releases.promote(opts.webapp, v2)
        with mock.patch.object(installer, "_run", Recorder()) as _:
            after = installer.install(pool=POOL)
        assert after.changed, \
            "a promote did not make the node reinstall — the hash missed it"
    finally:
        _exit(patches, root, www)


@th.django_unit_test("a vhost with no release gets an empty root, not a broken link")
def test_no_release_yet(opts):
    """404 is the better of two bad answers; a missing root makes nginx 500."""
    from mojo.apps.edge.services import installer, render

    root = tempfile.mkdtemp(prefix="edge-rel-")
    www = tempfile.mkdtemp(prefix="edge-www-")
    patches = _patches(root, www)
    _enter(patches)
    try:
        _seed_cert(opts)
        # Earlier tests in this module leave a release promoted; this one is
        # specifically about a vhost that has never had one.
        from mojo.apps.edge.models import WebApp
        WebApp.objects.filter(pk=opts.webapp.pk).update(current_release=None)

        with mock.patch.object(installer, "_run", Recorder()):
            result = installer.install(pool=POOL)

        link = render.www_dir(result.generation, opts.vhost.pk)
        assert os.path.isdir(link), \
            "a vhost with no release has no web root at all"
        assert not os.path.islink(link), \
            "an empty web root should be a directory, not a dangling symlink"
    finally:
        _exit(patches, root, www)


@th.django_unit_test("a release version that could escape its directory is refused")
def test_release_dir_is_contained(opts):
    from mojo.apps.edge.services import installer

    for version in ["../../etc", "a/b", ".."]:
        err = raises(installer.release_dir, opts.vhost.pk, version)
        assert err is not None, \
            f"release_dir accepted the version {version!r} — that escapes the site"


@th.django_unit_test("the node download endpoint refuses a non-current release")
def test_download_refuses_non_current(opts):
    """A node installs `current_release` and nothing else, so that is all the
    download endpoint will hand out."""
    from mojo.apps.edge.services import releases

    v1 = make_release(opts.webapp, "cur1", status="uploaded")
    v2 = make_release(opts.webapp, "cur2", status="uploaded")
    releases.promote(opts.webapp, v1)

    rows = releases.desired_webapps([opts.vhost])
    assert len(rows) == 1, f"expected one webapp row, got {rows}"
    assert rows[0]["release"]["version"] == "cur1", \
        f"the desired state names the wrong release: {rows[0]['release']}"
    assert rows[0]["release"]["id"] != v2.pk, \
        "a superseded release appeared in the desired state"
