"""The `django.conf` a provisioned environment runs on, and publishing it.

NOT THE SAME THING AS `python3 -m mojo.deploy render`. That command materializes
this package's cron and systemd TEMPLATES into `var/deploy` on a node that is
already running. This module builds the APPLICATION CONFIG — database
endpoints, cache endpoint, secret key — and publishes it to S3, from an
operator's machine, for `config_sync` to pull down. Same verb, different
artifact, opposite direction: one writes files on a node, this one writes an
object a node reads.

WHAT IS NOT IN THE FILE, and why each absence is deliberate:

    AWS_KEY / AWS_SECRET    The node carries an instance profile. A static key
                            in the config would be a credential sitting in a
                            bucket, on every node's disk, forever — and the
                            whole point of the instance role is that there
                            isn't one.
    REDIS_SCHEME            Left UNSET so `mojo/helpers/redis/client.py`'s
                            `rediss` default holds. The Valkey group is created
                            with transit encryption on, so writing `redis` here
                            would silently break every connection; writing
                            `rediss` would work but duplicate a default that
                            already agrees with us, and duplicated defaults
                            drift.
    INFRASTRUCTURE_MODE     Present, but NEVER a literal. It is whatever
                            `inputs.infrastructure_mode(answers)` returns for
                            the environment file. A run launched with
                            `--override-external` still renders `external`,
                            because the override is a property of one
                            invocation and the file is a property of the
                            environment — a node that came up believing it was
                            managed would keep believing it long after the
                            operator's terminal closed.

The object is published with its sha256 in S3 object metadata, because that is
exactly what `config_sync.remote_details` reads to verify what it downloaded,
and what `CONFIG_SYNC_REQUIRE_SHA` refuses an object for lacking.
"""

import hashlib

from mojo.deploy.provision import inputs, report, storage
from mojo.deploy.provision import spec as spec_module


STEP = "config"

CONF_NAME = "django.conf"

# Aurora's master user, as `data.py` creates it. Restated rather than imported
# so this module can be read on its own; a test asserts the two agree.
DB_USER = "mojo"

HEADER = """\
# django.conf — published by `python3 -m mojo.deploy.provision configure`.
#
# Pulled onto every node by `python3 -m mojo.deploy.config_sync` and installed
# at /opt/api/var/django.conf, 0640, ec2-user:www. DO NOT EDIT IT ON A NODE:
# the next config-sync tick overwrites it, and the node you edited becomes the
# one that behaves differently for reasons nobody can see.
#
# To change a value: change it here (the provisioning environment file or the
# secrets object it derives from), re-run `configure`, and every node picks it
# up within one timer interval.
"""


def _quote(value):
    """A Python string literal, because this file is executed as settings."""
    return '"%s"' % str(value).replace("\\", "\\\\").replace('"', '\\"')


def django_conf(spec, answers, observed, secrets):
    """The complete published config, as text.

    Pure: no AWS calls, no clock, no randomness. The secret key is read from
    the bootstrap secrets object — generated once by `storage.generate_secrets`
    and read back on every later run — so re-rendering does not invalidate
    every session in the fleet.
    """
    names = spec_module.names(spec)
    secrets = secrets or {}
    answers = answers or {}
    apex = answers.get("apex_domain") or spec.domain or ""

    lines = [HEADER]

    def section(title):
        lines.append("")
        lines.append("# ── %s %s" % (title, "─" * max(1, 60 - len(title))))

    def setting(key, value):
        lines.append("%s = %s" % (key, value))

    section("identity")
    setting("SECRET_KEY", _quote(secrets.get("django_secret_key", "")))
    setting("BASE_URL", _quote(f"https://{apex}" if apex else ""))
    setting("EMAIL_FROM", _quote(answers.get("operator_email") or ""))
    setting("GITHUB_REPO", _quote(answers.get("github_repo") or ""))
    # Push-to-deploy's whole trust boundary: GitHub signs each delivery with
    # this and the node verifies X-Hub-Signature-256 against it. Written even
    # when empty so the key's absence from a node is legible as "this estate
    # predates the field" rather than as a rendering bug.
    setting("GITHUB_WEBHOOK_SECRET",
            _quote(secrets.get("github_webhook_secret", "")))

    section("database")
    setting("DATABASE_HOST", _quote(observed.get("db_endpoint") or ""))
    setting("DATABASE_PORT", spec_module.DB_PORT)
    setting("DATABASE_NAME", _quote(names["db_name"]))
    setting("DATABASE_USER", _quote(DB_USER))
    setting("DATABASE_PASSWORD", _quote(secrets.get("db_password", "")))
    if observed.get("db_reader_endpoint"):
        setting("DATABASE_READER_HOST",
                _quote(observed.get("db_reader_endpoint")))

    section("cache")
    # REDIS_SCHEME is deliberately absent — see the module docstring.
    setting("REDIS_SERVER", _quote(observed.get("cache_endpoint") or ""))
    setting("REDIS_PORT", spec_module.CACHE_PORT)
    setting("REDIS_PASSWORD", _quote(secrets.get("cache_auth_token", "")))

    section("aws")
    # No AWS_KEY/AWS_SECRET. The node's instance profile is the credential.
    setting("AWS_REGION", _quote(spec.region))
    setting("AWS_CONFIG_BUCKET",
            _quote(observed.get("config_bucket") or names["config_bucket"]))
    setting("AWS_CONFIG_PREFIX", _quote(names["config_prefix"]))

    section("edge plane")
    # The allowlist a WebApp release is checked against at mint time AND at
    # fetch time. A list literal, not a bare string: `validators.release_buckets`
    # reads it with kind="list".
    setting("EDGE_RELEASE_BUCKETS",
            "[%s]" % _quote(observed.get("releases_bucket")
                            or names["releases_bucket"]))
    # KSMSecrets refuses to load without this, and dnsman's Certificate is a
    # KSMSecrets model — so an edge vhost cannot serve TLS without it.
    setting("KMS_KEY_ID", _quote(observed.get("kms_key_id") or ""))

    section("config plane")
    setting("CONFIG_SYNC_RESTART", "True")
    # The one line in this file that must never be a literal.
    setting("INFRASTRUCTURE_MODE",
            _quote(inputs.infrastructure_mode(answers)))

    lines.append("")
    return "\n".join(lines)


def conf_key(spec):
    """Where the published config lands, and what `config_sync` fetches."""
    return spec_module.names(spec)["django_conf_object"]


def ensure_config(clients, spec, answers, observed, apply=False):
    """Publish `django.conf` for this environment.

    Shaped like an ensure-service (findings, actions, result) even though it is
    driven by `configure` rather than by the DAG, so the CLI renders its
    findings through exactly the same path as everything else.
    """
    findings, actions = [], []
    result = report.Result()
    names = spec_module.names(spec)
    bucket = observed.get("config_bucket") or names["config_bucket"]
    key = conf_key(spec)
    secrets = observed.get("secrets") or {}

    missing = [label for label, value in (
        ("the database endpoint", observed.get("db_endpoint")),
        ("the cache endpoint", observed.get("cache_endpoint")),
        ("the bootstrap secrets", secrets.get("django_secret_key")),
    ) if not value]
    if missing:
        findings.append(report.pending(
            STEP, "config.incomplete",
            f"{', '.join(missing)} are not available yet, so django.conf "
            f"cannot be published"))
        return findings, actions, result

    body = django_conf(spec, answers, observed, secrets)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

    s3 = clients.get("s3")
    if storage.published_sha(s3, bucket, key) == digest:
        findings.append(report.existing(
            STEP, "config.ok", f"s3://{bucket}/{key} is current"))
        result.set("django_conf", {"bucket": bucket, "key": key,
                                   "sha256": digest})
        return findings, actions, result

    findings.append(report.missing(
        STEP, "config.stale",
        f"s3://{bucket}/{key} is missing or out of date",
        "configure publishes it; every node pulls it on the next config-sync "
        "tick"))
    actions.append(report.Action(STEP, "write", f"s3://{bucket}/{key}"))
    if not apply:
        return findings, actions, result

    stored = report.safe(
        findings, STEP, "s3.put_object",
        lambda: storage.put_object(s3, bucket, key, body, "text/plain"))
    if stored is not None:
        result.set("django_conf", {"bucket": bucket, "key": key,
                                   "sha256": digest})
    return findings, actions, result
