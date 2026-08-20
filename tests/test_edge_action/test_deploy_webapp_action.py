import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib import error


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


class ManifestTests(unittest.TestCase):
    def test_manifest_is_sorted_hashed_and_ignores_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "z.txt").write_bytes(b"z")
            (root / "assets").mkdir()
            (root / "assets/a.js").write_bytes(b"hello")
            (root / ".DS_Store").write_bytes(b"ignored")
            (root / "assets/.gitkeep").write_bytes(b"")

            rows = deploy_action.manifest(root)

        self.assertEqual([row["path"] for row in rows], ["assets/a.js", "z.txt"])
        self.assertEqual(rows[0]["size"], 5)
        self.assertEqual(
            rows[0]["sha256"],
            "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")


class DeployTests(unittest.TestCase):
    def _artifact(self, root):
        path = Path(root) / "index.html"
        path.write_bytes(b"hello")

    def test_uploads_completes_and_waits_until_live(self):
        with tempfile.TemporaryDirectory() as temporary:
            self._artifact(temporary)
            client = FakeClient([
                {"status": "deploying", "terminal": False},
                {"status": "live", "terminal": True, "detail": "live on 2"},
            ])
            state = deploy_action.deploy(args(temporary), client, sleep=lambda _: None)

        self.assertEqual(state["status"], "live")
        self.assertEqual(client.uploads[0][1], b"hello")
        self.assertEqual(client.calls[0][2]["version"], "a" * 40)
        self.assertEqual(client.calls[1], (
            "POST", "edge/release/complete", {"release": 7}))

    def _register_body(self, environment):
        """Run one deploy with `environment` as the WHOLE env, and return the
        body the register call was made with.

        `clear=True` matters: this suite may itself be running inside GitHub
        Actions, and a test that inherited the runner's own GITHUB_ACTIONS
        would assert nothing about the gate.
        """
        with tempfile.TemporaryDirectory() as temporary:
            self._artifact(temporary)
            client = FakeClient([{"status": "live", "terminal": True}])
            with mock.patch.dict(os.environ, environment, clear=True):
                deploy_action.deploy(
                    args(temporary), client, sleep=lambda _: None)
        return client.calls[0][2]

    def test_github_actions_marks_the_release_source(self):
        body = self._register_body({"GITHUB_ACTIONS": "true"})
        self.assertEqual(body.get("source"), "github")

    def test_running_the_script_by_hand_sends_no_source(self):
        # No marker at all, rather than a different one: the platform's own
        # default is what labels a hand run, and an absent key is additive.
        self.assertNotIn("source", self._register_body({}))

    def test_a_non_actions_runner_sends_no_source(self):
        # Some CI systems set GITHUB_ACTIONS to other values; only the exact
        # string the GitHub runner sets counts.
        self.assertNotIn("source", self._register_body({"GITHUB_ACTIONS": "false"}))

    def test_existing_verified_release_skips_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            self._artifact(temporary)
            client = FakeClient([{"status": "live", "terminal": True}], uploads=False)
            deploy_action.deploy(args(temporary), client, sleep=lambda _: None)
        self.assertEqual(client.uploads, [])

    def test_rolled_back_reports_runner_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            self._artifact(temporary)
            client = FakeClient([{
                "status": "rolled_back",
                "terminal": True,
                "detail": "fleet deployment failed",
                "targets": [{
                    "runner": "edge-2", "status": "failed", "error": "nginx failed",
                }],
            }], uploads=False)
            with self.assertRaisesRegex(
                    deploy_action.DeployError, "edge-2: nginx failed"):
                deploy_action.deploy(args(temporary), client, sleep=lambda _: None)

    def test_timeout_is_a_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            self._artifact(temporary)
            client = FakeClient([{"status": "deploying", "terminal": False}], uploads=False)
            moments = iter([0, 31])
            with self.assertRaisesRegex(deploy_action.DeployError, "within 30 seconds"):
                deploy_action.deploy(
                    args(temporary), client, sleep=lambda _: None,
                    clock=lambda: next(moments))

    def test_missing_platform_deployment_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            self._artifact(temporary)
            client = FakeClient([], uploads=False)
            original = client.json

            def no_deployment(method, path, body=None):
                if path == "edge/release/complete":
                    return {"release": 7, "deployment": None}
                return original(method, path, body)

            client.json = no_deployment
            with self.assertRaisesRegex(deploy_action.DeployError, "did not start"):
                deploy_action.deploy(args(temporary), client)


class TransportTests(unittest.TestCase):
    def test_presigned_upload_retries_once(self):
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
        self.assertEqual(opened.call_count, 2)

    def test_main_redacts_key_from_errors(self):
        secret = "top-secret-deploy-key"
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, "index.html").write_text("hello", encoding="utf-8")
            with mock.patch.dict(os.environ, {"MOJO_DEPLOY_KEY": secret}, clear=True), \
                    mock.patch.object(
                        deploy_action, "deploy",
                        side_effect=RuntimeError(f"request leaked {secret}")), \
                    contextlib.redirect_stderr(stderr):
                result = deploy_action.main([
                    "--api-url", "https://api.example.com",
                    "--webapp-id", "42",
                    "--artifact-dir", temporary,
                    "--version", "abc",
                ])
        self.assertEqual(result, 1)
        self.assertNotIn(secret, stderr.getvalue())
        self.assertIn("***", stderr.getvalue())


class ActionMetadataTests(unittest.TestCase):
    def test_action_keeps_key_in_environment_only(self):
        body = (ACTION_DIR / "action.yml").read_text(encoding="utf-8")
        self.assertNotIn("deploy-key:", body)
        self.assertIn('${MOJO_DEPLOY_KEY:-}', body)
        self.assertIn("::add-mask::${MOJO_DEPLOY_KEY}", body)
        self.assertIn("python3 \"${GITHUB_ACTION_PATH}/deploy.py\"", body)


if __name__ == "__main__":
    unittest.main()
