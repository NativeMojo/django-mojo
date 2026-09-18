"""Fixed node operations and non-secret evidence for fleet configuration."""

import http.client
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import tempfile
import time
import uuid
from urllib.parse import urlsplit

TARGET = "/opt/api/var/django.conf"
SOCKET_PATH = "/opt/api/var/asgi.sock"
RECEIPT_SUFFIX = ".fleet-config.json"
MAX_RECEIPT = 8192
REVISION = re.compile(r"^[a-f0-9]{32,64}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
PROOF_SALT = "mojo.fleet.config.proof"
TRIGGER_COMMAND = ["sudo", "-n", "/usr/bin/systemctl", "--no-block", "start",
                   "config-sync.service"]
RECEIPT_FIELDS = {"target_revision", "installed_revision", "installed_digest",
                  "status", "error_code", "updated_at", "installed_at",
                  "restart_requested_at", "request_service_supported"}


def _revision(data):
    if (not isinstance(data, dict) or set(data) != {"revision"}
            or not isinstance(data["revision"], str)
            or not REVISION.fullmatch(data["revision"])):
        raise ValueError("invalid fleet revision request")
    return data["revision"]


def read_receipt(target=TARGET):
    """Bounded no-follow read; malformed evidence proves nothing."""
    descriptor = None
    try:
        descriptor = os.open(target + RECEIPT_SUFFIX, os.O_RDONLY |
                             os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_RECEIPT:
            return {}
        raw = os.read(descriptor, MAX_RECEIPT + 1)
        after = os.fstat(descriptor)
        if (len(raw) > MAX_RECEIPT or before.st_size != len(raw)
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns):
            return {}
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) - RECEIPT_FIELDS:
            return {}
        for key in ("target_revision", "installed_revision"):
            if value.get(key) is not None and (
                    not isinstance(value[key], str) or not REVISION.fullmatch(value[key])):
                return {}
        if value.get("installed_digest") is not None and (
                not isinstance(value["installed_digest"], str)
                or not DIGEST.fullmatch(value["installed_digest"])):
            return {}
        if value.get("status") not in {"downloaded", "restart_requested", "failed", "healthy"}:
            return {}
        if value.get("request_service_supported") not in (None, True, False):
            return {}
        if value.get("error_code") not in {None, "sync_failed", "restart_failed",
                "integrity_failed", "download_failed", "override_invalid",
                "document_too_large", "empty_config"}:
            return {}
        for key in ("updated_at", "installed_at", "restart_requested_at"):
            if key in value and (type(value[key]) not in (int, float)
                                 or not 0 < value[key] < 1e12):
                return {}
        return value
    except (OSError, ValueError, UnicodeError):
        return {}
    finally:
        if descriptor is not None:
            os.close(descriptor)


def write_receipt(target, receipt):
    """Replace evidence atomically without ever persisting configuration values."""
    value = {key: item for key, item in receipt.items() if key in RECEIPT_FIELDS}
    value["updated_at"] = time.time()
    raw = json.dumps(value, sort_keys=True, allow_nan=False).encode()
    if len(raw) > MAX_RECEIPT:
        raise ValueError("fleet receipt too large")
    parent = os.path.dirname(os.path.abspath(target))
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".fleet-receipt-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            # Non-secret proof is readable by both the app and job identities.
            os.fchmod(handle.fileno(), 0o644)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target + RECEIPT_SUFFIX)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _supported():
    # The sealed authority is root-only. Root config-sync publishes its result;
    # before the first receipt, a masked/absent API unit is unsupported.
    receipt = read_receipt()
    if receipt.get("request_service_supported") is False:
        return False
    try:
        result = subprocess.run([
            "/usr/bin/systemctl", "show", "mojo-asgi.service",
            "--property=LoadState,UnitFileState"], capture_output=True,
            text=True, timeout=3, check=False)
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        return (result.returncode == 0 and fields.get("LoadState") == "loaded"
                and fields.get("UnitFileState") != "masked")
    except (OSError, subprocess.TimeoutExpired):
        return False


def trigger(data):
    """Request the existing service; never accepts a command or unit name."""
    from django.core import signing

    if not isinstance(data, dict) or set(data) != {"revision", "authorization"}:
        raise ValueError("invalid fleet trigger request")
    revision = _revision({"revision": data["revision"]})
    try:
        if not isinstance(data["authorization"], str) or len(data["authorization"]) > 65536:
            raise ValueError("invalid authorization")
        intent = signing.loads(data["authorization"], salt="mojo.fleet.apply.intent", max_age=300)
        if (not isinstance(intent, dict)
                or set(intent) != {"operation_id", "actor_id", "revision", "nodes"}
                or intent["revision"] != revision
                or type(intent["actor_id"]) is not int or intent["actor_id"] <= 0
                or not isinstance(intent["operation_id"], str)
                or not re.fullmatch(r"[a-f0-9]{32}", intent["operation_id"])
                or not isinstance(intent["nodes"], list) or len(intent["nodes"]) > 128
                or socket.gethostname().lower() not in intent["nodes"]):
            raise ValueError("invalid authorization")
    except (signing.BadSignature, ValueError, TypeError, KeyError):
        return {"revision": revision, "status": "failed", "error_code": "apply_authorization_invalid"}
    if not _supported():
        return {"revision": revision, "status": "unsupported",
                "error_code": "request_service_unsupported"}
    try:
        result = subprocess.run(TRIGGER_COMMAND, capture_output=True,
                                check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        result = None
    accepted = result is not None and result.returncode == 0
    return {"revision": revision, "status": "requested" if accepted else "failed",
            "error_code": None if accepted else "sync_trigger_failed"}


class _UnixHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET_PATH)


def _proof_destination():
    from django.conf import settings
    from mojo.apps.account.services import system_settings
    from mojo.helpers.request import API_ROOT

    origin = system_settings.get_value(system_settings.BASE_URL)
    try:
        parsed = urlsplit(origin or "")
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            return None
        headers = {"Host": parsed.netloc}
        if parsed.scheme == "https":
            # The shipped ASGI unit trusts this header on its local UDS. It
            # supplies the original public scheme; no network request is made.
            headers["X-Forwarded-Proto"] = "https"
            proxy = getattr(settings, "SECURE_PROXY_SSL_HEADER", None)
            if (isinstance(proxy, (tuple, list)) and len(proxy) == 2
                    and isinstance(proxy[0], str) and proxy[0].startswith("HTTP_")):
                headers[proxy[0][5:].replace("_", "-")] = proxy[1]
        return API_ROOT.rstrip("/") + "/account/admin/fleet/proof", headers
    except (ValueError, TypeError):
        return None


def current_digest(target=TARGET):
    """Hash bounded current config without following a symlink or exposing it."""
    descriptor = None
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        limit = 4 * 1024 * 1024
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            return None
        digest = hashlib.sha256()
        total = 0
        while total <= limit:
            block = os.read(descriptor, min(65536, limit + 1 - total))
            if not block:
                break
            total += len(block)
            digest.update(block)
        after = os.fstat(descriptor)
        if (total > limit or total != before.st_size or after.st_size != total
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns):
            return None
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _serving_proof(revision):
    from django.core import signing

    node = socket.gethostname().lower()
    nonce = uuid.uuid4().hex
    challenge = {"revision": revision, "nonce": nonce, "node": node}
    token = signing.dumps(challenge, salt=PROOF_SALT)
    destination = _proof_destination()
    if destination is None:
        return {"error_code": "proof_origin_unconfigured"}
    path, headers = destination
    headers["X-Mojo-Fleet-Proof"] = token
    connection = _UnixHTTPConnection("localhost", timeout=3)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        raw = response.read(MAX_RECEIPT + 1)
        if response.status != 200:
            return {"error_code": "proof_http_rejected"}
        if len(raw) > MAX_RECEIPT:
            return {"error_code": "proof_invalid"}
        value = json.loads(raw)
        if not isinstance(value, dict):
            return {"error_code": "proof_invalid"}
        value = value.get("data", value)
        if (not isinstance(value, dict) or value.get("nonce") != nonce
                or value.get("node") != node
                or type(value.get("pid")) is not int or value["pid"] <= 0):
            return {"error_code": "proof_invalid"}
        if value.get("loaded_revision") != revision:
            return {"error_code": "serving_revision_pending"}
        return value
    except PermissionError:
        return {"error_code": "proof_socket_permission_denied"}
    except (FileNotFoundError, ConnectionRefusedError):
        return {"error_code": "proof_socket_unavailable"}
    except TimeoutError:
        return {"error_code": "proof_timeout"}
    except (OSError, ValueError, http.client.HTTPException):
        return {"error_code": "proof_unavailable"}
    finally:
        connection.close()


def _service_state():
    try:
        result = subprocess.run([
            "/usr/bin/systemctl", "show", "mojo-asgi.service",
            "--property=ActiveState,MainPID,ExecMainStartTimestampMonotonic"],
            capture_output=True, text=True, timeout=3, check=False)
        if result.returncode:
            return {}
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        # systemd monotonic timestamps use CLOCK_MONOTONIC, as does Python.
        started = int(fields.get("ExecMainStartTimestampMonotonic", "0")) / 1e6
        return {"active": fields.get("ActiveState") == "active",
                "pid": int(fields.get("MainPID", "0")),
                "started_at": time.time() - (time.monotonic() - started) if started else 0}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def report(data):
    """Report installation separately from a freshly proven serving revision."""
    revision = _revision(data)
    result = {"revision": revision, "node": socket.gethostname().lower(),
              "installed": False, "restart_requested": False, "restarted": False,
              "healthy": False, "status": "pending", "error_code": None}
    if not _supported():
        result.update(status="unsupported", error_code="request_service_unsupported")
        return result
    receipt = read_receipt()
    result["installed_revision"] = receipt.get("installed_revision")
    matches_revision = receipt.get("installed_revision") == revision
    digest = current_digest() if matches_revision else None
    installed = bool(matches_revision and digest and digest == receipt.get("installed_digest"))
    if matches_revision and not installed:
        result.update(status="failed", error_code=(
            "installed_config_drift" if digest else "installed_config_unreadable"))
    result["installed"] = installed
    if receipt.get("target_revision") in (None, revision) and receipt.get("status") == "failed":
        result.update(status="failed", error_code=receipt.get("error_code") or "sync_failed")
    if not installed:
        return result
    result["restart_requested"] = receipt.get("status") in {"restart_requested", "healthy"}
    if result["status"] != "failed":
        result["status"] = "restart_requested" if result["restart_requested"] else "downloaded"
    state = _service_state()
    if not state:
        result["error_code"] = "service_state_unavailable"
        return result
    if not state.get("active") or not state.get("pid"):
        result["error_code"] = "service_not_active"
        return result
    if (not receipt.get("installed_at")
            or state.get("started_at", 0) < receipt["installed_at"]):
        result["error_code"] = "service_restart_pending"
        return result
    proof = _serving_proof(revision)
    if not proof or proof.get("error_code"):
        result["error_code"] = proof.get("error_code") if proof else "proof_unavailable"
        return result
    result["restarted"] = True
    result["healthy"] = proof.get("healthy") is True
    result["status"] = "healthy" if result["healthy"] else "restarted"
    result["error_code"] = None if result["healthy"] else "dependency_health_failed"
    return result
