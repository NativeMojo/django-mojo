#!/usr/bin/env python3
"""Dependency-free django-mojo WebApp release client for GitHub Actions."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib import error, request


IGNORED_NAMES = {".DS_Store", ".gitkeep", "Thumbs.db"}
TERMINAL_FAILURES = {"failed", "rolled_back", "superseded"}
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class DeployError(RuntimeError):
    pass


def redact(value, secret):
    text = str(value)
    return text.replace(secret, "***") if secret else text


def manifest(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise DeployError(f"artifact directory does not exist: {root}")

    entries = []
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file()
        and path.name not in IGNORED_NAMES
        and ".git" not in path.relative_to(root).parts
    )
    if not files:
        raise DeployError(f"artifact directory contains no deployable files: {root}")

    for path in files:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        entries.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": digest.hexdigest(),
            "size": size,
        })
    return entries


class Client:
    def __init__(self, api_url, token, timeout=60, sleep=time.sleep):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.sleep = sleep

    def _url(self, path):
        return f"{self.api_url}/api/{path.lstrip('/')}"

    def _decode(self, response):
        raw = response.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeployError("platform returned a non-JSON response") from exc
        if isinstance(payload, dict) and payload.get("status") is False:
            raise DeployError(payload.get("error") or "platform request failed")
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload

    def json(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Authorization": f"apikey {self.token}",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(
            self._url(path), data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return self._decode(response)
        except error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
                message = payload.get("error") or payload.get("message") or payload
            except (UnicodeDecodeError, json.JSONDecodeError):
                message = exc.reason
            raise DeployError(f"platform {method} {path} returned {exc.code}: {message}") from exc
        except error.URLError as exc:
            raise DeployError(f"platform {method} {path} failed: {exc.reason}") from exc

    def upload(self, url, path, headers, attempts=2):
        body = Path(path).read_bytes()
        last_error = None
        for attempt in range(1, attempts + 1):
            req = request.Request(url, data=body, headers=headers or {}, method="PUT")
            try:
                with request.urlopen(req, timeout=self.timeout):
                    return
            except error.HTTPError as exc:
                last_error = exc
                if exc.code not in RETRYABLE_STATUS or attempt == attempts:
                    break
            except error.URLError as exc:
                last_error = exc
                if attempt == attempts:
                    break
            self.sleep(min(attempt * 2, 5))
        raise DeployError(f"upload failed for {Path(path).name}: {last_error}")


def diagnostics(deployment):
    lines = []
    for phase in ("targets", "rollback_targets"):
        for target in deployment.get(phase) or []:
            if target.get("status") != "completed" or target.get("error"):
                message = target.get("error") or target.get("status") or "unknown"
                lines.append(f"{phase} {target.get('runner')}: {message}")
    return "; ".join(lines)


def write_output(name, value):
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"{name}={value}\n")


def deploy(args, client, sleep=time.sleep, clock=time.monotonic):
    root = Path(args.artifact_dir).resolve()
    release_manifest = manifest(root)
    registered = client.json("POST", "edge/release", {
        "webapp": int(args.webapp_id),
        "version": args.version,
        "manifest": release_manifest,
    })
    release_id = registered["release"]
    write_output("release", release_id)

    for upload in registered.get("uploads") or []:
        relative = upload["path"]
        print(f"Uploading {relative}")
        client.upload(upload["url"], root / relative, upload.get("headers") or {})

    completed = client.json("POST", "edge/release/complete", {"release": release_id})
    deployment_id = completed.get("deployment")
    if not deployment_id:
        raise DeployError(
            "release verified but no deployment started; this WebApp is on "
            "explicit manual hold (auto_promote=False)")
    write_output("deployment", deployment_id)

    deadline = clock() + float(args.timeout_seconds)
    while True:
        state = client.json("GET", f"edge/release/deployment/{deployment_id}")
        status = state.get("status", "unknown")
        print(f"Deployment {deployment_id}: {status} — {state.get('detail', '')}")
        if status == "live":
            write_output("status", status)
            return state
        if status in TERMINAL_FAILURES or state.get("terminal"):
            write_output("status", status)
            detail = diagnostics(state)
            raise DeployError(
                f"deployment {deployment_id} ended {status}: "
                f"{state.get('detail') or detail or 'no diagnostics'}"
                + (f"; {detail}" if detail and state.get("detail") else ""))
        if clock() >= deadline:
            write_output("status", "timeout")
            raise DeployError(
                f"deployment {deployment_id} did not become live within "
                f"{args.timeout_seconds} seconds")
        sleep(float(args.poll_seconds))


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--api-url", required=True)
    value.add_argument("--webapp-id", required=True, type=int)
    value.add_argument("--artifact-dir", default="dist")
    value.add_argument("--version", required=True)
    value.add_argument("--timeout-seconds", type=float, default=900)
    value.add_argument("--poll-seconds", type=float, default=3)
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    token = os.environ.get("MOJO_DEPLOY_KEY", "").strip()
    if not token:
        print("::error::MOJO_DEPLOY_KEY is required", file=sys.stderr)
        return 2
    try:
        deploy(args, Client(args.api_url, token))
    except Exception as exc:
        print(f"::error::{redact(exc, token)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
