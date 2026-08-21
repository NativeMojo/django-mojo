"""Split out of tests/test_edge_action/test_deploy_webapp_action.py (#1839).

deploy.py reads GITHUB_ACTIONS and MOJO_DEPLOY_KEY from os.environ at call
time with no injectable environment, so these tests patch.dict os.environ
(clear=True) — process-global, and unsafe under the parallel default tier.
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
ACTION_DIR = ROOT / "examples/github/actions/deploy-webapp"
SPEC = importlib.util.spec_from_file_location(
    "deploy_webapp_action_extended", ACTION_DIR / "deploy.py")
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


class DeployTests(unittest.TestCase):
    def _artifact(self, root):
        path = Path(root) / "index.html"
        path.write_bytes(b"hello")

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


class TransportTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
