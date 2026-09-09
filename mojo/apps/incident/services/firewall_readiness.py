"""Local firewall authority proof, independent of the optional host sensor."""
import json
import os
import pwd
import selectors
import subprocess
import time

from .firewall_truth import FirewallTruthError, permanent_set_name

STATUS_ARGV = ("/usr/bin/sudo", "-n", "--", "/usr/local/sbin/mojo-firewall-broker")
MAX_STATUS_BYTES = 4096
STATUS_TIMEOUT = 3.0
TRANSIENT_CODES = frozenset(("broker_timeout", "broker_start_failed", "host_busy"))


def _transport():
    """Drain at most one bounded response; stderr cannot consume memory."""
    process = subprocess.Popen(STATUS_ARGV, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        process.stdin.write(b'{"operation":"broker.status"}\n')
        process.stdin.close()
        deadline = time.monotonic() + STATUS_TIMEOUT
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise subprocess.TimeoutExpired(STATUS_ARGV, STATUS_TIMEOUT)
                chunk = os.read(process.stdout.fileno(), MAX_STATUS_BYTES + 1 - len(output))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_STATUS_BYTES:
                    return -1, bytes(output)
        return process.wait(timeout=max(0.01, deadline - time.monotonic())), bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate status key")
        result[key] = value
    return result


def probe(*, euid=None, app_uid=None, transport=None, permanent_name=None):
    """Always re-prove the effective OS identity and exact broker response."""
    def failed(code):
        return {"ready": False, "code": code, "transient": code in TRANSIENT_CODES}
    try:
        euid = os.geteuid() if euid is None else euid
        app_uid = pwd.getpwnam("ec2-user").pw_uid if app_uid is None else app_uid
        if euid != app_uid or euid <= 0:
            return failed("broker_wrong_user")
        returncode, payload = (transport or _transport)()
        if not isinstance(payload, bytes) or len(payload) > MAX_STATUS_BYTES:
            return failed("broker_response_overflow")
        value = json.loads(payload, object_pairs_hook=_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError("constant")))
        if returncode or not isinstance(value, dict) or value.get("ok") is not True:
            return failed("broker_unavailable")
        if (set(value) != {"ok", "schema", "version", "permanent_set_name"}
                or value["schema"] != "mojo.firewall.broker"
                or type(value["version"]) is not int or value["version"] != 1):
            return failed("broker_malformed_response")
        expected = permanent_set_name() if permanent_name is None else permanent_name
        if value["permanent_set_name"] != expected:
            return failed("permanent_set_config_mismatch")
        return {"ready": True, "code": "ready", "transient": False}
    except KeyError:
        return failed("broker_wrong_user")
    except subprocess.TimeoutExpired:
        return failed("broker_timeout")
    except OSError:
        return failed("broker_start_failed")
    except (ValueError, TypeError, UnicodeError):
        return failed("broker_malformed_response")


def require_ready():
    result = probe()
    if not result["ready"]:
        raise FirewallTruthError(result["code"], "local firewall broker is not ready")
    return result


def provider(engine):
    if "firewall" not in engine.channels or engine.runner_id not in engine.channels:
        return {"ready": False, "code": "firewall_channels_missing"}
    return probe()


def on_ready(engine):
    from mojo.apps.incident.asyncjobs import on_engine_start
    generation = getattr(engine, "_firewall_readiness_generation", 0)
    result = on_engine_start(engine, recovery=generation)
    engine._firewall_readiness_generation = generation + 1
    return result
