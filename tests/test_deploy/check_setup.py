"""mojo.deploy.check_setup — the read-only AWS account audit.

Covers the reworked parts: the BLIND status and its non-zero exit, the
universal/topology split, and that every listing call now follows pagination.

Deliberately NOT covered: check_lb / check_rds / check_cache beyond what the
exit-code tests need. Mocking those out end to end would assert the shape of
the fixtures rather than the behaviour of the code.
"""

import io
import json
import os
import shutil
import tempfile
from contextlib import redirect_stdout
from unittest import mock

from testit import helpers as th


class _FakeSession:
    """Stands in for boto3.Session. Any client not supplied is a bare Mock."""

    def __init__(self, clients=None, region_name="us-east-1"):
        self.region_name = region_name
        self._clients = dict(clients or {})

    def client(self, name):
        if name not in self._clients:
            self._clients[name] = mock.Mock()
        return self._clients[name]


def _denied(operation="Describe"):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": "AccessDenied"}}, operation)


def _tempdir():
    return tempfile.mkdtemp(prefix="testit_check_setup.")


def _config_file(root, body="AWS_REGION=us-east-1\n"):
    path = os.path.join(root, "django.conf")
    with open(path, "w") as handle:
        handle.write(body)
    return path


def _run(argv, session, config_body="AWS_REGION=us-east-1\n"):
    """Run main() with a fake session and --json, returning (exit code, report)."""
    from mojo.deploy import check_setup as cs

    root = _tempdir()
    try:
        path = _config_file(root, config_body)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cs.main(["--config", path, "--json"] + argv,
                           session_factory=lambda config, profile: session)
        return code, json.loads(buffer.getvalue())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _named(report, name):
    return [f for f in report["findings"] if f["name"] == name]


def _statuses(report, status):
    return [f["name"] for f in report["findings"] if f["status"] == status]


def _instance(iid, az="us-east-1a", itype="m6i.large", imds="required",
              profile=True):
    row = {
        "InstanceId": iid,
        "InstanceType": itype,
        "State": {"Name": "running"},
        "Placement": {"AvailabilityZone": az},
        "MetadataOptions": {"HttpTokens": imds},
        "Tags": [{"Key": "Name", "Value": iid}],
    }
    if profile:
        row["IamInstanceProfile"] = {
            "Arn": "arn:aws:iam::123456789012:instance-profile/app"}
    return row


def _healthy_cache(**overrides):
    group = {
        "ReplicationGroupId": "cache-1",
        "MemberClusters": ["a", "b"],
        "Engine": "valkey",
        "AutomaticFailover": "enabled",
        "MultiAZ": "enabled",
        "AtRestEncryptionEnabled": True,
        "TransitEncryptionEnabled": True,
        "AuthTokenEnabled": True,
        "SnapshotRetentionLimit": 3,
    }
    group.update(overrides)
    return group


# ---------------------------------------------------------------------------
# exit status
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_clean_account_exits_zero(opts):
    elasticache = mock.Mock()
    elasticache.describe_replication_groups.return_value = {
        "ReplicationGroups": [_healthy_cache()]}

    code, report = _run(["--section", "cache"],
                        _FakeSession({"elasticache": elasticache}))

    th.assert_eq(code, 0,
                 f"a section with no FAIL and no BLIND must exit 0; got "
                 f"{report['counts']}")
    th.assert_eq(report["counts"]["FAIL"], 0,
                 f"a healthy replication group must produce no FAIL: "
                 f"{_statuses(report, 'FAIL')}")


@th.django_unit_test()
def test_a_single_fail_exits_one(opts):
    elasticache = mock.Mock()
    elasticache.describe_replication_groups.return_value = {
        "ReplicationGroups": [_healthy_cache(MultiAZ="disabled")]}

    code, report = _run(["--section", "cache"],
                        _FakeSession({"elasticache": elasticache}))

    th.assert_eq(code, 1, "any FAIL must make the audit exit non-zero")
    th.assert_true(_named(report, "cache-1: not Multi-AZ"),
                   f"the Multi-AZ gap must be reported: "
                   f"{_statuses(report, 'FAIL')}")


@th.django_unit_test()
def test_access_denied_is_blind_and_exits_nonzero(opts):
    """The fail-closed rework. An audit credential that cannot see a section
    used to produce INFO findings and exit 0 — a green gate on an audit that
    never ran."""
    elasticache = mock.Mock()
    elasticache.describe_replication_groups.side_effect = _denied()
    elasticache.describe_cache_clusters.side_effect = _denied()

    code, report = _run(["--section", "cache"],
                        _FakeSession({"elasticache": elasticache}))

    blind = _statuses(report, "BLIND")
    th.assert_true(blind,
                   f"AccessDenied must produce a BLIND finding, got statuses "
                   f"{[f['status'] for f in report['findings']]}")
    th.assert_eq(report["counts"]["FAIL"], 0,
                 "this fixture has no real FAIL — the non-zero exit must come "
                 "from BLIND alone, which is the point of the test")
    th.assert_eq(code, 1,
                 "a section the credential could not see must exit non-zero; "
                 "a gate that returns green when blind is worse than no gate")


@th.django_unit_test()
def test_a_rejected_credential_is_blind_not_info(opts):
    """AuthFailure is what EC2 returns for a deactivated key. It is not a
    permission error, so it misses DENIED_CODES — but the check did not run,
    and a gate that exits 0 because its key was revoked is the fail-open case
    the BLIND status exists to close."""
    from botocore.exceptions import ClientError

    from mojo.deploy import check_setup as cs

    for code_name in ("AuthFailure", "InvalidClientTokenId", "ExpiredToken",
                      "SignatureDoesNotMatch"):
        report = cs.Report()

        def boom():
            raise ClientError({"Error": {"Code": code_name}}, "DescribeInstances")

        result = cs.safe(report, "ec2", "describe_instances", boom,
                         default="fallback")

        th.assert_eq(result, "fallback",
                     f"safe() must return the default for {code_name} rather "
                     f"than propagating")
        th.assert_eq(report.counts()[cs.BLIND], 1,
                     f"{code_name} means the credential was rejected and the "
                     f"check never ran, so it must be BLIND, not INFO — "
                     f"got {report.counts()}")


@th.django_unit_test()
def test_a_missing_credential_is_blind_not_info(opts):
    """NoCredentialsError is a BotoCoreError, so it used to fall into the INFO
    branch alongside genuine transient transport errors. A section that never
    ran because there was no credential must gate."""
    from botocore.exceptions import EndpointConnectionError, NoCredentialsError

    from mojo.deploy import check_setup as cs

    cases = (
        NoCredentialsError(),
        EndpointConnectionError(endpoint_url="https://ec2.us-west-2.amazonaws.com/"),
    )
    for err in cases:
        report = cs.Report()

        def boom():
            raise err

        cs.safe(report, "ec2", "describe_instances", boom, default=None)

        th.assert_eq(report.counts()[cs.BLIND], 1,
                     f"{type(err).__name__} means this check was never "
                     f"performed and must be BLIND, not INFO — "
                     f"got {report.counts()}")


@th.django_unit_test()
def test_safe_reports_a_botocore_error_as_info_not_blind(opts):
    from botocore.exceptions import BotoCoreError

    from mojo.deploy import check_setup as cs

    report = cs.Report()

    def boom():
        raise BotoCoreError()

    result = cs.safe(report, "ec2", "describe_instances", boom, default="fallback")

    th.assert_eq(result, "fallback",
                 "safe() must return the default rather than propagating")
    th.assert_eq(report.counts()[cs.BLIND], 0,
                 "a transport-level BotoCoreError is not a permission gap and "
                 "must not be reported as BLIND")
    th.assert_eq(report.counts()[cs.INFO], 1,
                 f"the error must still be recorded as INFO: {report.findings}")


@th.django_unit_test()
def test_safe_reports_throttling_as_blind(opts):
    from botocore.exceptions import ClientError

    from mojo.deploy import check_setup as cs

    report = cs.Report()

    def boom():
        raise ClientError({"Error": {"Code": "ThrottlingException"}}, "List")

    cs.safe(report, "s3", "list_buckets", boom)

    th.assert_eq(report.counts()[cs.BLIND], 1,
                 f"a throttled call did not run, so it must be BLIND: "
                 f"{report.findings}")


# ---------------------------------------------------------------------------
# universal vs topology
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_an_empty_account_is_not_a_failure_by_default(opts):
    """A dev account, a Lambda-only account, or one mid-setup legitimately has
    no EC2. Failing those from framework code is how an audit tool teaches
    everyone to ignore it."""
    ec2 = mock.Mock()
    ec2.describe_instances.return_value = {"Reservations": []}

    code, report = _run(["--section", "ec2"], _FakeSession({"ec2": ec2}))

    th.assert_eq(_statuses(report, "FAIL"), [],
                 f"an account with no EC2 instances must not FAIL without "
                 f"--topology; got {_statuses(report, 'FAIL')}")
    th.assert_eq(code, 0,
                 "an empty account must exit 0 by default — a non-zero gate "
                 "here is a false alarm on every dev and Lambda-only account")
    th.assert_true(_named(report, "no instances"),
                   "the emptiness must still be reported, just not as a FAIL")


@th.django_unit_test()
def test_an_empty_account_does_fail_under_the_topology_opt_in(opts):
    """The other half: someone who asked for the reference topology has said
    they expect a web node and API nodes, so an empty account is a real gap."""
    ec2 = mock.Mock()
    ec2.describe_instances.return_value = {"Reservations": []}

    code, report = _run(["--section", "ec2", "--topology", "reference"],
                        _FakeSession({"ec2": ec2}))

    th.assert_true(_named(report, "no instances"),
                   f"--topology reference must FAIL an empty account; got "
                   f"statuses {[f['status'] for f in report['findings']]}")
    th.assert_eq(_named(report, "no instances")[0]["status"], "FAIL",
                 "under the opt-in, an empty account is a topology violation")
    th.assert_eq(code, 1,
                 "a topology assertion that fails must gate")


@th.django_unit_test()
def test_topology_assertions_are_off_by_default(opts):
    ec2 = mock.Mock()
    ec2.describe_instances.return_value = {
        "Reservations": [{"Instances": [_instance("i-solo")]}]}

    code, report = _run(["--section", "ec2"], _FakeSession({"ec2": ec2}))

    th.assert_true(not _named(report, "single node"),
                   "a single-node deployment is a different shape, not a "
                   "misconfiguration — it must not FAIL by default")
    th.assert_eq(report["topology"], "none",
                 "the report must record which topology was asserted")
    th.assert_eq(code, 0,
                 f"a healthy single node must exit 0 by default; FAILs were "
                 f"{_statuses(report, 'FAIL')}")


@th.django_unit_test()
def test_topology_assertions_fire_under_the_opt_in(opts):
    ec2 = mock.Mock()
    ec2.describe_instances.return_value = {
        "Reservations": [{"Instances": [_instance("i-solo")]}]}

    code, report = _run(["--section", "ec2", "--topology", "reference"],
                        _FakeSession({"ec2": ec2}))

    th.assert_true(_named(report, "single node"),
                   f"--topology reference must assert the reference shape; "
                   f"findings were {[f['name'] for f in report['findings']]}")
    th.assert_eq(report["topology"], "reference",
                 "the report must record the asserted topology")
    th.assert_eq(code, 1, "an unmet topology assertion is a FAIL")


@th.django_unit_test()
def test_topology_can_be_turned_on_from_the_config_file(opts):
    ec2 = mock.Mock()
    ec2.describe_instances.return_value = {
        "Reservations": [{"Instances": [_instance("i-solo")]}]}

    code, report = _run(
        ["--section", "ec2"], _FakeSession({"ec2": ec2}),
        config_body="AWS_REGION=us-east-1\nMOJO_DEPLOY_TOPOLOGY=reference\n")

    th.assert_true(_named(report, "single node"),
                   "MOJO_DEPLOY_TOPOLOGY in django.conf is the persistent way "
                   "to opt in and must have the same effect as --topology")


@th.django_unit_test()
def test_universal_findings_still_fire_with_topology_off(opts):
    ec2 = mock.Mock()
    ec2.describe_instances.return_value = {
        "Reservations": [{"Instances": [
            _instance("i-solo", imds="optional", profile=False)]}]}

    code, report = _run(["--section", "ec2"], _FakeSession({"ec2": ec2}))

    th.assert_true(_named(report, "i-solo: IMDSv1 allowed"),
                   "IMDSv2 is a universal finding — the topology opt-in must "
                   "not switch off security checks")
    th.assert_true(_named(report, "i-solo: no IAM instance profile"),
                   "a missing instance profile is universal too")
    th.assert_eq(code, 1, "universal FAILs must still gate the exit code")


# ---------------------------------------------------------------------------
# pagination
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_describe_instances_follows_every_page(opts):
    """A PASS used to mean "nothing bad in the first page of the account"."""
    ec2 = mock.Mock()
    ec2.describe_instances.side_effect = [
        {"Reservations": [{"Instances": [_instance("i-page1", imds="optional")]}],
         "NextToken": "page-2"},
        {"Reservations": [{"Instances": [_instance("i-page2", imds="optional")]}]},
    ]

    code, report = _run(["--section", "ec2"], _FakeSession({"ec2": ec2}))

    th.assert_true(_named(report, "i-page1: IMDSv1 allowed"),
                   "the first page's instance must be audited")
    th.assert_true(_named(report, "i-page2: IMDSv1 allowed"),
                   "the SECOND page's instance must be audited too — without "
                   "pagination this finding is silently invisible")
    th.assert_eq(ec2.describe_instances.call_count, 2,
                 "describe_instances must be called once per page")
    second_call = ec2.describe_instances.call_args_list[1]
    th.assert_eq(second_call.kwargs.get("NextToken"), "page-2",
                 f"the continuation token must be sent back: {second_call}")


@th.django_unit_test()
def test_list_paginated_honours_a_named_token_field(opts):
    from mojo.deploy import check_setup as cs

    client = mock.Mock()
    client.describe_log_groups.side_effect = [
        {"logGroups": [{"logGroupName": "a"}], "nextToken": "n2"},
        {"logGroups": [{"logGroupName": "b"}]},
    ]

    rows = cs.list_paginated(client, "describe_log_groups", "logGroups",
                             token_field="nextToken")

    th.assert_eq([r["logGroupName"] for r in rows], ["a", "b"],
                 f"CloudWatch Logs uses a lowercase nextToken; both pages must "
                 f"be collected, got {rows}")


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_profile_is_preferred_over_static_keys(opts):
    from mojo.deploy import check_setup as cs

    config = {"AWS_REGION": "eu-west-1", "AWS_KEY": "k", "AWS_SECRET": "s"}
    with mock.patch("boto3.Session") as session_cls:
        cs.build_session(config, "prod")

    th.assert_eq(session_cls.call_args.kwargs,
                 {"profile_name": "prod", "region_name": "eu-west-1"},
                 f"--profile must win over django.conf keys, got "
                 f"{session_cls.call_args}")


@th.django_unit_test()
def test_static_keys_are_used_when_both_are_present(opts):
    from mojo.deploy import check_setup as cs

    config = {"AWS_REGION": "eu-west-1", "AWS_KEY": "k", "AWS_SECRET": "s"}
    with mock.patch("boto3.Session") as session_cls:
        cs.build_session(config, None)

    th.assert_eq(session_cls.call_args.kwargs,
                 {"aws_access_key_id": "k", "aws_secret_access_key": "s",
                  "region_name": "eu-west-1"},
                 f"a complete key pair must be used verbatim, got "
                 f"{session_cls.call_args}")


@th.django_unit_test()
def test_half_a_key_pair_falls_through_to_the_ambient_chain(opts):
    """Pins the deliberate divergence from mojo/helpers/aws/client.py, which
    raises PartialCredentialsError. A deploy-time audit that refuses to run is
    less useful than one that falls back and says so."""
    from mojo.deploy import check_setup as cs

    config = {"AWS_REGION": "eu-west-1", "AWS_KEY": "k"}
    with mock.patch("boto3.Session") as session_cls:
        session = cs.build_session(config, None)

    th.assert_true(session is not None,
                   "a half credential pair must not raise")
    th.assert_eq(session_cls.call_args.kwargs, {"region_name": "eu-west-1"},
                 f"the lone key must be ignored and the ambient chain used, "
                 f"got {session_cls.call_args}")


@th.django_unit_test()
def test_static_keys_in_the_config_file_are_a_fail(opts):
    from mojo.deploy import check_setup as cs

    sts = mock.Mock()
    sts.get_caller_identity.return_value = {
        "Account": "123456789012",
        "Arn": "arn:aws:iam::123456789012:user/app"}
    report = cs.Report()

    cs.check_account(report, _FakeSession({"sts": sts}),
                     {"AWS_KEY": "k", "AWS_SECRET": "s"})

    names = [f["name"] for f in report.findings if f["status"] == cs.FAIL]
    th.assert_in("static credentials in django.conf", names,
                 f"plaintext keys in django.conf must be a FAIL, got {names}")


# ---------------------------------------------------------------------------
# iam
# ---------------------------------------------------------------------------

def _iam_client(key_age_days):
    import datetime

    created = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(days=key_age_days))
    iam = mock.Mock()
    iam.list_instance_profiles.return_value = {
        "InstanceProfiles": [{"InstanceProfileName": "app"}]}
    iam.list_attached_user_policies.return_value = {"AttachedPolicies": []}
    iam.list_access_keys.return_value = {"AccessKeyMetadata": [
        {"AccessKeyId": "AKIAEXAMPLE12345", "Status": "Active",
         "CreateDate": created}]}
    iam.list_mfa_devices.return_value = {"MFADevices": [{"SerialNumber": "x"}]}
    return iam


@th.django_unit_test()
def test_an_access_key_older_than_a_year_is_a_fail(opts):
    from mojo.deploy import check_setup as cs

    report = cs.Report()
    identity = {"Arn": "arn:aws:iam::123456789012:user/app"}
    cs.check_iam(report, _FakeSession({"iam": _iam_client(400)}), identity)

    failures = [f for f in report.findings if f["status"] == cs.FAIL]
    th.assert_true(any("key 400 days old" in f["name"] for f in failures),
                   f"a key past a year must FAIL, got {failures}")


@th.django_unit_test()
def test_a_fresh_access_key_passes(opts):
    from mojo.deploy import check_setup as cs

    report = cs.Report()
    identity = {"Arn": "arn:aws:iam::123456789012:user/app"}
    cs.check_iam(report, _FakeSession({"iam": _iam_client(30)}), identity)

    th.assert_eq(report.counts()[cs.FAIL], 0,
                 f"a 30-day-old key is inside every rotation window: "
                 f"{report.findings}")
    th.assert_true(any(f["name"] == "app: key age" for f in report.findings),
                   f"the key age must still be reported as a PASS: "
                   f"{report.findings}")


@th.django_unit_test()
def test_a_role_identity_says_why_the_user_checks_were_skipped(opts):
    """The old code returned silently on a role identity, producing nothing at
    all on exactly the deployments the reference topology recommends."""
    from mojo.deploy import check_setup as cs

    report = cs.Report()
    identity = {"Arn": "arn:aws:sts::123456789012:assumed-role/app/node"}
    cs.check_iam(report, _FakeSession({"iam": _iam_client(30)}), identity)

    th.assert_true(any("skipped" in f["name"] for f in report.findings),
                   f"a role identity must say the per-user checks do not "
                   f"apply, not vanish: {[f['name'] for f in report.findings]}")


# ---------------------------------------------------------------------------
# s3
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_incomplete_public_access_block_fails_and_names_the_gaps(opts):
    from botocore.exceptions import ClientError

    s3 = mock.Mock()
    s3.list_buckets.return_value = {"Buckets": [{"Name": "assets"}]}
    s3.get_public_access_block.return_value = {
        "PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True,
            "IgnorePublicAcls": False,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": False,
        }}
    s3.get_bucket_policy.side_effect = ClientError(
        {"Error": {"Code": "NoSuchBucketPolicy"}}, "GetBucketPolicy")
    s3.get_bucket_versioning.return_value = {"Status": "Enabled"}
    s3.get_bucket_encryption.return_value = {
        "ServerSideEncryptionConfiguration": {"Rules": [
            {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}}

    code, report = _run(["--section", "s3"], _FakeSession({"s3": s3}))

    found = _named(report, "assets: public access block incomplete")
    th.assert_true(found,
                   f"a partial public-access block must FAIL; findings were "
                   f"{[f['name'] for f in report['findings']]}")
    th.assert_eq(found[0]["status"], "FAIL",
                 "an incomplete public-access block is a FAIL, not a warning")
    th.assert_in("IgnorePublicAcls", found[0]["detail"],
                 f"the finding must name the flags that are off: "
                 f"{found[0]['detail']}")
    th.assert_in("RestrictPublicBuckets", found[0]["detail"],
                 f"the finding must name every flag that is off: "
                 f"{found[0]['detail']}")
    th.assert_eq(code, 1, "an exposed-bucket risk must gate the exit code")


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_an_unknown_section_is_rejected_before_any_aws_call(opts):
    from mojo.deploy import check_setup as cs

    root = _tempdir()
    try:
        path = _config_file(root)
        factory = mock.Mock()
        with mock.patch("boto3.Session") as session_cls:
            with th.assert_raises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    cs.main(["--config", path, "--section", "nope"],
                            session_factory=factory)
        th.assert_eq(factory.call_count, 0,
                     "a typo in --section must cost a usage message, not a "
                     "round of AWS API calls")
        th.assert_eq(session_cls.call_count, 0,
                     "no boto3.Session may be constructed for an invalid section")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_json_output_carries_findings_and_counts(opts):
    elasticache = mock.Mock()
    elasticache.describe_replication_groups.return_value = {
        "ReplicationGroups": [_healthy_cache(MultiAZ="disabled")]}

    code, report = _run(["--section", "cache"],
                        _FakeSession({"elasticache": elasticache}))

    th.assert_in("findings", report, f"--json must emit findings: {report}")
    th.assert_in("counts", report, f"--json must emit counts: {report}")
    th.assert_eq(sorted(report["counts"].keys()),
                 sorted(["PASS", "WARN", "FAIL", "INFO", "BLIND"]),
                 f"every status must be counted, including BLIND: "
                 f"{report['counts']}")
    th.assert_eq(report["counts"]["FAIL"], len(_statuses(report, "FAIL")),
                 "the FAIL count must agree with the findings list")
    for finding in report["findings"]:
        th.assert_in("section", finding,
                     f"every finding needs its section: {finding}")
        th.assert_in("detail", finding,
                     f"every finding needs a detail: {finding}")


@th.django_unit_test()
def test_default_config_path_is_absolute_and_not_package_relative(opts):
    from mojo.deploy import check_setup as cs

    th.assert_true(os.path.isabs(cs.DEFAULT_CONFIG_PATH),
                   f"the default config path must be absolute, got "
                   f"{cs.DEFAULT_CONFIG_PATH}")
    package_dir = os.path.dirname(os.path.abspath(cs.__file__))
    th.assert_true(not cs.DEFAULT_CONFIG_PATH.startswith(package_dir),
                   "the default must not be derived from __file__ — under "
                   "site-packages that resolves to a path that never exists")
    th.assert_eq(cs.DEFAULT_CONFIG_PATH, "/opt/api/var/django.conf",
                 f"the default must be where django.conf lives on a node, got "
                 f"{cs.DEFAULT_CONFIG_PATH}")


@th.django_unit_test()
def test_no_config_and_no_profile_exits_two(opts):
    from mojo.deploy import check_setup as cs

    code = cs.main(["--config", "/nonexistent/django.conf"])

    th.assert_eq(code, 2,
                 "with nothing to authenticate with, the audit must exit 2 "
                 "rather than pretend it ran")
