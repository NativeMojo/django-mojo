"""The two resources an environment needs before it can serve a WebApp.

A django-mojo environment could be provisioned complete — nodes serving, TLS
valid, push-to-deploy working — and still be unable to host a single static
site, because the edge plane needs two things nothing created:

    a releases bucket, the only place EDGE_RELEASE_BUCKETS declares, without
    which `releases.register` refuses to mint a release at all; and

    a KMS key, because dnsman's Certificate is a KSMSecrets model and those
    raise outright when KMS_KEY_ID is unset — so the environment cannot hold
    a certificate, and therefore cannot serve an edge vhost.

Both are discovered only at WebApp-onboarding time, which is the worst moment
to discover them. These tests hold them to the same standard as the rest of the
package: created once, adopted on every later run, and never guessed at.
"""

from testit import helpers as th


REGION = "us-west-2"
PROJECT = "wmx"
ENV = "prod"
ACCOUNT = "123456789012"
KEY_ID = "1234abcd-12ab-34cd-56ef-1234567890ab"


def _stub(service):
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(
        service, region_name=REGION,
        aws_access_key_id="testing", aws_secret_access_key="testing")
    return client, Stubber(client)


def _spec(**overrides):
    from mojo.deploy.provision import spec as spec_module
    overrides.setdefault("account_id", ACCOUNT)
    return spec_module.build(PROJECT, ENV, REGION,
                             preset=overrides.pop("preset", "small"),
                             **overrides)


def _clients(**overrides):
    from mojo.deploy.provision import discover
    return discover.Clients(session=None, **overrides)


def _observed(**overrides):
    from mojo.deploy.provision import discover
    observed = discover.blank()
    observed.account_id = ACCOUNT
    observed.update(overrides)
    return observed


def _codes(findings, status=None):
    return [f.code for f in findings
            if status is None or f.status == status]


def _assert_no_blind(findings, message):
    from mojo.deploy.provision import report
    blind = [f"{f.code}: {f.message}" for f in findings
             if f.status == report.BLIND]
    th.assert_eq(blind, [], message)


# ── the releases bucket ─────────────────────────────────────────────────────

@th.django_unit_test("the releases bucket is its own bucket, not the config bucket")
def test_releases_bucket_is_separate(opts):
    from mojo.deploy.provision import spec as spec_module

    names = spec_module.names(_spec())
    th.assert_true(names["releases_bucket"] != names["config_bucket"],
                   "a GitHub Actions credential writes to the releases "
                   "bucket, and the config bucket holds this environment's "
                   "bootstrap-secrets.json — those must not meet")


@th.django_unit_test("a missing releases bucket is created and hardened")
def test_releases_bucket_is_created(opts):
    from mojo.deploy.provision import storage
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    client, stubber = _stub("s3")
    stubber.add_response("create_bucket", {})
    stubber.add_response("put_bucket_tagging", {})
    # The same hardening the config bucket gets, through the same helper —
    # queued in the order that helper applies it.
    stubber.add_response("put_bucket_versioning", {})
    stubber.add_response("put_bucket_encryption", {})
    stubber.add_response("put_public_access_block", {})
    stubber.add_response("put_bucket_policy", {})
    with stubber:
        findings, actions, result = storage.ensure_releases_bucket(
            _clients(s3=client), spec, _observed(), apply=True)

    _assert_no_blind(findings, "every request must be model-valid")
    th.assert_eq(result["releases_bucket"], names["releases_bucket"],
                 "the bucket name must come from spec.names()")
    th.assert_true(any(a.verb == "create" for a in actions),
                   f"a create must be declared: {[a.verb for a in actions]}")


@th.django_unit_test("an existing releases bucket is adopted, not recreated")
def test_releases_bucket_is_adopted(opts):
    from mojo.deploy.provision import report, storage
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    client, stubber = _stub("s3")
    with stubber:
        findings, actions, result = storage.ensure_releases_bucket(
            _clients(s3=client), spec,
            _observed(releases_bucket=names["releases_bucket"],
                      releases_bucket_state={}), apply=False)

    _assert_no_blind(findings, "a preview must make no S3 call")
    th.assert_in("releases.ok", _codes(findings, report.PASS),
                 f"the bucket must read as present: {_codes(findings)}")


# ── the KMS key ─────────────────────────────────────────────────────────────

@th.django_unit_test("the key is created, aliased and set to rotate")
def test_kms_key_is_created(opts):
    from mojo.deploy.provision import encryption
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    client, stubber = _stub("kms")
    stubber.add_response("create_key", {"KeyMetadata": {"KeyId": KEY_ID}})
    stubber.add_response("create_alias", {})
    stubber.add_response("enable_key_rotation", {})
    with stubber:
        findings, actions, result = encryption.ensure_key(
            _clients(kms=client), spec, _observed(), apply=True)

    _assert_no_blind(findings, "every request must be model-valid")
    stubber.assert_no_pending_responses()
    th.assert_eq(result["kms_key_id"], KEY_ID,
                 "the key id is what KMS_KEY_ID is rendered from")
    th.assert_true(any(names["kms_alias"] in str(a.target) for a in actions),
                   "the alias is the resume anchor and must be named")


@th.django_unit_test("a key that exists by alias is adopted without creating a second")
def test_kms_key_is_adopted(opts):
    from mojo.deploy.provision import encryption, report

    client, stubber = _stub("kms")
    with stubber:
        findings, actions, result = encryption.ensure_key(
            _clients(kms=client), _spec(),
            _observed(kms_key_id=KEY_ID, kms_key_rotation=True), apply=True)

    _assert_no_blind(findings, "an adopted key must cost no KMS call")
    th.assert_eq(actions, [], "nothing may be created for an existing key")
    th.assert_in("kms.ok", _codes(findings, report.PASS), str(_codes(findings)))
    th.assert_eq(result["kms_key_id"], KEY_ID, "the observed key is the key")


@th.django_unit_test("a created key that could not be aliased is reported, never retried into a second key")
def test_kms_orphan_key_is_reported(opts):
    from mojo.deploy.provision import encryption, report

    client, stubber = _stub("kms")
    stubber.add_response("create_key", {"KeyMetadata": {"KeyId": KEY_ID}})
    stubber.add_client_error("create_alias", service_error_code="AccessDenied")
    with stubber:
        findings, actions, result = encryption.ensure_key(
            _clients(kms=client), _spec(), _observed(), apply=True)

    th.assert_in("kms.orphan_key", _codes(findings, report.MANUAL),
                 f"an unaliased key is invisible to the next run, which would "
                 f"create another one and bill for both: {_codes(findings)}")
    manual = [f for f in findings if f.code == "kms.orphan_key"][0]
    th.assert_true(KEY_ID in (manual.remedy or ""),
                   "the remedy must name the key that was left behind")
    th.assert_eq(result.as_dict().get("kms_key_id"), None,
                 "an unreachable key must not be reported as this "
                 "environment's key")


@th.django_unit_test("rotation disabled on an adopted key is drift, and is corrected")
def test_kms_rotation_is_converged(opts):
    from mojo.deploy.provision import encryption, report

    client, stubber = _stub("kms")
    stubber.add_response("enable_key_rotation", {})
    with stubber:
        findings, actions, result = encryption.ensure_key(
            _clients(kms=client), _spec(),
            _observed(kms_key_id=KEY_ID, kms_key_rotation=False), apply=True)

    _assert_no_blind(findings, "enable_key_rotation must be model-valid")
    stubber.assert_no_pending_responses()
    th.assert_in("kms.rotation_off", _codes(findings, report.DRIFT),
                 str(_codes(findings)))


@th.django_unit_test("a key scheduled for deletion is not adopted")
def test_kms_pending_deletion_is_not_adopted(opts):
    from mojo.deploy.provision import discover

    client, stubber = _stub("kms")
    stubber.add_response(
        "describe_key",
        {"KeyMetadata": {"KeyId": KEY_ID, "KeyState": "PendingDeletion"}})
    observed = _observed()
    with stubber:
        discover._observe_encryption(
            _clients(kms=client), _spec(), observed, [])

    th.assert_eq(observed.kms_key_id, None,
                 "adopting a key with a deletion date already fixed writes "
                 "secrets that become permanently unreadable on that date")


# ── what the nodes are told ─────────────────────────────────────────────────

@th.django_unit_test("django.conf declares the bucket and the key the edge plane needs")
def test_conf_publishes_the_edge_plane_settings(opts):
    from mojo.deploy.provision import render
    from mojo.deploy.provision import spec as spec_module

    spec = _spec()
    names = spec_module.names(spec)
    observed = _observed(releases_bucket=names["releases_bucket"],
                         kms_key_id=KEY_ID)
    text = render.django_conf(spec, {"apex_domain": "example.com"}, observed,
                              {})

    values = {}
    for line in text.splitlines():
        if " = " in line and not line.strip().startswith("#"):
            key, _, raw = line.partition(" = ")
            values[key.strip()] = raw.strip()

    th.assert_eq(values.get("KMS_KEY_ID"), f'"{KEY_ID}"',
                 "KSMSecrets raises without this, so dnsman cannot hold a "
                 "certificate and no edge vhost can serve TLS")
    th.assert_eq(values.get("EDGE_RELEASE_BUCKETS"),
                 '["%s"]' % names["releases_bucket"],
                 "a list literal — validators.release_buckets reads it with "
                 "kind='list', and a bare string would allowlist nothing")
    th.assert_eq(values.get("EDGE_SOCKET_BASE"), '"/opt/api/var"',
                 "validators.socket_base() defaults to /run/mojo while the "
                 "framework's own mojo-asgi.service binds "
                 "<PROJ_PATH>/var/asgi.sock — unpublished, no edge vhost can "
                 "declare an upstream to the app running beside it")
