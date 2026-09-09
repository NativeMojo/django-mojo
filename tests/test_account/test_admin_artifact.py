"""Consumer artifact, offline transaction and source-session boundaries."""

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile

from testit import helpers as th

TESTIT_TIER = "admin"
ROOT = Path(__file__).resolve().parents[2]


def load(relative):
    spec = importlib.util.spec_from_file_location("artifact_test_module", ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(root):
    root.mkdir(parents=True)
    (root / ".vite").mkdir()
    (root / "index.html").write_text('<!doctype html><title>Fixture</title>')
    (root / ".vite/manifest.json").write_text('{}')
    data = {
        "schema_version": 1, "artifact": "portal-mojo-admin", "version": "1.0.0",
        "source_revision": "a" * 40, "source_dirty": False,
        "toolchain": {"node": "24.21.0", "npm": "11.19.0"},
        "lockfile_sha256": "b" * 64, "entrypoint": "index.html", "asset_base": "./",
        "api_mode": "same-origin", "source_session_contract": 1,
        "vite_manifest": ".vite/manifest.json", "files": [],
    }
    for name in (".vite/manifest.json", "index.html"):
        content = (root / name).read_bytes()
        data["files"].append({"path": name, "size": len(content),
                              "sha256": hashlib.sha256(content).hexdigest()})
    (root / "admin-artifact.json").write_text(json.dumps(data))
    return data


@th.django_unit_test("artifact validation rejects corrupt schemas, bytes and path kinds")
def test_artifact_validation(opts):
    module = load("mojo/apps/account/services/admin_artifact.py")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve() / "artifact"
        original = fixture(root)
        result = module.validate(root)
        assert result["allowlist"] == frozenset({"index.html"}), "metadata must not be runtime-deliverable"
        with th.assert_raises(TypeError):
            result["metadata"]["api_mode"] = "foreign"
        with th.assert_raises(module.ArtifactError):
            module.validate(root, "0" * 64)
        for field, bad in (("source_dirty", True), ("api_mode", "configured"),
                           ("asset_base", "/"), ("schema_version", True),
                           ("source_revision", "short"), ("toolchain", {})):
            value = {**original, field: bad}
            (root / "admin-artifact.json").write_text(json.dumps(value))
            with th.assert_raises(module.ArtifactError):
                module.validate(root)
        (root / "admin-artifact.json").write_text(json.dumps(original))
        for bad in ("../x", "/x", "a//b", "a/./b", "a\\b", "%2e", "a?b", "a#b"):
            with th.assert_raises(module.ArtifactError):
                module.safe_path(bad)
        (root / "extra.js").write_text("stale")
        with th.assert_raises(module.ArtifactError):
            module.validate(root)
        (root / "extra.js").unlink()
        content = (root / "index.html").read_bytes()
        (root / "index.html").write_bytes(content + b"changed")
        with th.assert_raises(module.ArtifactError):
            module.validate(root)
        (root / "index.html").unlink()
        (root / "index.html").symlink_to(root / ".vite/manifest.json")
        with th.assert_raises(module.ArtifactError):
            module.validate(root)


@th.django_unit_test("offline vendor is a no-op for identical bytes and restores a failed promotion")
def test_vendor_transaction(opts):
    module = load("scripts/vendor_admin_portal.py")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        source, destination = root / "source", root / "installed"
        fixture(source)
        digest = module.artifact.validate(source)["manifest_sha256"]
        destination.mkdir()
        (destination / "old.js").write_text("old")
        def fail_stage(left, right):
            if Path(left).name.startswith(".admin-portal-v2.stage-"):
                raise OSError("interrupted promotion")
            left.rename(right)
        with th.assert_raises(OSError):
            module.vendor(source, digest, destination, rename=fail_stage)
        assert (destination / "old.js").read_text() == "old", "failed promotion lost previous tree"
        module.vendor(source, digest, destination)
        assert not (destination / "old.js").exists(), "stale handwritten file survived replacement"
        before = (destination / "index.html").stat().st_mtime_ns
        module.vendor(source, digest, destination)
        assert (destination / "index.html").stat().st_mtime_ns == before, "identical input rewrote files"
        destination.rename(root / ".admin-portal-v2.backup")
        module.vendor(source, digest, destination)
        assert destination.is_dir(), "interrupted first rename was not recovered"
        with th.assert_raises(module.artifact.ArtifactError):
            module.vendor(destination, digest, destination)
        # A second interruption can prevent automatic restoration. Keep the
        # deterministic backup so the next invocation can recover it.
        (destination / "index.html").write_text("old but recoverable")
        def fail_promotion_and_restore(left, right):
            if Path(left) == destination:
                left.rename(right)
            else:
                raise OSError("promotion and restoration interrupted")
        with th.assert_raises(OSError):
            module.vendor(source, digest, destination, rename=fail_promotion_and_restore)
        assert (root / ".admin-portal-v2.backup/index.html").read_text() == "old but recoverable", "restoration failure discarded recoverable backup"
        module.vendor(source, digest, destination)
        assert module.artifact.validate(destination, digest), "recovery could not install pinned source"


@th.django_unit_test("vendor lock contention refuses a concurrent replacement")
def test_vendor_lock(opts):
    import fcntl
    module = load("scripts/vendor_admin_portal.py")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        source = root / "source"
        fixture(source)
        digest = module.artifact.validate(source)["manifest_sha256"]
        with (root / ".admin-portal-v2.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with th.assert_raises(BlockingIOError):
                module.vendor(source, digest, root / "installed")


@th.django_unit_test("archive proof rejects duplicate, traversing and symlink members")
def test_archive_boundaries(opts):
    import zipfile
    module = load("scripts/verify_admin_portal_package.py")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        source = root / "source"
        fixture(source)
        digest = module.artifact.validate(source)["manifest_sha256"]
        archive = root / "valid.whl"
        with zipfile.ZipFile(archive, "w") as output:
            for path in source.rglob("*"):
                if path.is_file():
                    output.write(path, module.PREFIX + path.relative_to(source).as_posix())
        result, _ = module.inspect_archive(archive, digest, root / "unpacked")
        assert result["manifest_sha256"] == digest, "wheel identity changed"
        for name, mode in (("../escape", 0), ("link", 0o120777)):
            bad = root / "bad.whl"
            with zipfile.ZipFile(bad, "w") as output:
                entry = zipfile.ZipInfo(name)
                entry.external_attr = mode << 16
                output.writestr(entry, "target")
            with th.assert_raises(module.artifact.ArtifactError):
                module.inspect_archive(bad, digest, root / "bad-output")
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(root / "duplicate.whl", "w") as output:
                output.writestr("duplicate", "one")
                output.writestr("duplicate", "two")
        with th.assert_raises(module.artifact.ArtifactError):
            module.inspect_archive(root / "duplicate.whl", digest, root / "duplicates")


@th.unit_test("package verifier resolves only its own temporary-directory alias")
def test_verifier_owned_temporary_alias(opts):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import tarfile
    import zipfile
    module = load("scripts/verify_admin_portal_package.py")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        repo = root / "repo"
        source = repo / module.PREFIX
        fixture(source)
        digest = module.artifact.validate(source)["manifest_sha256"]
        dist = root / "dist"
        dist.mkdir()
        with zipfile.ZipFile(dist / "django_mojo-fixture.whl", "w") as output:
            for path in source.rglob("*"):
                if path.is_file():
                    output.write(path, path.relative_to(repo).as_posix())
        with tarfile.open(dist / "django_mojo-fixture.tar.gz", "w:gz") as output:
            output.add(source, arcname="django_mojo-fixture/" + module.PREFIX)
        owned = root / "owned-temporary-directory"
        owned.mkdir()
        alias = root / "temporary-alias"
        alias.symlink_to(owned, target_is_directory=True)
        # Both bindings belong only to this directly loaded verifier instance.
        module.REPO = repo
        module.tempfile = SimpleNamespace(TemporaryDirectory=lambda **kwargs: nullcontext(str(alias)))
        result = module.verify(dist, digest)
        assert result["manifest_sha256"] == digest, "owned temporary alias prevented archive proof"
        with th.assert_raises(module.artifact.ArtifactError):
            module.artifact.validate(alias / "wheel" / module.PREFIX, digest)
        user_alias = root / "user-artifact-alias"
        user_alias.symlink_to(source, target_is_directory=True)
        with th.assert_raises(module.artifact.ArtifactError):
            module.artifact.validate(user_alias, digest)


@th.django_unit_test("source session expiry is bounded by both JWT and deployment TTL")
def test_source_deadline(opts):
    import time
    from types import SimpleNamespace
    from django.core.cache import cache
    from django.http import HttpResponse
    from mojo.apps.account.services import admin_portal
    from mojo.apps.account.utils.jwtoken import JWToken
    user = SimpleNamespace(pk=-4060, get_auth_key=lambda: "artifact-test-owned-key")
    class UnavailableCache:
        def set(self, *args, **kwargs):
            raise OSError("test-owned unavailable cache")
    for lifetime in (30, admin_portal.SESSION_TTL + 600, -5):
        token = JWToken(access_token_expiry=lifetime).create_access_token(uid=user.pk)
        request = SimpleNamespace(bearer="bearer", auth_token=SimpleNamespace(token=token), user=user)
        assert admin_portal.issue_with_metadata(request, cache_backend=UnavailableCache()) is None, "cache failure issued a usable session"
        grant = admin_portal.issue_with_metadata(request)
        if lifetime < 0:
            assert grant is None, "expired JWT minted a source session"
            continue
        try:
            assert 0 < grant["source_session_expires_in"] <= min(lifetime, admin_portal.SESSION_TTL), "grant exceeded a bound"
            stored = cache.get(admin_portal._cache_key(grant["session_id"]))
            assert stored["source_session_expires_at"] == grant["source_session_expires_at"], "cache deadline disagrees"
            response = HttpResponse()
            admin_portal.set_cookie(response, grant["session_id"], expires_at=grant["source_session_expires_at"])
            age = int(response.cookies[admin_portal.COOKIE_NAME]["max-age"])
            assert 0 < age <= grant["source_session_expires_in"], "cookie outlives grant"
            stored["source_session_expires_at"] = int(time.time()) - 1
            cache.set(admin_portal._cache_key(grant["session_id"]), stored, timeout=60)
            assert admin_portal.validate(SimpleNamespace(COOKIES={admin_portal.COOKIE_NAME: grant["session_id"]})) is None, "cache TTL bypassed explicit expiry"
        finally:
            admin_portal._delete(grant["session_id"])


@th.django_unit_test("gate and legacy share the frozen nonsecret coordination contract")
def test_coordination_contract(opts):
    source = (ROOT / "mojo/apps/account/static/account/admin-source-session.js").read_text()
    for value in ("mojo:admin-source-session:v1", "mojo:admin-source-generation:v1",
                  "navigator.locks.request", "new BroadcastChannel(LOCK)",
                  "sessionStorage", "crypto.randomUUID", "await response.text()"):
        assert value in source, f"missing shared protocol behavior: {value}"
    assert "channel.postMessage({type: 'generation', ...value})" in source, "message schema changed"
    gate = (ROOT / "mojo/apps/account/rest/admin_portal.py").read_text()
    core = (ROOT / "mojo/apps/account/admin_portal/assets/core.js").read_text()
    assert "admin-source-session.js" in gate and "admin-source-session.js" in core, "clients diverged"
    assert "MojoAdminSourceSession.issue" in gate and "MojoAdminSourceSession.issue" in core, "an issuer bypasses coordination"


@th.django_unit_test("hosted auth coordinates only validated local Admin return paths")
def test_hosted_auth_source_scope(opts):
    from django.test import RequestFactory
    from mojo.apps.account.rest.bouncer.views import _auth_context
    from mojo.apps.account.services.admin_portal import ADMIN_PATH
    for destination, expected in ((f"/{ADMIN_PATH}/v2/", True),
                                  (f"/{ADMIN_PATH}/", True),
                                  ("/application/", False),
                                  (f"https://elsewhere.invalid/{ADMIN_PATH}/", False),
                                  (f"//elsewhere.invalid/{ADMIN_PATH}/", False)):
        request = RequestFactory().get("/auth", HTTP_HOST="localhost")
        request.DATA = {"redirect": destination}
        assert _auth_context(request)["admin_source_session"] is expected, f"hosted auth scope incorrect for {destination}"
    source = (ROOT / "mojo/apps/account/templates/account/auth_base.html").read_text()
    assert "{% if admin_source_session %}" in source and "MojoAdminSourceSession.explicitLogin" in source, "fresh-login activation lacks the scoped hook"
