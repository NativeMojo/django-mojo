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

import json
import secrets as randomness
import string

from mojo.deploy.provision import report
from mojo.deploy.provision import spec as spec_module


BUCKET_STEP = "config_bucket"
SECRETS_STEP = "secrets"
STAGE1_STEP = "stage1_payload"

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
