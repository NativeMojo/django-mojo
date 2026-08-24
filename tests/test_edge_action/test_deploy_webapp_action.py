"""Coverage for the shipped deploy-webapp GitHub Action example.

Converted from unittest.TestCase to testit (maestro #2792): the previous
class-based form was never collected by the testit runner (it collects
module-level ``test_*`` functions, not TestCase methods), so this coverage had
gone silently dead. These are pure in-process unit tests — no server, no DB —
so they run under ``@th.unit_test`` and carry no Django dependency.
"""
import importlib.util
from http.client import RemoteDisconnected
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import mock
from urllib import error

from testit import helpers as th
from testit.helpers import assert_eq, assert_true, assert_in


ROOT = Path(__file__).resolve().parents[2]
ACTION_DIR = ROOT / "examples/github/actions/deploy-webapp"
SPEC = importlib.util.spec_from_file_location(
    "deploy_webapp_action", ACTION_DIR / "deploy.py")
deploy_action = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy_action)


class FakeClient:
    def __init__(self, states, uploads=True):
        self.states = iter(states)
        self.calls = []
        self.uploads = []
        self.include_uploads = uploads

    def json(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "edge/release":
            uploads = []
            if self.include_uploads:
                uploads = [{
                    "path": "index.html",
                    "url": "https://uploads.example/index.html",
                    "headers": {"x-amz-checksum-sha256": "checksum"},
                }]
            return {"release": 7, "status": "pending", "uploads": uploads}
        if path == "edge/release/complete":
            return {"release": 7, "deployment": 12, "deployment_status": "queued"}
        return next(self.states)

    def upload(self, url, path, headers):
        self.uploads.append((url, Path(path).read_bytes(), headers))


def args(directory, timeout=30, poll=0):
    return SimpleNamespace(
        api_url="https://api.example.com",
        webapp_id=42,
        artifact_dir=str(directory),
        version="a" * 40,
        timeout_seconds=timeout,
        poll_seconds=poll,
    )


def _write_artifact(root):
    path = Path(root) / "index.html"
    path.write_bytes(b"hello")


@th.unit_test("deploy action: manifest is sorted, hashed and ignores metadata")
def test_manifest_is_sorted_hashed_and_ignores_metadata(opts):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "z.txt").write_bytes(b"z")
        (root / "assets").mkdir()
        (root / "assets/a.js").write_bytes(b"hello")
        (root / ".DS_Store").write_bytes(b"ignored")
        (root / "assets/.gitkeep").write_bytes(b"")

        rows = deploy_action.manifest(root)

    assert_eq([row["path"] for row in rows], ["assets/a.js", "z.txt"],
              "manifest must be sorted and drop metadata/empty files")
    assert_eq(rows[0]["size"], 5, "assets/a.js size must be 5 bytes")
    assert_eq(
        rows[0]["sha256"],
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
        "assets/a.js sha256 must match the known hash of 'hello'")


@th.unit_test("deploy action: uploads, completes and waits until live")
def test_uploads_completes_and_waits_until_live(opts):
    with tempfile.TemporaryDirectory() as temporary:
        _write_artifact(temporary)
        client = FakeClient([
            {"status": "deploying", "terminal": False},
            {"status": "live", "terminal": True, "detail": "live on 2"},
        ])
        state = deploy_action.deploy(args(temporary), client, sleep=lambda _: None)

    assert_eq(state["status"], "live", "final state must be live")
    assert_eq(client.uploads[0][1], b"hello", "the artifact bytes must be uploaded")
    assert_eq(client.calls[0][2]["version"], "a" * 40,
              "the release call must carry the version")
    assert_eq(client.calls[1], ("POST", "edge/release/complete", {"release": 7}),
              "the second call must complete the release")


@th.unit_test("deploy action: an existing verified release skips upload")
def test_existing_verified_release_skips_upload(opts):
    with tempfile.TemporaryDirectory() as temporary:
        _write_artifact(temporary)
        client = FakeClient([{"status": "live", "terminal": True}], uploads=False)
        deploy_action.deploy(args(temporary), client, sleep=lambda _: None)
    assert_eq(client.uploads, [],
              "a release with no pending uploads must upload nothing")


@th.unit_test("deploy action: a rolled-back deploy reports runner diagnostics")
def test_rolled_back_reports_runner_diagnostics(opts):
    with tempfile.TemporaryDirectory() as temporary:
        _write_artifact(temporary)
        client = FakeClient([{
            "status": "rolled_back",
            "terminal": True,
            "detail": "fleet deployment failed",
            "targets": [{
                "runner": "edge-2", "status": "failed", "error": "nginx failed",
            }],
        }], uploads=False)
        with th.assert_raises(deploy_action.DeployError) as ctx:
            deploy_action.deploy(args(temporary), client, sleep=lambda _: None)
    assert_true("edge-2: nginx failed" in str(ctx.exception),
                f"error must name the failing runner and reason; got {ctx.exception!r}")


@th.unit_test("deploy action: a timeout is a failure")
def test_timeout_is_a_failure(opts):
    with tempfile.TemporaryDirectory() as temporary:
        _write_artifact(temporary)
        client = FakeClient([{"status": "deploying", "terminal": False}], uploads=False)
        moments = iter([0, 31])
        with th.assert_raises(deploy_action.DeployError) as ctx:
            deploy_action.deploy(
                args(temporary), client, sleep=lambda _: None,
                clock=lambda: next(moments))
    assert_true("within 30 seconds" in str(ctx.exception),
                f"timeout error must state the deadline; got {ctx.exception!r}")


@th.unit_test("deploy action: a missing platform deployment is not a success")
def test_missing_platform_deployment_is_not_reported_as_success(opts):
    with tempfile.TemporaryDirectory() as temporary:
        _write_artifact(temporary)
        client = FakeClient([], uploads=False)
        original = client.json

        def no_deployment(method, path, body=None):
            if path == "edge/release/complete":
                return {"release": 7, "deployment": None}
            return original(method, path, body)

        client.json = no_deployment
        with th.assert_raises(deploy_action.DeployError) as ctx:
            deploy_action.deploy(args(temporary), client)
    assert_true("did not start" in str(ctx.exception),
                f"a null deployment must fail, not pass; got {ctx.exception!r}")


@th.unit_test("deploy action: a presigned upload retries once on URLError")
def test_presigned_upload_retries_once(opts):
    response = mock.MagicMock()
    response.__enter__.return_value = response
    client = deploy_action.Client(
        "https://api.example.com", "secret", sleep=lambda _: None)
    with tempfile.NamedTemporaryFile() as artifact:
        artifact.write(b"bytes")
        artifact.flush()
        with mock.patch.object(
                deploy_action.request, "urlopen",
                side_effect=[error.URLError("temporary"), response]) as opened:
            client.upload(
                "https://uploads.example/object", artifact.name,
                {"x-amz-checksum-sha256": "bound"})
    assert_eq(opened.call_count, 2,
              "a transient URLError should reopen and retry the upload once")


@th.unit_test("deploy action: a presigned upload retries a remote disconnect")
def test_presigned_upload_retries_remote_disconnect(opts):
    response = mock.MagicMock()
    response.__enter__.return_value = response
    client = deploy_action.Client(
        "https://api.example.com", "secret", sleep=lambda _: None)
    with tempfile.NamedTemporaryFile() as artifact:
        artifact.write(b"bytes")
        artifact.flush()
        with mock.patch.object(
                deploy_action.request, "urlopen",
                side_effect=[RemoteDisconnected("temporary"), response]) as opened:
            client.upload(
                "https://uploads.example/object", artifact.name,
                {"x-amz-checksum-sha256": "bound"})
    assert_eq(opened.call_count, 2,
              "a remote disconnect should reopen and retry the presigned upload")


@th.unit_test("deploy action: a presigned upload uses bounded backoff")
def test_presigned_upload_uses_bounded_backoff(opts):
    response = mock.MagicMock()
    response.__enter__.return_value = response
    waits = []
    client = deploy_action.Client(
        "https://api.example.com", "secret", sleep=waits.append)
    interruptions = [error.URLError("temporary") for _ in range(6)]
    with tempfile.NamedTemporaryFile() as artifact:
        artifact.write(b"bytes")
        artifact.flush()
        with mock.patch.object(
                deploy_action.request, "urlopen",
                side_effect=interruptions + [response]) as opened:
            client.upload(
                "https://uploads.example/object", artifact.name,
                {"x-amz-checksum-sha256": "bound"})
    assert_eq(opened.call_count, 7,
              "the upload should retain a useful transient-failure retry budget")
    assert_eq(waits, [2, 4, 5, 5, 5, 5],
              "upload retry delays should remain bounded")


@th.unit_test("deploy action: the action keeps the deploy key in the environment only")
def test_action_keeps_key_in_environment_only(opts):
    body = (ACTION_DIR / "action.yml").read_text(encoding="utf-8")
    assert_true("deploy-key:" not in body,
                "action.yml must not accept the deploy key as an input")
    assert_in('${MOJO_DEPLOY_KEY:-}', body,
              "action.yml must read the key from the environment")
    assert_in("::add-mask::${MOJO_DEPLOY_KEY}", body,
              "action.yml must mask the key in the workflow log")
    assert_in("python3 \"${GITHUB_ACTION_PATH}/deploy.py\"", body,
              "action.yml must invoke the shipped deploy.py")


@th.unit_test("deploy action: the example versions every github attempt")
def test_example_versions_every_github_attempt(opts):
    body = (ACTION_DIR / "README.md").read_text(encoding="utf-8")
    assert_in(
        "version: ${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}",
        body,
        "the shipped example must not reuse an immutable release across reruns")
    assert_true(
        "version: ${{ github.sha }}\n" not in body,
        "the shipped example must not retain the commit-only release version")
