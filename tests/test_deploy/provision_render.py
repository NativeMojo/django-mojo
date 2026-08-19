"""The published `django.conf`, and the two cross-item contracts it carries.

`render.django_conf()` is pure — no AWS, no clock, no randomness — so almost
everything here is an assertion about a string. That is the point: this file
is what a node runs on, and the failures it can carry are all silent ones. A
missing DATABASE_HOST is loud; `INFRASTRUCTURE_MODE = "managed"` written into
an environment whose file says `external` is not, and neither is a stray
AWS_KEY.

THE TWO ASSERTIONS THAT SPAN ITEMS:

    INFRASTRUCTURE_MODE must come from `inputs.infrastructure_mode(answers)`
    and never be a literal. The `external` case is tested explicitly, because
    a hardcoded "managed" passes every other test in this file.

    The substituted CloudWatch agent template's three log-group names must
    EQUAL `spec.names()["log_groups"]` — the same dict the log groups are
    created from and the node role's grant is scoped to. Written by hand in
    the template, they would drift, and the symptom would be a node quietly
    logging nowhere.
"""

import json

from testit import helpers as th


REGION = "us-west-2"
PROJECT = "demo"
ENV = "prod"

ANSWERS = {
    "project": PROJECT,
    "env": ENV,
    "region": REGION,
    "apex_domain": "example.com",
    "operator_email": "ops@example.com",
    "preset": "micro",
    "github_repo": "acme/demo",
}

SECRETS = {
    "db_password": "dbpassword0123456789",
    "cache_auth_token": "cachetoken0123456789",
    "django_secret_key": "secretkey0123456789",
}


def _spec(**overrides):
    from mojo.deploy.provision import spec as spec_module

    return spec_module.build(PROJECT, ENV, REGION,
                             preset=overrides.pop("preset", "micro"),
                             domain="example.com", **overrides)


def _observed(**overrides):
    from mojo.deploy.provision import discover

    observed = discover.blank()
    observed.account_id = "123456789012"
    observed.config_bucket = f"{PROJECT}-{ENV}-config"
    observed.db_endpoint = "demo-prod-aurora.cluster-abc.us-west-2.rds.amazonaws.com"
    observed.cache_endpoint = "demo-prod-cache.abc.clustercfg.usw2.cache.amazonaws.com"
    observed.secrets = dict(SECRETS)
    observed.update(overrides)
    return observed


def _conf(answers=None, spec=None, observed=None):
    from mojo.deploy.provision import render

    return render.django_conf(spec or _spec(), answers or dict(ANSWERS),
                              observed or _observed(), dict(SECRETS))


def _settings(text):
    """The conf parsed as the settings assignments it is."""
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or " = " not in line:
            continue
        key, _, raw = line.partition(" = ")
        values[key.strip()] = raw.strip().strip('"')
    return values


@th.django_unit_test("django.conf carries every key a prod node needs to start")
def test_conf_has_every_required_key(opts):
    values = _settings(_conf())

    for key in ("SECRET_KEY", "BASE_URL", "EMAIL_FROM", "GITHUB_REPO",
                "DATABASE_HOST", "DATABASE_PORT", "DATABASE_NAME",
                "DATABASE_USER", "DATABASE_PASSWORD",
                "REDIS_SERVER", "REDIS_PORT",
                "AWS_REGION", "AWS_CONFIG_BUCKET", "AWS_CONFIG_PREFIX",
                "CONFIG_SYNC_RESTART", "INFRASTRUCTURE_MODE"):
        th.assert_true(key in values,
                       f"{key} must be published — a node without it starts "
                       f"with a Django default instead of this environment")

    th.assert_eq(values["BASE_URL"], "https://example.com",
                 "BASE_URL must be the https apex, since that is what token "
                 "links and webhooks are built from")
    th.assert_eq(values["DATABASE_USER"], "mojo",
                 "DATABASE_USER must match data.py's MASTER_USERNAME, or the "
                 "node cannot authenticate to the cluster that was just built")
    th.assert_eq(values["EMAIL_FROM"], ANSWERS["operator_email"],
                 "EMAIL_FROM comes from the operator email answer")
    th.assert_eq(values["GITHUB_REPO"], ANSWERS["github_repo"],
                 "GITHUB_REPO comes from the repository answer")
    th.assert_eq(values["CONFIG_SYNC_RESTART"], "True",
                 "config_sync must restart the app when the config changes")


@th.django_unit_test("django.conf carries the endpoints AWS actually reported")
def test_conf_uses_observed_endpoints(opts):
    observed = _observed()
    values = _settings(_conf(observed=observed))

    th.assert_eq(values["DATABASE_HOST"], observed.db_endpoint,
                 "DATABASE_HOST must be the writer endpoint that was observed")
    th.assert_eq(values["REDIS_SERVER"], observed.cache_endpoint,
                 "REDIS_SERVER must be the cache endpoint that was observed")
    th.assert_eq(values["DATABASE_PASSWORD"], SECRETS["db_password"],
                 "the database password comes from the bootstrap secrets "
                 "object, which is generated once and read back forever")
    th.assert_eq(values["SECRET_KEY"], SECRETS["django_secret_key"],
                 "the Django secret key comes from the same object — "
                 "regenerating it on every render would log the fleet out")


@th.django_unit_test("no AWS key material is ever published to the config bucket")
def test_conf_publishes_no_aws_credentials(opts):
    text = _conf()

    for forbidden in ("AWS_KEY", "AWS_SECRET", "AWS_ACCESS_KEY",
                      "aws_access_key_id", "aws_secret_access_key"):
        th.assert_true(forbidden not in text,
                       f"{forbidden} must never appear in the published "
                       f"config — the node carries an instance profile, and a "
                       f"static key here would sit on every node forever")


@th.django_unit_test("REDIS_SCHEME is left unset so the rediss default holds")
def test_conf_omits_redis_scheme(opts):
    values = _settings(_conf())

    th.assert_true("REDIS_SCHEME" not in values,
                   "REDIS_SCHEME must stay unset: the Valkey group is built "
                   "with transit encryption on, and mojo/helpers/redis already "
                   "defaults to rediss — writing it here duplicates a default "
                   "that can then drift away from the infrastructure")


@th.django_unit_test("INFRASTRUCTURE_MODE reflects the env file, never a literal")
def test_infrastructure_mode_is_not_hardcoded(opts):
    from mojo.deploy.provision import inputs

    managed = _settings(_conf(answers=dict(ANSWERS)))
    th.assert_eq(managed["INFRASTRUCTURE_MODE"], inputs.MANAGED,
                 "an environment file with no mode declaration renders managed")

    external_answers = dict(ANSWERS)
    external_answers[inputs.MODE_KEY] = inputs.EXTERNAL
    external = _settings(_conf(answers=external_answers))
    th.assert_eq(external["INFRASTRUCTURE_MODE"], inputs.EXTERNAL,
                 "a file declaring external must render external — including "
                 "on an --override-external run, because the override is a "
                 "property of one invocation and never of the node it builds")

    # A typo is external too (fail-closed), and this is the assertion that a
    # hardcoded literal cannot pass.
    typo_answers = dict(ANSWERS)
    typo_answers[inputs.MODE_KEY] = "extrenal"
    typo = _settings(_conf(answers=typo_answers))
    th.assert_eq(typo["INFRASTRUCTURE_MODE"], inputs.EXTERNAL,
                 "an unrecognized mode must render external — a switch whose "
                 "job is to refuse must not be turned off by a spelling mistake")


@th.django_unit_test("the config is published with the sha256 config_sync verifies")
def test_publish_sets_sha256_metadata(opts):
    import hashlib

    from mojo.deploy.provision import discover, render

    class _S3:
        def __init__(self):
            self.calls = []

        def head_object(self, **kwargs):
            raise RuntimeError("no such object")

        def put_object(self, **kwargs):
            self.calls.append(kwargs)
            return {"ETag": "abc"}

    s3 = _S3()
    clients = discover.Clients(session=None, s3=s3)
    spec = _spec()
    observed = _observed()

    findings, actions, result = render.ensure_config(
        clients, spec, dict(ANSWERS), observed, apply=True)

    th.assert_eq(len(s3.calls), 1,
                 f"exactly one object should be published, got {len(s3.calls)}")
    call = s3.calls[0]
    body = call["Body"]
    expected = hashlib.sha256(body).hexdigest()
    th.assert_eq((call.get("Metadata") or {}).get("sha256"), expected,
                 "the sha256 metadata is what config_sync reads to verify the "
                 "download; without it CONFIG_SYNC_REQUIRE_SHA refuses the file")
    th.assert_eq(call["Key"], render.conf_key(spec),
                 "the object must land under the prefix a node's bootstrap.conf "
                 "names, or config_sync 404s on a bucket that plainly has it")
    th.assert_eq(call.get("ServerSideEncryption"), "AES256",
                 "the published config holds every downstream credential and "
                 "must be encrypted at rest")
    th.assert_true(bool(result.as_dict().get("django_conf")),
                   "a successful publish must report what it published")


@th.django_unit_test("nothing is published when the endpoints are not up yet")
def test_publish_waits_for_endpoints(opts):
    from mojo.deploy.provision import discover, render, report

    class _S3:
        def head_object(self, **kwargs):
            raise AssertionError("S3 must not be touched before the endpoints "
                                 "exist")

        def put_object(self, **kwargs):
            raise AssertionError("nothing may be published before the "
                                 "endpoints exist")

    clients = discover.Clients(session=None, s3=_S3())
    observed = _observed(db_endpoint=None)

    findings, actions, result = render.ensure_config(
        clients, _spec(), dict(ANSWERS), observed, apply=True)

    th.assert_true(any(f.status == report.PENDING for f in findings),
                   "an unbuilt database is PENDING, not a failure — Aurora "
                   "takes ten minutes and the next run picks this up")
    th.assert_eq(actions, [], "and nothing is proposed")


@th.django_unit_test("the agent template's log groups equal spec.names() exactly")
def test_cloudwatch_template_matches_spec_names(opts):
    from mojo.deploy.provision import spec as spec_module
    from mojo.deploy.provision import storage

    spec = _spec()
    names = spec_module.names(spec)
    document = json.loads(storage.cloudwatch_agent_config(spec))

    collected = document["logs"]["logs_collected"]["files"]["collect_list"]
    used = {entry["log_group_name"] for entry in collected}
    expected = set(names["log_groups"].values())

    th.assert_eq(used, expected,
                 f"the agent must write to exactly the groups this environment "
                 f"creates: {sorted(expected)}, got {sorted(used)}. These names "
                 f"come from spec.names() in observability.py, identity.py and "
                 f"here — hand-writing one of them is how a node ends up "
                 f"logging into a group nobody reads")

    paths = {entry["file_path"] for entry in collected}
    th.assert_true(any(path.startswith("/opt/api/var/logs/") for path in paths),
                   f"the app's logit output directory (VAR_ROOT/logs) must be "
                   f"collected, got {sorted(paths)}")
    th.assert_true("/var/log/cloud-init-output.log" in paths,
                   "cloud-init's output is where a failed stage 1 is visible "
                   "without SSH, so it must reach CloudWatch")
    th.assert_true("@" not in storage.cloudwatch_agent_config(spec),
                   "no substitution placeholder may survive into what a node "
                   "installs")


@th.django_unit_test("the packaged stage1.sh is substituted, never shipped raw")
def test_stage1_script_substitutes_the_version(opts):
    from mojo.deploy.provision import storage

    script = storage.stage1_script("1.2.3")
    th.assert_true(storage.VERSION_PLACEHOLDER not in script,
                   "the version placeholder must be substituted before upload")
    th.assert_true('DJANGO_MOJO_VERSION="1.2.3"' in script,
                   "the substituted script must pin the version it was given")
    th.assert_true("set -euo pipefail" in script,
                   "the published script must still fail loudly")

    default = storage.stage1_script()
    th.assert_true(f'DJANGO_MOJO_VERSION="{storage.django_mojo_version()}"'
                   in default,
                   "with no version given, the pin is this django-mojo's own "
                   "version — that is the whole point of the pin")
