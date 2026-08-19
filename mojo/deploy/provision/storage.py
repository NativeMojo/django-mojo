"""The config bucket, the bootstrap secrets, and the stage-1 payload.

The bucket is where an environment keeps the things a node needs before it can
read anything else — its django.conf, its secrets, its stage-1 parameters. That
makes the secrets object the resume anchor for the whole bootstrap: it is
generated exactly once and read back on every subsequent run, so a second
`apply` reuses the same database password rather than generating a new one and
locking itself out of the cluster it created ten minutes ago.

ONE DELIBERATE EXCEPTION TO THE TAG-AT-CREATION RULE lives in this file.
`CreateBucket` has no tag parameter — S3 simply does not offer one — so the
bucket is tagged by a second call. That is safe here and only here, because a
bucket name is globally unique and this package finds its bucket BY NAME, not by
tag. An interrupted run leaves an untagged bucket that the next run still finds
and still adopts, so the duplicate-resource hazard the tag-at-creation rule
exists to prevent cannot occur. Every resource that IS adopted by tag is tagged
in its create call.
"""

import hashlib
import json
import os
import secrets as randomness
import string
import subprocess

from mojo.deploy.provision import report
from mojo.deploy.provision import spec as spec_module


BUCKET_STEP = "config_bucket"
SECRETS_STEP = "secrets"
STAGE1_STEP = "stage1_payload"
PAYLOAD_STEP = "bootstrap_payload"

# The placeholder the packaged stage1.sh ships with. Substituted here, on the
# operator's machine, so the node installs the SAME django-mojo release that
# provisioned its environment rather than whatever PyPI's `latest` is by the
# time the instance boots.
VERSION_PLACEHOLDER = "@DJANGO_MOJO_VERSION@"

CLOUDWATCH_PLACEHOLDERS = (
    ("@LOG_GROUP_NGINX@", "nginx"),
    ("@LOG_GROUP_APP@", "app"),
    ("@LOG_GROUP_CLOUD_INIT@", "cloud-init"),
)

PYPI_PROJECT = "django-mojo"
PYPI_URL = "https://pypi.org/pypi/%s/%s/json"
PYPI_TIMEOUT = 10

# RDS rejects '/', '"', '@' and spaces in a master password, and ElastiCache is
# fussier still. Alphanumerics at this length are well past the point where
# character-class variety buys anything.
PASSWORD_ALPHABET = string.ascii_letters + string.digits
PASSWORD_LENGTH = 40
SECRET_KEY_LENGTH = 64

STAGE1_VERSION = 1


def ensure_config_bucket(clients, spec, observed, apply=False):
    findings, actions = [], []
    result = report.Result()
    names = spec_module.names(spec)
    bucket = names["config_bucket"]
    s3 = clients.get("s3")
    result.set("config_bucket", bucket)

    if not observed.get("config_bucket"):
        findings.append(report.missing(
            BUCKET_STEP, "bucket.missing", f"bucket {bucket} does not exist",
            "apply creates it with versioning, encryption, a public access "
            "block and a deny-insecure-transport policy"))
        actions.append(report.Action(BUCKET_STEP, "create", bucket))
        if not apply:
            return findings, actions, result

        request = {"Bucket": bucket}
        # us-east-1 is the one region where LocationConstraint must be OMITTED.
        # Sending it there fails with InvalidLocationConstraint; omitting it
        # anywhere else silently creates the bucket in us-east-1.
        if spec.region != "us-east-1":
            request["CreateBucketConfiguration"] = {
                "LocationConstraint": spec.region}
        created = report.safe(findings, BUCKET_STEP, "s3.create_bucket",
                              lambda: s3.create_bucket(**request))
        if created is None:
            return findings, actions, result
        report.safe(
            findings, BUCKET_STEP, "s3.put_bucket_tagging",
            lambda: s3.put_bucket_tagging(
                Bucket=bucket,
                Tagging={"TagSet": spec_module.tag_list(spec, "storage",
                                                        name=bucket)}))
        state = {}
    else:
        findings.append(report.existing(
            BUCKET_STEP, "bucket.ok", f"bucket {bucket} is in place"))
        state = observed.get("config_bucket_state") or {}

    _converge_bucket_settings(s3, spec, bucket, state, BUCKET_STEP,
                              findings, actions, apply)
    return findings, actions, result


def _converge_bucket_settings(s3, spec, bucket, state, step, findings, actions,
                              apply):
    """The four settings that make a bucket safe to keep secrets in.

    All four are modifiable in place on a bucket that already exists, which is
    why they are DRIFT rather than MANUAL — a bucket someone created by hand can
    be brought up to this standard without being replaced.
    """
    if (state or {}).get("versioning") != "Enabled":
        findings.append(report.drift(
            step, "bucket.versioning",
            f"versioning is not enabled on {bucket}",
            "apply enables it — an overwritten secrets object is otherwise "
            "unrecoverable"))
        actions.append(report.Action(step, "modify", bucket, "versioning"))
        if apply:
            report.safe(
                findings, step, "s3.put_bucket_versioning",
                lambda: s3.put_bucket_versioning(
                    Bucket=bucket,
                    VersioningConfiguration={"Status": "Enabled"}))

    if not (state or {}).get("encryption"):
        findings.append(report.drift(
            step, "bucket.encryption",
            f"default encryption is not configured on {bucket}",
            "apply sets SSE-S3 with a bucket key"))
        actions.append(report.Action(step, "modify", bucket, "encryption"))
        if apply:
            report.safe(
                findings, step, "s3.put_bucket_encryption",
                lambda: s3.put_bucket_encryption(
                    Bucket=bucket,
                    ServerSideEncryptionConfiguration={"Rules": [{
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "AES256"},
                        "BucketKeyEnabled": True}]}))

    block = (((state or {}).get("public_access_block") or {})
             .get("PublicAccessBlockConfiguration") or {})
    wanted_block = {"BlockPublicAcls": True, "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True, "RestrictPublicBuckets": True}
    if any(block.get(key) is not True for key in wanted_block):
        findings.append(report.drift(
            step, "bucket.public_access",
            f"the public access block on {bucket} is not fully on",
            "apply turns all four settings on"))
        actions.append(report.Action(step, "modify", bucket,
                                     "public access block"))
        if apply:
            report.safe(
                findings, step, "s3.put_public_access_block",
                lambda: s3.put_public_access_block(
                    Bucket=bucket,
                    PublicAccessBlockConfiguration=dict(wanted_block)))

    if not _has_secure_transport_deny((state or {}).get("policy"), bucket):
        findings.append(report.drift(
            step, "bucket.secure_transport",
            f"{bucket} does not deny plain-HTTP access",
            "apply adds a deny-on-aws:SecureTransport-false statement"))
        actions.append(report.Action(step, "modify", bucket, "bucket policy"))
        if apply:
            report.safe(
                findings, step, "s3.put_bucket_policy",
                lambda: s3.put_bucket_policy(
                    Bucket=bucket,
                    Policy=json.dumps(secure_transport_policy(bucket))))

    if not findings or all(f.status == report.PASS for f in findings):
        findings.append(report.existing(
            step, "bucket.settings.ok",
            f"{bucket} has versioning, encryption, a public access block and a "
            f"transport policy"))


def secure_transport_policy(bucket):
    arn = f"arn:aws:s3:::{bucket}"
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "DenyInsecureTransport",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [arn, f"{arn}/*"],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        }],
    }


def _has_secure_transport_deny(policy, bucket):
    statements = (policy or {}).get("Statement") or []
    if isinstance(statements, dict):
        statements = [statements]
    for statement in statements:
        if statement.get("Effect") != "Deny":
            continue
        condition = ((statement.get("Condition") or {}).get("Bool") or {})
        value = condition.get("aws:SecureTransport")
        if str(value).lower() == "false":
            return True
    return False


# ── secrets ─────────────────────────────────────────────────────────────────

def generate_secrets():
    """New credentials for a brand-new environment.

    Called once, ever, per environment. Everything downstream reads what this
    produced out of the bucket, which is what makes a re-run of `apply` connect
    to the database it built rather than to a password it has just invented.
    """
    return {
        "db_password": _token(PASSWORD_LENGTH),
        "cache_auth_token": _token(PASSWORD_LENGTH),
        "django_secret_key": _token(SECRET_KEY_LENGTH),
    }


def _token(length):
    return "".join(randomness.choice(PASSWORD_ALPHABET) for _ in range(length))


def ensure_secrets(clients, spec, observed, apply=False):
    findings, actions = [], []
    result = report.Result()
    names = spec_module.names(spec)
    bucket = observed.get("config_bucket") or names["config_bucket"]
    key = names["secrets_object"]

    existing = observed.get("secrets")
    if existing:
        findings.append(report.existing(
            SECRETS_STEP, "secrets.ok",
            f"s3://{bucket}/{key} exists and was read back"))
        result.set("secrets", dict(existing))
        return findings, actions, result

    findings.append(report.missing(
        SECRETS_STEP, "secrets.missing",
        f"s3://{bucket}/{key} does not exist",
        "apply generates the database password, cache auth token and Django "
        "secret key once, and every later run reads them back"))
    # The action names the object, never its contents. Nothing in this package
    # puts a secret into a finding, an action or a return value that a renderer
    # might print.
    actions.append(report.Action(SECRETS_STEP, "write", f"s3://{bucket}/{key}"))
    if not apply:
        return findings, actions, result

    generated = generate_secrets()
    s3 = clients.get("s3")
    stored = report.safe(
        findings, SECRETS_STEP, "s3.put_object",
        lambda: s3.put_object(
            Bucket=bucket, Key=key,
            Body=json.dumps(generated, indent=2, sort_keys=True).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256"))
    if stored is not None:
        result.set("secrets", generated)
    return findings, actions, result


# ── stage 1 ─────────────────────────────────────────────────────────────────

def stage1_document(spec, observed, secrets_key):
    """What a node needs to know before it can fetch anything else.

    Endpoints and names only — no credentials. The node reads this, then reads
    the secrets object separately with the instance role, so an operator can
    hand someone this document to debug a boot without handing over the
    database password.
    """
    names = spec_module.names(spec)
    return {
        "version": STAGE1_VERSION,
        "project": spec.project,
        "env": spec.env,
        "region": spec.region,
        "config_bucket": observed.get("config_bucket") or names["config_bucket"],
        "secrets_object": secrets_key,
        "database": {
            "engine": spec_module.DB_ENGINE,
            "host": observed.get("db_endpoint"),
            "reader_host": observed.get("db_reader_endpoint"),
            "port": spec_module.DB_PORT,
            "name": names["db_name"],
            "user": "mojo",
        },
        "cache": {
            "engine": spec_module.CACHE_ENGINE,
            "host": observed.get("cache_endpoint"),
            "port": spec_module.CACHE_PORT,
            # Transit encryption is on, so the client scheme is rediss://.
            # mojo/helpers/redis/client.py already defaults to that.
            "tls": True,
        },
        "log_groups": dict(names["log_groups"]),
        "hostnames": list(names["nodes"]),
    }


def ensure_stage1_payload(clients, spec, observed, apply=False):
    """Write the parameters the node's first boot reads.

    This slice writes the endpoints and names. The node sequence that consumes
    it extends the same object rather than inventing a second one, so there
    stays exactly one place a node looks.
    """
    findings, actions = [], []
    result = report.Result()
    names = spec_module.names(spec)
    bucket = observed.get("config_bucket") or names["config_bucket"]
    key = names["stage1_object"]

    wanted = stage1_document(spec, observed, names["secrets_object"])
    missing_endpoints = [
        label for label, value in (("database", wanted["database"]["host"]),
                                   ("cache", wanted["cache"]["host"]))
        if not value]
    if missing_endpoints:
        # Not an error. Aurora and Valkey take minutes; the payload is written
        # on the run where their endpoints first exist.
        findings.append(report.pending(
            STAGE1_STEP, "stage1.endpoints",
            f"the {' and '.join(missing_endpoints)} endpoint(s) are not "
            f"available yet, so the stage-1 payload cannot be written"))
        return findings, actions, result

    current = observed.get("stage1")
    if current == wanted:
        findings.append(report.existing(
            STAGE1_STEP, "stage1.ok", f"s3://{bucket}/{key} is up to date"))
        result.set("stage1", dict(wanted))
        return findings, actions, result

    findings.append(report.drift(
        STAGE1_STEP, "stage1.drift" if current else "stage1.missing",
        f"s3://{bucket}/{key} "
        f"{'does not match the topology' if current else 'does not exist'}",
        "apply writes it"))
    actions.append(report.Action(STAGE1_STEP, "write", f"s3://{bucket}/{key}"))
    if not apply:
        return findings, actions, result

    s3 = clients.get("s3")
    stored = report.safe(
        findings, STAGE1_STEP, "s3.put_object",
        lambda: s3.put_object(
            Bucket=bucket, Key=key,
            Body=json.dumps(wanted, indent=2, sort_keys=True).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256"))
    if stored is not None:
        result.set("stage1", dict(wanted))
    return findings, actions, result


# ── the boot payload ────────────────────────────────────────────────────────

def scripts_dir():
    """Where the packaged node scripts live.

    Resolved by package path, NOT through `mojo.deploy.__main__`'s `LOCATABLE`
    allowlist. That tuple exists so a project's sudo-executed shim can resolve
    exactly two scripts and nothing else; stage1.sh is downloaded and run by a
    booting node, never located by a shim, so widening the allowlist for it
    would give away the guard for no benefit.
    """
    import mojo.deploy

    return os.path.join(
        os.path.dirname(os.path.abspath(mojo.deploy.__file__)), "scripts")


def django_mojo_version():
    import mojo

    return mojo.__version__


def stage1_script(version=None):
    """The packaged stage1.sh, with the version pin substituted.

    Refuses to hand back an unsubstituted script: the placeholder reaching a
    node means `pip install django-mojo==@DJANGO_MOJO_VERSION@`, which fails
    at boot rather than here.
    """
    version = version or django_mojo_version()
    with open(os.path.join(scripts_dir(), "stage1.sh")) as handle:
        body = handle.read()
    if VERSION_PLACEHOLDER not in body:
        raise ValueError(
            f"the packaged stage1.sh no longer contains {VERSION_PLACEHOLDER} "
            f"— the version pin would silently stop being applied")
    return body.replace(VERSION_PLACEHOLDER, version)


def cloudwatch_agent_config(spec):
    """The packaged agent configuration, pointed at THIS environment's groups.

    The names come from `spec.names()` and nowhere else, so the agent, the log
    groups `observability.py` creates and the IAM grant `identity.py` writes
    cannot drift apart — a test asserts these three substituted values equal
    `spec.names()["log_groups"]`.
    """
    names = spec_module.names(spec)
    with open(os.path.join(scripts_dir(), "cloudwatch-agent.json")) as handle:
        body = handle.read()
    for placeholder, kind in CLOUDWATCH_PLACEHOLDERS:
        body = body.replace(placeholder, names["log_groups"][kind])
    body = body.replace("@METRICS_NAMESPACE@", names["metrics_namespace"])
    return body


def app_archive(project_root="."):
    """`git archive HEAD` of the project, as bytes, plus any warnings.

    A tarball rather than a clone: a fresh node has no deploy key yet, and the
    tree it unpacks is exactly the commit the operator provisioned from.

    A DIRTY WORKTREE WARNS AND DOES NOT BLOCK. `git archive HEAD` ships the
    commit, not the working tree, so uncommitted work simply is not in what the
    node runs — worth saying out loud, not worth refusing over, and it matches
    the warn-only treatment a dirty tree already gets elsewhere in this package.
    """
    warnings = []
    root = project_root or "."

    dirty = subprocess.run(["git", "-C", root, "status", "--porcelain"],
                           capture_output=True, text=True, check=False)
    if dirty.returncode != 0:
        raise ValueError(
            f"{os.path.abspath(root)} is not a git worktree, so there is no "
            f"HEAD to archive — run `apply` from the project this environment "
            f"deploys")
    if dirty.stdout.strip():
        warnings.append(
            "the worktree has uncommitted changes — the node receives HEAD, "
            "not what is on your disk")

    archived = subprocess.run(
        ["git", "-C", root, "archive", "--format=tar.gz", "HEAD"],
        capture_output=True, check=False)
    if archived.returncode != 0:
        raise ValueError(
            f"git archive HEAD failed in {os.path.abspath(root)}: "
            f"{archived.stderr.decode('utf-8', 'replace').strip()}")
    return archived.stdout, warnings


def pypi_has_version(version, timeout=PYPI_TIMEOUT):
    """Is this exact django-mojo version published? True / False / None.

    None means "could not tell" — offline, proxied, PyPI having a bad day —
    and is deliberately distinct from False, because refusing to provision on
    a failed HTTP request would be a network flake standing between an
    operator and their environment.

    Run BEFORE any instance launches. A pin that does not exist fails at
    `pip install` on a node that is already running and already billing, and
    the operator finds out by reading /var/log/mojo-stage1.log over SSH.

    A yanked release still installs from an exact pin (PEP 592), so "published
    once" is the whole question here.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        PYPI_URL % (PYPI_PROJECT, version), method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return 200 <= answer.status < 300
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return False
        return None
    except Exception:
        return None


def published_sha(s3, bucket, key):
    """The sha256 this package recorded when it last wrote the object."""
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except Exception:
        return None
    return (head.get("Metadata") or {}).get("sha256")


def put_object(s3, bucket, key, body, content_type):
    """One published object, with its digest in the metadata.

    `config_sync` reads exactly this metadata key to verify what it downloads,
    and refuses an object it cannot verify when `CONFIG_SYNC_REQUIRE_SHA` is
    on — so every publisher in this package sets it.
    """
    if isinstance(body, str):
        body = body.encode("utf-8")
    return s3.put_object(
        Bucket=bucket, Key=key, Body=body, ContentType=content_type,
        ServerSideEncryption="AES256",
        Metadata={"sha256": hashlib.sha256(body).hexdigest()})


def ensure_bootstrap_payload(clients, spec, observed, apply=False):
    """Everything a node downloads at boot, published before any node exists.

    Three objects under the bucket's `bootstrap/` prefix: the substituted
    stage-1 script, the application tarball and the CloudWatch agent's
    configuration. `nodes` depends on this step and refuses to launch without
    it, which is what keeps the ordering honest — an instance that boots before
    its payload is published spends money doing nothing until someone SSHes in.
    """
    findings, actions = [], []
    result = report.Result()
    names = spec_module.names(spec)
    bucket = observed.get("config_bucket") or names["config_bucket"]
    version = django_mojo_version()
    s3 = clients.get("s3")
    keys = (names["stage1_script_object"], names["cloudwatch_object"],
            names["app_archive_object"])

    if not apply:
        # A preview must stay cheap. Building the tarball and asking PyPI about
        # the version are both real work — a `status` run does them on every
        # invocation otherwise — and neither changes the answer a dry run can
        # give: whether the objects are there. The full check happens on the
        # run that is about to launch something.
        absent = [key for key in keys if published_sha(s3, bucket, key) is None]
        if not absent:
            findings.append(report.existing(
                PAYLOAD_STEP, "payload.ok",
                f"the boot payload is published under s3://{bucket}/"
                f"{names['bootstrap_prefix']}/"))
            return findings, actions, result
        for key in absent:
            findings.append(report.missing(
                PAYLOAD_STEP, "payload.missing",
                f"s3://{bucket}/{key} does not exist",
                "apply publishes it — and checks the version pin against PyPI "
                "— before any node is launched"))
            actions.append(report.Action(PAYLOAD_STEP, "write",
                                         f"s3://{bucket}/{key}"))
        return findings, actions, result

    published = pypi_has_version(version)
    if published is False:
        findings.append(report.manual(
            PAYLOAD_STEP, "payload.version_unpublished",
            f"django-mojo {version} is not on PyPI, and the node's stage 1 "
            f"pins exactly that version",
            "publish this release, or provision from a checkout of one that is "
            "published — nothing is launched while the pin cannot be installed"))
        return findings, actions, result
    if published is None:
        findings.append(report.existing(
            PAYLOAD_STEP, "payload.version_unverified",
            f"could not reach PyPI to confirm django-mojo {version} is "
            f"published — continuing, but a node will fail at `pip install` if "
            f"it is not"))
    else:
        findings.append(report.existing(
            PAYLOAD_STEP, "payload.version",
            f"nodes will pin django-mojo {version}"))

    try:
        script = stage1_script(version)
        agent_config = cloudwatch_agent_config(spec)
        archive, warnings = app_archive(getattr(spec, "project_root", None))
    except ValueError as err:
        findings.append(report.manual(
            PAYLOAD_STEP, "payload.unbuildable",
            f"the boot payload could not be built: {err}",
            "fix the above and re-run — no node is launched without it"))
        return findings, actions, result

    for warning in warnings:
        findings.append(report.drift(
            PAYLOAD_STEP, "payload.worktree", warning,
            "commit the change if the node should have it"))

    objects = (
        (names["stage1_script_object"], script, "text/x-shellscript"),
        (names["cloudwatch_object"], agent_config, "application/json"),
        (names["app_archive_object"], archive, "application/gzip"),
    )

    stale = []
    for key, body, _ in objects:
        payload = body.encode("utf-8") if isinstance(body, str) else body
        digest = hashlib.sha256(payload).hexdigest()
        if published_sha(s3, bucket, key) == digest:
            findings.append(report.existing(
                PAYLOAD_STEP, "payload.ok", f"s3://{bucket}/{key} is current"))
            continue
        stale.append(key)
        findings.append(report.missing(
            PAYLOAD_STEP, "payload.stale",
            f"s3://{bucket}/{key} is missing or out of date",
            "apply publishes it before any node is launched"))
        actions.append(report.Action(PAYLOAD_STEP, "write", f"s3://{bucket}/{key}"))

    for key, body, content_type in objects:
        if key not in stale:
            continue
        stored = report.safe(
            findings, PAYLOAD_STEP, "s3.put_object",
            lambda k=key, b=body, c=content_type: put_object(
                s3, bucket, k, b, c))
        if stored is None:
            return findings, actions, result

    # Only now may a node be launched. `nodes` reads this key and refuses
    # without it, so an unpublished payload can never become a running,
    # billing, half-provisioned instance.
    result.set("bootstrap_payload", {
        "bucket": bucket,
        "version": version,
        "objects": [key for key, _, _ in objects],
    })
    return findings, actions, result


def ensure_storage(clients, spec, observed, apply=False):
    """Bucket, secrets and stage-1 payload, for a caller converging this area
    alone."""
    findings, actions = [], []
    result = report.Result()
    merged = dict(observed or {})
    for step in (ensure_config_bucket, ensure_secrets, ensure_stage1_payload):
        step_findings, step_actions, step_result = step(
            clients, spec, merged, apply)
        findings.extend(step_findings)
        actions.extend(step_actions)
        result.update(**step_result.as_dict())
        merged.update(step_result.as_dict())
    return findings, actions, result
