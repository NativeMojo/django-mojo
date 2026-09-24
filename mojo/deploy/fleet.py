#!/usr/bin/env python3
"""Operate a django-mojo fleet from an operator's machine: edit the canonical
django.conf in S3, roll it out one node at a time, and read node/database
health — the things an incident needs to be one command instead of a script.

    python3 -m mojo.deploy.fleet config keys      --env prod
    python3 -m mojo.deploy.fleet config get DATABASES.default.CONN_MAX_AGE --env prod
    python3 -m mojo.deploy.fleet config set DATABASES.default.CONN_MAX_AGE=0 \\
                                            DATABASES.readonly.CONN_MAX_AGE=0 --env prod
    python3 -m mojo.deploy.fleet config versions  --env prod
    python3 -m mojo.deploy.fleet config rollback --version-id <id> --env prod
    python3 -m mojo.deploy.fleet sync             --env prod        # rolling, health-gated
    python3 -m mojo.deploy.fleet nodes status     --env prod
    python3 -m mojo.deploy.fleet db connections   --env prod --minutes 60
    python3 -m mojo.deploy.fleet db instance      --env prod
    python3 -m mojo.deploy.fleet errors           --env prod --since 30m

This is the complement of ``check_setup`` (audits the AWS account) and
``check_node`` (audits one node): those observe, this one operates. Nothing
here imports Django.

Project wiring — two files, both in the project tree:

* ``aws/fleet.json`` (non-secret, committed) names each environment: region,
  config bucket/key/KMS key, bucket owner, node ssh aliases, app root, DB
  instance, service names. See ``FLEET_SCHEMA`` for the keys.
* ``var/django.conf`` (never committed) may carry ``AWS_KEY``/``AWS_SECRET``.
  When both are present they are the credential — only a super-admin's
  checkout has them. Otherwise ``--profile`` or the ambient boto3 chain is
  used. Credential values are never printed, logged, or written anywhere.

Mutating commands are exactly ``config set``, ``config unset``, ``config rollback`` and
``sync``. Everything else is read-only and safe mid-incident.

``config set`` is surgical: it downloads the live object, verifies it against
its own sha256 metadata, changes only the named paths, re-renders only those
top-level lines, proves every other key is byte-for-byte unchanged, prints a
redacted diff, keeps a mode-0600 rollback copy under ``var/fleet/<env>/``, and
publishes with the sha256 metadata ``config_sync`` requires. It never
re-renders the whole file from a template — a live object may be older than
the project's renderer, and an incident is not the time to ship that drift.

``sync`` never lets two nodes restart together: it holds every node's
config-sync timer, then per node forces one sync, waits for the new sha to
land and the ASGI service to answer, and only then moves on. (The framework's
hostname jitter is not a rollout strategy: two hostnames can hash 2 seconds
apart.)
"""

import argparse
import ast
import datetime
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time


FLEET_FILE = os.path.join("aws", "fleet.json")
CONF_FILE = os.path.join("var", "django.conf")
ROLLBACK_DIR = os.path.join("var", "fleet")
SECRET_KEY_RE = re.compile(r"(PASSWORD|SECRET|TOKEN|CREDENTIAL|PRIVATE|AUTH|_KEY$|^KEY$)", re.I)
REDACTED = "<redacted>"
MISSING = object()

FLEET_SCHEMA = {
    "region": "AWS region of the environment",
    "config_bucket": "S3 bucket holding the canonical django.conf",
    "config_key": "S3 key of the canonical django.conf",
    "config_kms_key_arn": "KMS key used for SSE-KMS on publish",
    "bucket_owner": "AWS account id expected to own the bucket",
    "nodes": "ordered list of ssh aliases (BatchMode) — rollout order",
    "app_root": "project path on the node, e.g. /opt/api",
    "api_host": "Host header for the loopback health probe",
}
FLEET_OPTIONAL = {
    "health_path": "/api/version",
    "asgi_service": "mojo-asgi.service",
    "sync_service": "config-sync.service",
    "sync_timer": "config-sync.timer",
    "manage_user": "www",
    "db_instance": None,
    "access_log": "var/edge/log/access.log",
    "error_log": "var/logs/error.log",
    "node_conf": "var/django.conf",
}
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]


class FleetError(Exception):
    pass


# ---------------------------------------------------------------------------
# project wiring
# ---------------------------------------------------------------------------

def read_config(path):
    """Parse a key=value conf. Comments and bad lines are skipped."""
    config = {}
    try:
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return {}
    return config


def load_fleet(project, env):
    path = os.path.join(project, FLEET_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except OSError:
        raise FleetError(f"no fleet file at {path}")
    except ValueError as err:
        raise FleetError(f"{path} is not valid JSON: {err}")
    environments = document.get("environments")
    if not isinstance(environments, dict) or not environments:
        raise FleetError(f"{path} must define a non-empty 'environments' object")
    env = env or document.get("default_env")
    if env not in environments:
        raise FleetError(f"unknown environment {env!r}; known: {', '.join(sorted(environments))}")
    spec = dict(FLEET_OPTIONAL)
    spec.update(environments[env])
    missing = [key for key in FLEET_SCHEMA if not spec.get(key)]
    if missing:
        raise FleetError(f"{path} environment {env!r} is missing: {', '.join(missing)}")
    if not isinstance(spec["nodes"], list) or not all(isinstance(n, str) for n in spec["nodes"]):
        raise FleetError(f"{path} environment {env!r}: 'nodes' must be a list of ssh aliases")
    spec["name"] = env
    return spec


def build_session(project, spec, profile=None, *, session_class=None):
    """boto3 session: --profile, else AWS_KEY/AWS_SECRET from var/django.conf,
    else the ambient chain. Only a super-admin checkout carries the pair.
    ``session_class`` is a test seam for boto3.Session."""
    if session_class is None:
        import boto3
        session_class = boto3.Session

    region = spec["region"]
    if profile:
        return session_class(profile_name=profile, region_name=region)
    config = read_config(os.path.join(project, CONF_FILE))
    key, secret = config.get("AWS_KEY"), config.get("AWS_SECRET")
    if key and secret:
        return session_class(aws_access_key_id=key, aws_secret_access_key=secret,
                             region_name=region)
    if key or secret:
        print("warning: only one of AWS_KEY/AWS_SECRET is set — using the ambient "
              "credential chain", file=sys.stderr)
    return session_class(region_name=region)


# ---------------------------------------------------------------------------
# canonical django.conf: parse / patch / redact
# ---------------------------------------------------------------------------

def parse_conf(text):
    """Return ([lines], {key: (index, value)}) for a canonical ``KEY = <literal>``
    file. Comments and blank lines are kept verbatim so a patch rewrites only
    the lines it changes. A malformed line is an error, never skipped: this is
    the file the whole fleet boots from."""
    lines = text.split("\n")
    keys = {}
    for index, line in enumerate(lines):
        if not line.strip() or line.startswith("#"):
            continue
        if " = " not in line:
            raise FleetError(f"line {index + 1} is not 'KEY = <literal>'")
        key, literal = line.split(" = ", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise FleetError(f"line {index + 1}: {key!r} is not a settings key")
        if key in keys:
            raise FleetError(f"duplicate key {key}")
        try:
            value = ast.literal_eval(literal)
        except (ValueError, SyntaxError) as err:
            raise FleetError(f"line {index + 1} ({key}): not a Python literal: {err}")
        keys[key] = (index, value)
    return lines, keys


def redact(key, value):
    if SECRET_KEY_RE.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: redact(str(k), v) for k, v in value.items()}
    return value


def split_path(path):
    parts = path.split(".")
    if not parts[0] or not re.fullmatch(r"[A-Z][A-Z0-9_]*", parts[0]):
        raise FleetError(f"{path!r}: first segment must be a settings key")
    return parts[0], parts[1:]


def get_path(value, parts):
    for part in parts:
        if not isinstance(value, dict) or part not in value:
            raise FleetError(f"path segment {part!r} not found")
        value = value[part]
    return value


def set_path(value, parts, new):
    """Return a copy of ``value`` with ``parts`` set to ``new``. Intermediate
    segments must already exist as dicts — a typo must not create a key."""
    if not parts:
        return new
    if not isinstance(value, dict) or parts[0] not in value:
        raise FleetError(f"path segment {parts[0]!r} not found")
    copy = dict(value)
    copy[parts[0]] = set_path(value[parts[0]], parts[1:], new)
    return copy


def parse_assignment(text):
    if "=" not in text:
        raise FleetError(f"{text!r}: expected KEY[.path]=<python literal>")
    path, literal = text.split("=", 1)
    path = path.strip()
    split_path(path)
    if literal.startswith("@"):
        try:
            with open(literal[1:], encoding="utf-8", newline="") as handle:
                return path, handle.read()
        except (OSError, UnicodeError):
            raise FleetError(f"{path}: cannot read value file as UTF-8") from None
    try:
        value = ast.literal_eval(literal)
    except (ValueError, SyntaxError):
        raise FleetError(f"{path}: value is not a Python literal") from None
    return path, value


def redact_path(path, value):
    if any(SECRET_KEY_RE.search(part) for part in path.split(".")):
        return REDACTED
    return redact(path, value)


def _render_changes(text, new_values, removed=()):
    """Render only touched lines, then prove the key set and untouched bytes."""
    lines, keys = parse_conf(text)
    # Keep line terminators (including CRLF), comments and final blank lines.
    chunks = [line + "\n" for line in lines[:-1]] + [lines[-1]]
    touched = set(new_values) | set(removed)
    by_index = {index: key for key, (index, _) in keys.items()}
    result = []
    for index, chunk in enumerate(chunks):
        key = by_index.get(index)
        if key in removed:
            continue
        if key in new_values:
            ending = "\r\n" if chunk.endswith("\r\n") else "\n" if chunk.endswith("\n") else ""
            chunk = f"{key} = {new_values[key]!r}{ending}"
        result.append(chunk)
    new_text = "".join(result)
    additions = [key for key in new_values if key not in keys]
    newline = "\r\n" if "\r\n" in text else "\n"
    separator = newline if additions and new_text and not new_text.endswith("\n") else ""
    new_text += separator
    for key in additions:
        new_text += f"{key} = {new_values[key]!r}{newline}"

    after_lines, after = parse_conf(new_text)
    if set(after) != (set(keys) | set(new_values)) - set(removed):
        raise FleetError("patch changed unexpected keys; refusing")
    for key, value in new_values.items():
        if after[key][1] != value:
            raise FleetError(f"{key} did not round-trip through repr; refusing")
    before_indexes = {keys[key][0] for key in touched if key in keys}
    after_indexes = {after[key][0] for key in touched if key in after}
    before_untouched = "".join(chunk for i, chunk in enumerate(chunks) if i not in before_indexes)
    # Appending to an unterminated final line needs one separator; its content
    # still stays byte-identical. No other untouched bytes may change.
    if separator and len(chunks) - 1 not in before_indexes:
        before_untouched += separator
    after_chunks = [line + "\n" for line in after_lines[:-1]] + [after_lines[-1]]
    after_untouched = "".join(chunk for i, chunk in enumerate(after_chunks) if i not in after_indexes)
    if before_untouched != after_untouched:
        raise FleetError("patch changed untouched lines; refusing")
    return new_text


def apply_changes(text, assignments, *, add=False):
    """Apply ``KEY[.path]=<literal>`` assignments to a canonical file.

    Returns (new_text, [(path, old, new)]). Only the top-level lines of the
    touched keys are re-rendered (``KEY = repr(value)``, the renderer's own
    format); every other line is proven byte-identical afterwards."""
    _, keys = parse_conf(text)
    changes = []
    new_values = {}
    for assignment in assignments:
        path, new = parse_assignment(assignment)
        key, parts = split_path(path)
        if key not in keys and key not in new_values:
            if not add:
                raise FleetError(f"{key} is not present in the canonical file; refusing to add keys without --add")
            if parts:
                raise FleetError("--add only creates top-level keys; supply the whole value")
            new_values[key] = new
            changes.append((path, MISSING, new))
            continue
        current = new_values[key] if key in new_values else keys[key][1]
        old = get_path(current, parts)
        if old == new and type(old) is type(new):
            print(f"note: {path} already {redact_path(path, new)!r}", file=sys.stderr)
            continue
        new_values[key] = set_path(current, parts, new)
        changes.append((path, old, new))
    if not changes:
        raise FleetError("nothing to change")
    return _render_changes(text, new_values), changes


def apply_unsets(text, names):
    """Remove explicit top-level keys, never implicitly remove nested paths."""
    _, keys = parse_conf(text)
    removed = {}
    for name in names:
        key, parts = split_path(name)
        if parts:
            raise FleetError("config unset accepts top-level keys only")
        if key not in keys:
            raise FleetError(f"{key} is not present in the canonical file")
        removed[key] = keys[key][1]
    if not removed:
        raise FleetError("nothing to change")
    return _render_changes(text, {}, removed), [(key, value, MISSING) for key, value in removed.items()]


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# S3 canonical object
# ---------------------------------------------------------------------------

def fetch_canonical(s3, spec, version_id=None):
    """(text, version_id, metadata_sha). Refuses a body that does not match
    its own sha256 metadata — that is the integrity contract config_sync
    enforces on the node, so the operator side must not be weaker."""
    kwargs = {"Bucket": spec["config_bucket"], "Key": spec["config_key"],
              "ExpectedBucketOwner": spec["bucket_owner"]}
    if version_id:
        kwargs["VersionId"] = version_id
    response = s3.get_object(**kwargs)
    text = response["Body"].read().decode("utf-8")
    metadata_sha = (response.get("Metadata") or {}).get("sha256")
    if metadata_sha and metadata_sha != sha256_text(text):
        raise FleetError("canonical object does not match its sha256 metadata; refusing")
    return text, response.get("VersionId"), metadata_sha


def publish_canonical(s3, spec, text):
    digest = sha256_text(text)
    response = s3.put_object(
        Bucket=spec["config_bucket"], Key=spec["config_key"],
        ExpectedBucketOwner=spec["bucket_owner"],
        Body=text.encode("utf-8"), ContentType="text/plain",
        ServerSideEncryption="aws:kms", SSEKMSKeyId=spec["config_kms_key_arn"],
        Metadata={"sha256": digest})
    return digest, response.get("VersionId")


def save_rollback(project, spec, text, version_id):
    directory = os.path.join(project, ROLLBACK_DIR, spec["name"])
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(directory, f"django.conf.{stamp}.{version_id or 'noversion'}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


# ---------------------------------------------------------------------------
# nodes over ssh
# ---------------------------------------------------------------------------

def ssh(node, script, timeout=90, runner=None):
    """Run a bash script on a node. Returns (returncode, stdout, stderr)."""
    runner = runner or subprocess.run
    done = runner(["ssh", *SSH_OPTS, node, "bash", "-s"], input=script,
                  capture_output=True, text=True, timeout=timeout)
    return done.returncode, done.stdout, done.stderr


def node_status_script(spec, minutes):
    root = spec["app_root"]
    conf = os.path.join(root, spec["node_conf"])
    access = os.path.join(root, spec["access_log"])
    return f"""
set -o pipefail
echo "host=$(hostname)"
echo "now=$(date -u +%H:%M:%S)"
echo "asgi=$(systemctl is-active {shlex.quote(spec['asgi_service'])} 2>/dev/null)"
echo "timer=$(systemctl is-active {shlex.quote(spec['sync_timer'])} 2>/dev/null)"
echo "conf_sha=$(sudo sha256sum {shlex.quote(conf)} 2>/dev/null | cut -c1-64)"
echo "conf_mtime=$(sudo stat -c %y {shlex.quote(conf)} 2>/dev/null | cut -c1-19)"
echo "last_sync=$(sudo journalctl -u {shlex.quote(spec['sync_service'])} --no-pager -n 40 2>/dev/null | grep -E 'config changed|restart|error' | tail -1 | cut -c1-140)"
ps -eo pid,etimes,nlwp,cmd | awk '/uvicorn|spawn_main|jobs.py/ && !/awk/ {{ role="other"; if ($0 ~ /uvicorn/) role="asgi-parent"; if ($0 ~ /spawn_main/) role="asgi-worker"; if ($0 ~ /jobs.py engine/) role="jobs-engine"; if ($0 ~ /jobs.py scheduler/) role="jobs-scheduler"; print "proc=" $1 " " role " " $2 " " $3 }}'
sudo ss -tnp state established '( dport = :5432 )' 2>/dev/null | awk 'NR>1 {{ if (match($0, /pid=[0-9]+/)) print "pg=" substr($0, RSTART+4, RLENGTH-4) }}' | sort | uniq -c | awk '{{ print $2 " " $1 }}'
sudo awk -v since="$(date -u -d '-{int(minutes)} min' +%Y%m%d%H%M)" 'BEGIN {{ split("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec", m, " "); for (i in m) mon[m[i]] = sprintf("%02d", i) }} {{ d=substr($4,2,2); mo=substr($4,5,3); y=substr($4,9,4); hm=substr($4,14,2) substr($4,17,2); t=y mon[mo] d hm; if (t >= since) {{ tot++; if ($9 ~ /^5/) e++ }} }} END {{ printf "http=%d %d\\n", tot+0, e+0 }}' {shlex.quote(access)} 2>/dev/null
"""


def parse_node_status(stdout):
    status = {"procs": {}, "pg": {}}
    for line in stdout.splitlines():
        if line.startswith("proc="):
            pid, role, etimes, threads = line[5:].split()
            status["procs"][pid] = {"role": role, "uptime": int(etimes), "threads": int(threads)}
        elif line.startswith("pg="):
            pid, count = line[3:].split()
            status["pg"][pid] = int(count)
        elif line.startswith("http="):
            total, errors = line[5:].split()
            status["http_total"], status["http_5xx"] = int(total), int(errors)
        elif "=" in line:
            key, value = line.split("=", 1)
            status[key] = value
    return status


def human_uptime(seconds):
    if seconds >= 86400:
        return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 60}m{seconds % 60}s"


def render_node_status(node, status, canonical_sha, minutes):
    drift = ""
    if canonical_sha:
        drift = "in sync" if status.get("conf_sha") == canonical_sha else "DRIFT vs S3"
    print(f"== {node} ({status.get('host', '?')}) {status.get('now', '')} UTC")
    print(f"   {status.get('asgi', '?')}  sync-timer={status.get('timer', '?')}  "
          f"conf {status.get('conf_mtime', '?')} {drift}")
    if status.get("last_sync"):
        print(f"   last sync: {status['last_sync'].split(': ', 1)[-1][:110]}")
    print(f"   http last {minutes}m: {status.get('http_total', 0)} requests, "
          f"{status.get('http_5xx', 0)} x 5xx")
    total = 0
    for pid, proc in sorted(status["procs"].items(), key=lambda kv: kv[1]["role"]):
        sockets = status["pg"].get(pid, 0)
        total += sockets
        print(f"   {proc['role']:<14} pid {pid:<8} up {human_uptime(proc['uptime']):<7} "
              f"threads {proc['threads']:<3} pg-sockets {sockets}")
    stray = {pid: n for pid, n in status["pg"].items() if pid not in status["procs"]}
    for pid, n in stray.items():
        total += n
        print(f"   {'other':<14} pid {pid:<8} pg-sockets {n}")
    print(f"   pg sockets total: {total}")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_config_keys(args, project, spec, session):
    text, version, _ = fetch_canonical(session.client("s3"), spec)
    _, keys = parse_conf(text)
    print(f"# {spec['config_key']} version={version} sha256={sha256_text(text)}")
    for key in keys:
        value = keys[key][1]
        shape = type(value).__name__
        if isinstance(value, dict):
            shape += "{" + ",".join(str(k) for k in value) + "}"
        print(f"{key}  ({shape})")
    return 0


def cmd_config_get(args, project, spec, session):
    text, version, _ = fetch_canonical(session.client("s3"), spec)
    _, keys = parse_conf(text)
    key, parts = split_path(args.path)
    if key not in keys:
        raise FleetError(f"{key} is not in the canonical file")
    value = get_path(keys[key][1], parts)
    leaf = parts[-1] if parts else key
    if not args.reveal:
        value = redact(leaf, value) if not isinstance(value, dict) else redact(key, value)
        if SECRET_KEY_RE.search(key) and not parts:
            value = REDACTED
    print(f"# version={version}")
    print(f"{args.path} = {value!r}")
    return 0


def cmd_config_set(args, project, spec, session):
    s3 = session.client("s3")
    text, version, _ = fetch_canonical(s3, spec)
    new_text, changes = apply_changes(text, args.assignments, add=args.add)
    return publish_config_edit(args, project, spec, s3, text, version, new_text, changes)


def cmd_config_unset(args, project, spec, session):
    s3 = session.client("s3")
    text, version, _ = fetch_canonical(s3, spec)
    new_text, changes = apply_unsets(text, args.keys)
    return publish_config_edit(args, project, spec, s3, text, version, new_text, changes)


def publish_config_edit(args, project, spec, s3, text, version, new_text, changes):
    for path, old, new in changes:
        if old is MISSING:
            print(f"+ {path} = {redact_path(path, new)!r}")
        elif new is MISSING:
            print(f"- {path} = {redact_path(path, old)!r}")
        else:
            print(f"{path}: {redact_path(path, old)!r} -> {redact_path(path, new)!r}")
    rollback = save_rollback(project, spec, text, version)
    print(f"rollback copy: {rollback} (prior version {version})")
    if args.dry_run:
        print(f"dry-run: would publish sha256={sha256_text(new_text)}")
        return 0
    digest, new_version = publish_canonical(s3, spec, new_text)
    print(f"published s3://{spec['config_bucket']}/{spec['config_key']} "
          f"version={new_version} sha256={digest}")
    print("nodes pick it up on their next config-sync tick (~2-3 min) and restart "
          "with hostname jitter — run `sync` for a health-gated rolling restart")
    return 0


def cmd_config_versions(args, project, spec, session):
    s3 = session.client("s3")
    response = s3.list_object_versions(Bucket=spec["config_bucket"], Prefix=spec["config_key"],
                                       ExpectedBucketOwner=spec["bucket_owner"])
    versions = [v for v in response.get("Versions", []) if v["Key"] == spec["config_key"]]
    for entry in sorted(versions, key=lambda v: v["LastModified"], reverse=True)[:args.limit]:
        flag = "latest" if entry.get("IsLatest") else ""
        print(f"{entry['LastModified']:%Y-%m-%d %H:%M:%S}Z  {entry['VersionId']}  "
              f"{entry['Size']:>6}B  {flag}")
    return 0


def cmd_config_rollback(args, project, spec, session):
    s3 = session.client("s3")
    current, version, _ = fetch_canonical(s3, spec)
    target, _, _ = fetch_canonical(s3, spec, version_id=args.version_id)
    if target == current:
        raise FleetError("that version is byte-identical to the current object")
    _, before = parse_conf(current)
    _, after = parse_conf(target)
    for key in sorted(set(before) | set(after)):
        if before.get(key, (None, None))[1] != after.get(key, (None, None))[1]:
            print(f"{key}: changes")
    rollback = save_rollback(project, spec, current, version)
    print(f"rollback copy of the current object: {rollback}")
    if args.dry_run:
        print("dry-run: not publishing")
        return 0
    digest, new_version = publish_canonical(s3, spec, target)
    print(f"published version={new_version} sha256={digest} (content of {args.version_id})")
    return 0


def canonical_sha(session, spec):
    try:
        text, _, _ = fetch_canonical(session.client("s3"), spec)
    except Exception as err:  # noqa: BLE001 — status must still render without S3 access
        print(f"warning: cannot read canonical object ({err.__class__.__name__}); "
              "drift check skipped", file=sys.stderr)
        return None
    return sha256_text(text)


def cmd_nodes_status(args, project, spec, session):
    expected = canonical_sha(session, spec) if not args.no_s3 else None
    script = node_status_script(spec, args.minutes)
    failed = 0
    for node in spec["nodes"]:
        code, out, err = args.ssh(node, script)
        if code != 0 and not out:
            failed += 1
            print(f"== {node}: ssh failed ({code}): {err.strip()[:200]}")
            continue
        render_node_status(node, parse_node_status(out), expected, args.minutes)
    return 1 if failed else 0


def _wait_for(description, probe, timeout, sleep, interval=5):
    deadline = time.monotonic() + timeout
    while True:
        result = probe()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise FleetError(f"timed out waiting for {description}")
        sleep(interval)


def cmd_sync(args, project, spec, session):
    """Rolling: hold every node's timer, then per node force one sync, wait for
    the canonical sha to land and the ASGI service to answer, restore its
    timer, move on. A failed gate stops the roll with the remaining timers
    still held, so the fleet cannot restart itself behind your back."""
    expected = canonical_sha(session, spec)
    if not expected:
        raise FleetError("cannot read the canonical object; refusing to roll")
    nodes = args.nodes or spec["nodes"]
    conf = os.path.join(spec["app_root"], spec["node_conf"])
    timer, service, asgi = spec["sync_timer"], spec["sync_service"], spec["asgi_service"]
    health = (f"curl -sk -o /dev/null -w '%{{http_code}}' -H 'Host: {spec['api_host']}' "
              f"https://127.0.0.1{spec['health_path']}")
    run, sleep = args.ssh, args.sleep
    held = []
    try:
        for node in nodes:
            code, _, err = run(node, f"sudo systemctl stop {shlex.quote(timer)}")
            if code != 0:
                raise FleetError(f"{node}: cannot hold {timer}: {err.strip()[:160]}")
            held.append(node)
        print(f"held {timer} on {', '.join(held)}")
        for node in nodes:
            print(f"== {node}: syncing")
            code, out, err = run(node, f"sudo systemctl start {shlex.quote(service)}; "
                                       f"sudo sha256sum {shlex.quote(conf)} | cut -c1-64")
            if code != 0:
                raise FleetError(f"{node}: {service} failed: {err.strip()[:160]}")
            if out.strip().splitlines()[-1] != expected:
                raise FleetError(f"{node}: node conf sha does not match the canonical object after sync")
            print(f"   conf in sync ({expected[:12]}…); waiting for {asgi} restart + health")
            sleep(args.settle)

            def probe(node=node):
                code, out, _ = run(node, f"systemctl is-active {shlex.quote(asgi)}; "
                                         f"ps -o etimes= -C uvicorn | sort -n | head -1; {health}")
                parts = out.split()
                if len(parts) < 3 or parts[0] != "active":
                    return None
                try:
                    uptime = int(parts[1])
                except ValueError:
                    return None
                return parts if parts[2] == "200" and uptime < args.settle + args.timeout else None

            result = _wait_for(f"{node} healthy after restart", probe, args.timeout, sleep)
            print(f"   {asgi} active, uptime {result[1]}s, {spec['health_path']} -> {result[2]}")
            run(node, f"sudo systemctl start {shlex.quote(timer)}")
            held.remove(node)
            print(f"   {timer} restored")
    finally:
        if held:
            print(f"WARNING: {timer} still held on {', '.join(held)} — restore by hand once "
                  "the fleet is verified: sudo systemctl start " + timer, file=sys.stderr)
    return 0


def cmd_db_connections(args, project, spec, session):
    if spec.get("db_instance"):
        cw = session.client("cloudwatch")
        end = datetime.datetime.now(datetime.timezone.utc)
        start = end - datetime.timedelta(minutes=args.minutes)
        stats = cw.get_metric_statistics(
            Namespace="AWS/RDS", MetricName="DatabaseConnections",
            Dimensions=[{"Name": "DBInstanceIdentifier", "Value": spec["db_instance"]}],
            StartTime=start, EndTime=end, Period=60, Statistics=["Maximum"])
        points = sorted(stats.get("Datapoints", []), key=lambda p: p["Timestamp"])
        for point in points:  # boto3 hands back tz-local datetimes; the fleet speaks UTC
            point["Timestamp"] = point["Timestamp"].astimezone(datetime.timezone.utc)
        if points:
            peak = max(points, key=lambda p: p["Maximum"])
            print(f"CloudWatch DatabaseConnections last {args.minutes}m: "
                  f"now {points[-1]['Maximum']:.0f}, peak {peak['Maximum']:.0f} "
                  f"at {peak['Timestamp']:%H:%M}Z")
            tail = points[-12:]
            print("   " + " ".join(f"{p['Timestamp']:%H:%M}={p['Maximum']:.0f}" for p in tail))
    node = spec["nodes"][0]
    manage = os.path.join(spec["app_root"], "bin", "manage.py")
    query = (
        "from django.db import connection\n"
        "c = connection.cursor()\n"
        "c.execute(\"show max_connections\"); print('max_connections', c.fetchone()[0])\n"
        "c.execute(\"select count(*) from pg_stat_activity where backend_type='client backend'\")\n"
        "print('client_backends', c.fetchone()[0])\n"
        "c.execute(\"select coalesce(client_addr::text,'<local>'), coalesce(state,'-'), count(*),"
        " count(*) filter (where state='idle' and now()-state_change > interval '5 min')"
        " from pg_stat_activity where backend_type='client backend' group by 1,2 order by 3 desc\")\n"
        "for addr, state, n, stale in c.fetchall(): print(f'{addr:<18} {state:<22} {n:>4}  idle>5m {stale}')\n"
    )
    script = (f"cd {shlex.quote(spec['app_root'])} && sudo -u {shlex.quote(spec['manage_user'])} "
              f"python3 {shlex.quote(manage)} shell <<'PY' 2>/dev/null | grep -v 'objects imported'\n"
              f"{query}PY\n")
    code, out, err = args.ssh(node, script)
    if code != 0 and not out.strip():
        raise FleetError(f"{node}: pg_stat_activity query failed: {err.strip()[:200]}")
    print(f"pg_stat_activity via {node}:")
    for line in out.strip().splitlines():
        print("   " + line)
    return 0


def cmd_db_instance(args, project, spec, session):
    if not spec.get("db_instance"):
        raise FleetError("fleet.json has no db_instance for this environment")
    rds = session.client("rds")
    instance = rds.describe_db_instances(DBInstanceIdentifier=spec["db_instance"])["DBInstances"][0]
    group = instance["DBParameterGroups"][0]["DBParameterGroupName"]
    print(f"{spec['db_instance']}: {instance['DBInstanceClass']} {instance['Engine']} "
          f"{instance['EngineVersion']}  status={instance['DBInstanceStatus']}  "
          f"param-group={group}")
    paginator = rds.get_paginator("describe_db_parameters")
    for page in paginator.paginate(DBParameterGroupName=group):
        for param in page["Parameters"]:
            if param["ParameterName"] in ("max_connections", "idle_session_timeout",
                                          "idle_in_transaction_session_timeout"):
                print(f"   {param['ParameterName']} = {param.get('ParameterValue', '<engine default>')}"
                      f"  ({param.get('Source', '?')})")
    return 0


def parse_since(text):
    match = re.fullmatch(r"(\d+)([mhd])", text.strip())
    if not match:
        raise FleetError(f"--since must look like 30m, 2h or 1d, not {text!r}")
    amount, unit = int(match.group(1)), match.group(2)
    return amount * {"m": 1, "h": 60, "d": 1440}[unit]


def cmd_errors(args, project, spec, session):
    minutes = parse_since(args.since)
    path = os.path.join(spec["app_root"], spec["error_log"])
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")
    script = (f"sudo awk -v s={shlex.quote(since)} 'substr($0,1,16) >= s' {shlex.quote(path)} "
              f"| grep -E 'ERROR|Error|Exception|FATAL' "
              f"| sed -E 's/^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}} [0-9:,]+ - //; s/^ +//; s/[0-9]+/N/g' "
              f"| cut -c1-140 | sort | uniq -c | sort -rn | head -{args.top}")
    for node in spec["nodes"]:
        code, out, err = args.ssh(node, script)
        print(f"== {node} error signatures since {since}Z")
        if code != 0 and not out.strip():
            print(f"   ssh failed: {err.strip()[:160]}")
            continue
        print(out.rstrip() or "   (none)")
    return 0


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog="mojo.deploy.fleet",
                                     description=__doc__.split("\n\n")[0])
    # Shared options live on every leaf command so they can be given after the
    # subcommand (`fleet config keys --env prod`), which is how people type them.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default=os.getcwd(), help="project root (default: cwd)")
    common.add_argument("--env", help="environment from aws/fleet.json (default: default_env)")
    common.add_argument("--profile", help="AWS profile instead of var/django.conf credentials")
    sub = parser.add_subparsers(dest="command", required=True)

    def leaf(group, name, **kwargs):
        return group.add_parser(name, parents=[common], **kwargs)

    config = sub.add_parser("config", help="canonical django.conf in S3").add_subparsers(
        dest="action", required=True)
    leaf(config, "keys", help="list keys (no values)").set_defaults(func=cmd_config_keys)
    get = leaf(config, "get", help="print one key or dotted path, redacted")
    get.add_argument("path")
    get.add_argument("--reveal", action="store_true", help="print secret values")
    get.set_defaults(func=cmd_config_get)
    setp = leaf(config, "set", help="KEY[.path]=<literal> ... (surgical, publishes)")
    setp.add_argument("assignments", nargs="+")
    setp.add_argument("--add", action="store_true", help="allow new top-level keys; values may use @file")
    setp.add_argument("--dry-run", action="store_true")
    setp.set_defaults(func=cmd_config_set)
    unset = leaf(config, "unset", help="remove named top-level settings (publishes)")
    unset.add_argument("keys", nargs="+")
    unset.add_argument("--dry-run", action="store_true")
    unset.set_defaults(func=cmd_config_unset)
    versions = leaf(config, "versions", help="list object versions")
    versions.add_argument("--limit", type=int, default=15)
    versions.set_defaults(func=cmd_config_versions)
    rollback = leaf(config, "rollback", help="republish an earlier version's content")
    rollback.add_argument("--version-id", required=True)
    rollback.add_argument("--dry-run", action="store_true")
    rollback.set_defaults(func=cmd_config_rollback)

    sync = leaf(sub, "sync", help="health-gated rolling config-sync across nodes")
    sync.add_argument("--nodes", nargs="*", help="subset/order override")
    sync.add_argument("--settle", type=int, default=70,
                      help="seconds to wait for the jittered restart before probing")
    sync.add_argument("--timeout", type=int, default=120, help="health gate timeout per node")
    sync.set_defaults(func=cmd_sync)

    nodes = sub.add_parser("nodes", help="node health").add_subparsers(dest="action", required=True)
    status = leaf(nodes, "status")
    status.add_argument("--minutes", type=int, default=5, help="access-log window")
    status.add_argument("--no-s3", action="store_true", help="skip the drift check")
    status.set_defaults(func=cmd_nodes_status)

    db = sub.add_parser("db", help="database health").add_subparsers(dest="action", required=True)
    connections = leaf(db, "connections")
    connections.add_argument("--minutes", type=int, default=60)
    connections.set_defaults(func=cmd_db_connections)
    leaf(db, "instance").set_defaults(func=cmd_db_instance)

    errors = leaf(sub, "errors", help="error.log signatures per node")
    errors.add_argument("--since", default="30m")
    errors.add_argument("--top", type=int, default=15)
    errors.set_defaults(func=cmd_errors)
    return parser


def main(argv=None, *, session_factory=None, ssh_runner=None, sleep=None):
    """``session_factory``, ``ssh_runner`` and ``sleep`` are test seams."""
    args = build_parser().parse_args(argv)
    args.ssh = ssh_runner or ssh
    args.sleep = sleep or time.sleep
    project = os.path.abspath(args.project)
    try:
        spec = load_fleet(project, args.env)
        session = (session_factory or build_session)(project, spec, args.profile)
        return args.func(args, project, spec, session) or 0
    except FleetError as err:
        print(f"fleet: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
