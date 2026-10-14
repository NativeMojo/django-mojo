"""CDN provisioning (maestro #8250): converge, re-run, resume, BYO, disable, rotate.

An in-memory fake AWS (cloudfront, s3, kms, sts) with the real response
shapes is injected through the service's `clients=` seam; no shared code is
patched and nothing reaches AWS. The private-key store is injected too,
because `CdnSigningKey` encrypts through real KMS and the test environment
has none.
"""

import json

from botocore.exceptions import ClientError

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

PREFIX = "fm_cdnprov_"
BUCKET = "cdn-prov-test-bucket"
REGION = "us-west-2"
CMK_ARN = "arn:aws:kms:us-west-2:123456789012:key/11111111-2222-3333-4444-555555555555"
ACCOUNT = "123456789012"


def _denied(operation):
    return ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"},
                        "ResponseMetadata": {"HTTPStatusCode": 403}}, operation)


class _FakeAws:
    """Enough of CloudFront, S3, KMS and STS for the provisioner, with a log
    of every mutating call and a set of operations to deny."""

    def __init__(self, bucket=BUCKET, kms_manager="CUSTOMER", sse="aws:kms", pab=True,
                 ownership=True, bucket_policy=None, key_policy=None):
        self.bucket = bucket
        self.mutations = []
        self.deny = set()
        self.pab = pab
        self.ownership = ownership
        self.sse = sse
        self.kms_manager = kms_manager
        self.bucket_policy = bucket_policy
        self.key_policy = key_policy or {"Version": "2012-10-17", "Statement": [
            {"Sid": "Enable IAM User Permissions", "Effect": "Allow",
             "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:root"}, "Action": "kms:*", "Resource": "*"}]}
        self.public_keys = {}
        self.key_groups = {}
        self.oacs = {}
        self.distributions = {}
        self.tags = {}
        self.invalidations = []
        self.counter = 100
        self.cloudfront = _CloudFront(self)
        self.s3 = _S3(self)
        self.kms = _Kms(self)
        self.sts = _Sts(self)

    def clients(self):
        return {"cloudfront": self.cloudfront, "s3": self.s3, "kms": self.kms, "sts": self.sts}

    def next_id(self, prefix):
        self.counter += 1
        return f"{prefix}{self.counter}"

    def check(self, operation):
        if operation in self.deny:
            raise _denied(operation)

    def mutate(self, operation):
        self.check(operation)
        self.mutations.append(operation)


class _CloudFront:
    def __init__(self, aws):
        self.aws = aws

    def list_public_keys(self, **kw):
        self.aws.check("list_public_keys")
        return {"PublicKeyList": {"Items": [{"Id": k, "Name": v["Name"]} for k, v in self.aws.public_keys.items()]}}

    def create_public_key(self, PublicKeyConfig):
        self.aws.mutate("create_public_key")
        key_id = self.aws.next_id("K")
        self.aws.public_keys[key_id] = dict(PublicKeyConfig)
        return {"PublicKey": {"Id": key_id}}

    def list_key_groups(self, **kw):
        self.aws.check("list_key_groups")
        return {"KeyGroupList": {"Items": [
            {"KeyGroup": {"Id": gid, "KeyGroupConfig": dict(cfg)}} for gid, cfg in self.aws.key_groups.items()]}}

    def get_key_group(self, Id):
        self.aws.check("get_key_group")
        return {"KeyGroup": {"Id": Id, "KeyGroupConfig": dict(self.aws.key_groups[Id])}, "ETag": "E-kg"}

    def create_key_group(self, KeyGroupConfig):
        self.aws.mutate("create_key_group")
        gid = self.aws.next_id("KG")
        self.aws.key_groups[gid] = dict(KeyGroupConfig)
        return {"KeyGroup": {"Id": gid}}

    def update_key_group(self, Id, IfMatch, KeyGroupConfig):
        self.aws.mutate("update_key_group")
        self.aws.key_groups[Id] = dict(KeyGroupConfig)
        return {"KeyGroup": {"Id": Id}}

    def list_origin_access_controls(self, **kw):
        self.aws.check("list_origin_access_controls")
        return {"OriginAccessControlList": {"Items": [dict(Id=oid, **cfg) for oid, cfg in self.aws.oacs.items()]}}

    def create_origin_access_control(self, OriginAccessControlConfig):
        self.aws.mutate("create_origin_access_control")
        oid = self.aws.next_id("OAC")
        self.aws.oacs[oid] = dict(OriginAccessControlConfig)
        return {"OriginAccessControl": {"Id": oid}}

    def _summary(self, did):
        d = self.aws.distributions[did]
        cfg = d["DistributionConfig"]
        return {"Id": did, "ARN": d["ARN"], "DomainName": d["DomainName"], "Status": d["Status"],
                "Enabled": cfg["Enabled"], "Origins": cfg["Origins"],
                "DefaultCacheBehavior": cfg["DefaultCacheBehavior"], "CacheBehaviors": cfg["CacheBehaviors"]}

    def list_distributions(self, **kw):
        self.aws.check("list_distributions")
        return {"DistributionList": {"Items": [self._summary(did) for did in self.aws.distributions]}}

    def list_tags_for_resource(self, Resource):
        self.aws.check("list_tags_for_resource")
        return {"Tags": {"Items": list(self.aws.tags.get(Resource, []))}}

    def get_distribution(self, Id):
        self.aws.check("get_distribution")
        if Id not in self.aws.distributions:
            raise ClientError({"Error": {"Code": "NoSuchDistribution"}}, "GetDistribution")
        d = self.aws.distributions[Id]
        return {"Distribution": dict(d, Id=Id), "ETag": d["ETag"]}

    def get_distribution_config(self, Id):
        self.aws.check("get_distribution_config")
        d = self.aws.distributions[Id]
        return {"DistributionConfig": json.loads(json.dumps(d["DistributionConfig"])), "ETag": d["ETag"]}

    def create_distribution_with_tags(self, DistributionConfigWithTags):
        self.aws.mutate("create_distribution_with_tags")
        did = self.aws.next_id("E")
        arn = f"arn:aws:cloudfront::{ACCOUNT}:distribution/{did}"
        self.aws.distributions[did] = {
            "ARN": arn, "DomainName": f"{did.lower()}.cloudfront.net", "Status": "InProgress",
            "DistributionConfig": json.loads(json.dumps(DistributionConfigWithTags["DistributionConfig"])),
            "ETag": "E-1"}
        self.aws.tags[arn] = list(DistributionConfigWithTags["Tags"]["Items"])
        return {"Distribution": dict(self.aws.distributions[did], Id=did), "ETag": "E-1"}

    def update_distribution(self, Id, IfMatch, DistributionConfig):
        self.aws.mutate("update_distribution")
        d = self.aws.distributions[Id]
        d["DistributionConfig"] = json.loads(json.dumps(DistributionConfig))
        d["ETag"] = IfMatch + "+"
        return {"Distribution": dict(d, Id=Id), "ETag": d["ETag"]}

    def create_invalidation(self, DistributionId, InvalidationBatch):
        self.aws.mutate("create_invalidation")
        self.aws.invalidations.append((DistributionId, list(InvalidationBatch["Paths"]["Items"])))
        return {"Invalidation": {"Id": "I1"}}


class _S3:
    def __init__(self, aws):
        self.aws = aws

    def get_bucket_location(self, Bucket):
        self.aws.check("get_bucket_location")
        return {"LocationConstraint": REGION}

    def get_public_access_block(self, Bucket):
        self.aws.check("get_public_access_block")
        flag = bool(self.aws.pab)
        return {"PublicAccessBlockConfiguration": {k: flag for k in (
            "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")}}

    def put_public_access_block(self, **kw):
        self.aws.mutate("put_public_access_block")
        self.aws.pab = True

    def get_bucket_ownership_controls(self, Bucket):
        self.aws.check("get_bucket_ownership_controls")
        return {"OwnershipControls": {"Rules": [
            {"ObjectOwnership": "BucketOwnerEnforced" if self.aws.ownership else "ObjectWriter"}]}}

    def put_bucket_ownership_controls(self, **kw):
        self.aws.mutate("put_bucket_ownership_controls")
        self.aws.ownership = True

    def get_bucket_encryption(self, Bucket):
        self.aws.check("get_bucket_encryption")
        default = {"SSEAlgorithm": self.aws.sse}
        if self.aws.sse == "aws:kms" and self.aws.kms_manager == "CUSTOMER":
            default["KMSMasterKeyID"] = CMK_ARN
        return {"ServerSideEncryptionConfiguration": {"Rules": [{"ApplyServerSideEncryptionByDefault": default}]}}

    def get_bucket_policy(self, Bucket):
        self.aws.check("get_bucket_policy")
        if self.aws.bucket_policy is None:
            raise ClientError({"Error": {"Code": "NoSuchBucketPolicy"}}, "GetBucketPolicy")
        return {"Policy": json.dumps(self.aws.bucket_policy)}

    def put_bucket_policy(self, Bucket, Policy):
        self.aws.mutate("put_bucket_policy")
        self.aws.bucket_policy = json.loads(Policy)


class _Kms:
    def __init__(self, aws):
        self.aws = aws

    def describe_key(self, KeyId):
        self.aws.check("describe_key")
        return {"KeyMetadata": {"KeyId": "11111111-2222-3333-4444-555555555555", "Arn": CMK_ARN,
                                "KeyManager": self.aws.kms_manager}}

    def get_key_policy(self, KeyId, PolicyName):
        self.aws.check("get_key_policy")
        return {"Policy": json.dumps(self.aws.key_policy)}

    def put_key_policy(self, KeyId, PolicyName, Policy):
        self.aws.mutate("put_key_policy")
        self.aws.key_policy = json.loads(Policy)


class _Sts:
    def __init__(self, aws):
        self.aws = aws

    def get_caller_identity(self):
        self.aws.check("get_caller_identity")
        return {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:role/django-mojo-setup"}


_STORED = {}


def _store(row, pem):
    """The injected private-key store: remembers the PEM per row, no KMS."""
    _STORED[row.pk] = pem


def _load(row):
    """The injected private-key loader: reads the PEM back from the store, not KMS."""
    from mojo.apps.fileman.services import cdn
    return cdn.load_private_key(_STORED[row.pk])


def _seams(aws):
    return {"clients": aws.clients(), "region": REGION, "secret_writer": _store, "key_loader": _load}


def _provisioner(manager, aws, **kw):
    from mojo.apps.fileman.services.cdn_provision import CdnProvisioner
    return CdnProvisioner(manager, **_seams(aws), **kw)


def _codes(report, status=None):
    return {i["code"] for i in report["items"] if status is None or i["status"] == status}


def _manager(name, is_public, bucket=BUCKET, **secrets):
    from mojo.apps.fileman.models import FileManager
    fm = FileManager(name=name, backend_type="s3", backend_url=f"s3://{bucket}/fileman/{name}",
                     is_active=True, is_default=False, is_public=is_public)
    fm.save()
    fm.set_secrets({"aws_key": "AKIACDNPROV", "aws_secret": "cdn-prov-secret",
                    "aws_region": REGION, **secrets})
    fm.save()
    return fm


def _reset(opts):
    from mojo.apps.fileman.models import CdnDistribution, CdnSigningKey
    from mojo.apps.fileman.services import cdn
    CdnDistribution.objects.filter(bucket_name__startswith="cdn-prov-").delete()
    CdnSigningKey.objects.all().delete()
    _STORED.clear()
    cdn._signer_cache.clear()


@th.django_unit_setup()
def setup_cdn_provision(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager
    FileManager.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(username__startswith=PREFIX).delete()
    _reset(opts)
    opts.superuser = User.objects.create(
        username=f"{PREFIX}super", email=f"{PREFIX}super@example.com", is_superuser=True)
    opts.member = User.objects.create(
        username=f"{PREFIX}member", email=f"{PREFIX}member@example.com", is_superuser=False)
    opts.public = _manager(f"{PREFIX}public", True).pk
    opts.private = _manager(f"{PREFIX}private", False).pk
    opts.optout = _manager(f"{PREFIX}optout", True, cdn="off").pk


def _fresh(pk):
    from mojo.apps.fileman.models import FileManager
    return FileManager.objects.get(pk=pk)


@th.tier("core")
@th.django_unit_test("CDN provision: a fresh account gets everything, in order, and a second run creates nothing")
def test_enable_creates_everything_then_is_idempotent(opts):
    from mojo.apps.fileman.models import CdnDistribution, CdnSigningKey
    _reset(opts)
    aws = _FakeAws()
    report = _provisioner(_fresh(opts.public), aws, apply=True).run()
    assert_true(not report["blocking"], f"a fresh enable must converge: {report['items']}")
    expected = ["create_public_key", "create_key_group", "create_origin_access_control",
                "create_distribution_with_tags", "put_bucket_policy", "put_key_policy"]
    assert_eq(aws.mutations, expected, "enable must create each resource once, in dependency order")
    row = CdnDistribution.objects.get(bucket_name=BUCKET)
    assert_true(row.is_enabled and row.distribution_id and row.oac_id and row.key_group_id,
                f"the CdnDistribution row must record every id: {row.to_dict(graph='default')}")
    assert_eq(row.status, "pending", "a freshly created distribution is still deploying")
    assert_eq(row.domain_name, f"{row.distribution_id.lower()}.cloudfront.net", "the domain is recorded")
    key = CdnSigningKey.objects.get(pk=row.signing_key_id)
    assert_true(key.key_id and key.public_key_name == f"mojo-fm-key-{key.pk}",
                "the signing key row carries the CloudFront id and its deterministic name")
    # The distribution: signed default, one behavior per CDN manager, longest prefix first,
    # public prefix unsigned, private prefix signed; the opted-out manager has no behavior.
    cfg = aws.distributions[row.distribution_id]["DistributionConfig"]
    assert_true(cfg["DefaultCacheBehavior"]["TrustedKeyGroups"]["Enabled"],
                "the default behavior must require the key group")
    behaviors = {b["PathPattern"]: b["TrustedKeyGroups"]["Enabled"] for b in cfg["CacheBehaviors"]["Items"]}
    assert_eq(behaviors, {f"fileman/{PREFIX}public/*": False, f"fileman/{PREFIX}private/*": True},
              f"behaviors must mirror the managers on the bucket: {behaviors}")
    assert_eq(cfg["Origins"]["Items"][0]["OriginAccessControlId"], row.oac_id, "the origin uses the OAC")
    assert_eq(cfg["Origins"]["Items"][0]["S3OriginConfig"]["OriginAccessIdentity"], "", "no legacy OAI")
    # Policies hold exactly our statements with the SourceArn condition.
    sids = [s["Sid"] for s in aws.bucket_policy["Statement"]]
    assert_eq(sids, [f"AllowCloudFrontServicePrincipalReadOnly_{row.distribution_id}"], "bucket policy statement")
    kms_sids = [s["Sid"] for s in aws.key_policy["Statement"]]
    assert_true(f"AllowCloudFrontDecrypt_{row.distribution_id}" in kms_sids and len(kms_sids) == 2,
                f"the KMS policy must gain our Decrypt statement and keep the existing one: {kms_sids}")
    # The manager now delivers through the CDN.
    assert_true(_fresh(opts.public).uses_cdn, "the manager resolves the new distribution")
    assert_true(not _fresh(opts.optout).uses_cdn, "an opted-out manager stays on S3")

    # Second run: nothing mutates.
    aws.mutations.clear()
    report = _provisioner(_fresh(opts.public), aws, apply=True).run()
    assert_eq(aws.mutations, [], f"a converged account must see no mutation on re-run: {report['items']}")
    assert_true("distribution.ok" in _codes(report, "PASS"), f"re-run reports the distribution as converged: {_codes(report)}")
    assert_true(not report["blocking"], "a converged re-run is not blocking")


@th.tier("core")
@th.django_unit_test("CDN provision: an interrupted run resumes and creates only what is missing")
def test_resume_after_partial_failure(opts):
    from mojo.apps.fileman.models import CdnSigningKey
    _reset(opts)
    aws = _FakeAws()
    aws.deny.add("create_key_group")
    report = _provisioner(_fresh(opts.private), aws, apply=True).run()
    assert_true(report["blocking"], "a denied CreateKeyGroup must block the run")
    denied = [i for i in report["items"] if i["code"] == "cloudfront.create_key_group.denied"]
    assert_true(denied and "cloudfront:CreateKeyGroup" in denied[0]["remedy"],
                f"the denial must name the exact IAM action: {report['items']}")
    assert_eq(aws.mutations, ["create_public_key", "create_origin_access_control"],
              "the public key (before the denial) and the independent OAC were created; nothing after")
    assert_true("distribution.blocked" in _codes(report, "BLOCKED") or any(
        i["status"] == "BLOCKED" for i in report["items"]), "dependent steps are BLOCKED, not run")
    assert_eq(CdnSigningKey.objects.filter(key_id__isnull=False).count(), 1,
              "the key row records the public key id it created")

    aws.deny.clear()
    aws.mutations.clear()
    report = _provisioner(_fresh(opts.private), aws, apply=True).run()
    assert_true(not report["blocking"], f"the rerun must converge: {report['items']}")
    assert_true("create_public_key" not in aws.mutations, "the rerun reuses the public key by its name")
    assert_eq(aws.mutations[0], "create_key_group", "the rerun creates the missing key group first")
    assert_eq(CdnSigningKey.objects.count(), 1, "no second signing key row is created on resume")


@th.tier("core")
@th.django_unit_test("CDN provision: foreign distributions are never adopted; tagged ones are")
def test_adoption_by_tag_only(opts):
    from mojo.apps.fileman.models import CdnDistribution
    _reset(opts)
    aws = _FakeAws()
    # Somebody else's distribution on the same bucket origin, untagged.
    aws.distributions["EFOREIGN"] = {
        "ARN": f"arn:aws:cloudfront::{ACCOUNT}:distribution/EFOREIGN", "DomainName": "foreign.cloudfront.net",
        "Status": "Deployed", "ETag": "E-f", "DistributionConfig": {
            "CallerReference": "theirs", "Enabled": True, "Comment": "",
            "Origins": {"Quantity": 1, "Items": [{"Id": "o", "DomainName": f"{BUCKET}.s3.{REGION}.amazonaws.com",
                                                  "S3OriginConfig": {"OriginAccessIdentity": ""}}]},
            "DefaultCacheBehavior": {"TargetOriginId": "o", "TrustedKeyGroups": {"Enabled": False, "Quantity": 0}},
            "CacheBehaviors": {"Quantity": 0, "Items": []}}}
    report = _provisioner(_fresh(opts.public), aws, apply=True).run()
    assert_true("distribution.foreign" in _codes(report, "MANUAL"),
                f"an untagged distribution on our bucket must be a MANUAL finding: {_codes(report)}")
    assert_true("create_distribution_with_tags" not in aws.mutations and "update_distribution" not in aws.mutations,
                "a foreign distribution is neither duplicated nor modified")
    assert_true(not CdnDistribution.objects.filter(bucket_name=BUCKET, is_enabled=True).exists(),
                "nothing is enabled while the distribution question is open")

    # Tag it: now it is ours, and it is converged (behaviors + signed default) rather than duplicated.
    aws.tags[f"arn:aws:cloudfront::{ACCOUNT}:distribution/EFOREIGN"] = [
        {"Key": "mojo:fileman-bucket", "Value": BUCKET}]
    aws.mutations.clear()
    report = _provisioner(_fresh(opts.public), aws, apply=True).run()
    assert_true("create_distribution_with_tags" not in aws.mutations, "a tagged distribution is adopted, not recreated")
    assert_true("update_distribution" in aws.mutations, "the adopted distribution is converged")
    row = CdnDistribution.objects.get(bucket_name=BUCKET)
    assert_eq(row.distribution_id, "EFOREIGN", "the adopted id is recorded")
    cfg = aws.distributions["EFOREIGN"]["DistributionConfig"]
    assert_true(cfg["DefaultCacheBehavior"]["TrustedKeyGroups"]["Enabled"], "the adopted default behavior is signed")
    assert_true(cfg["Origins"]["Items"][0].get("OriginAccessControlId"), "the adopted origin gains the OAC")

    # Retired (Enabled=false) and tagged: re-enabled on the next enable.
    aws.distributions["EFOREIGN"]["DistributionConfig"]["Enabled"] = False
    aws.mutations.clear()
    _provisioner(_fresh(opts.public), aws, apply=True).run()
    assert_true(aws.distributions["EFOREIGN"]["DistributionConfig"]["Enabled"], "a retired tagged distribution is re-enabled")


@th.tier("core")
@th.django_unit_test("CDN provision: check is read-only and prints the exact policy JSON for every gap")
def test_check_reports_gaps_with_exact_remedies(opts):
    from mojo.apps.fileman.models import CdnDistribution
    _reset(opts)
    aws = _FakeAws(pab=False, ownership=False)
    report = _provisioner(_fresh(opts.public), aws, apply=False).run()
    assert_eq(aws.mutations, [], "check must not mutate")
    codes = _codes(report)
    for code in ("signing_key.missing", "public_access_block.off", "ownership.not_enforced"):
        assert_true(code in codes, f"check must report {code}: {codes}")
    pab = [i for i in report["items"] if i["code"] == "public_access_block.off"][0]
    assert_true(f"{PREFIX}optout" in pab["remedy"],
                f"the Block Public Access remedy names the opted-out public manager it would break: {pab['remedy']}")

    # BYO: an operator's distribution registered by id with a custom domain.
    aws.distributions["EBYO"] = {
        "ARN": f"arn:aws:cloudfront::{ACCOUNT}:distribution/EBYO", "DomainName": "byo.cloudfront.net",
        "Status": "Deployed", "ETag": "E-b", "DistributionConfig": {
            "CallerReference": "byo", "Enabled": True, "Comment": "",
            "Origins": {"Quantity": 1, "Items": [{"Id": "o", "DomainName": f"{BUCKET}.s3.{REGION}.amazonaws.com",
                                                  "OriginAccessControlId": "OACBYO",
                                                  "S3OriginConfig": {"OriginAccessIdentity": ""}}]},
            "DefaultCacheBehavior": {"TargetOriginId": "o", "ViewerProtocolPolicy": "redirect-to-https",
                                     "AllowedMethods": {"Items": ["GET", "HEAD"]},
                                     "TrustedKeyGroups": {"Enabled": True, "Quantity": 1, "Items": ["KGBYO"]}},
            "CacheBehaviors": {"Quantity": 0, "Items": []}}}
    from mojo.apps.fileman.services import cdn_provision
    row = cdn_provision.register_byo(_fresh(opts.public), "EBYO", "https://media.example.com")
    aws.key_groups["KGBYO"] = {"Name": "mojo-fm-manual", "Items": []}
    row.key_group_id = "KGBYO"
    row.save()
    aws = _FakeAws(bucket_policy={"Version": "2012-10-17", "Statement": [
        {"Sid": "AllowPublicReadForPrefix_old", "Effect": "Allow", "Principal": "*",
         "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{BUCKET}/old/*"}]})
    aws.distributions["EBYO"] = json.loads(json.dumps(
        {"ARN": f"arn:aws:cloudfront::{ACCOUNT}:distribution/EBYO", "DomainName": "byo.cloudfront.net",
         "Status": "Deployed", "ETag": "E-b"})) | {"DistributionConfig": {
            "CallerReference": "byo", "Enabled": True, "Comment": "",
            "Origins": {"Quantity": 1, "Items": [{"Id": "o", "DomainName": f"{BUCKET}.s3.{REGION}.amazonaws.com",
                                                  "OriginAccessControlId": "OACBYO",
                                                  "S3OriginConfig": {"OriginAccessIdentity": ""}}]},
            "DefaultCacheBehavior": {"TargetOriginId": "o", "ViewerProtocolPolicy": "redirect-to-https",
                                     "AllowedMethods": {"Items": ["GET", "HEAD"]},
                                     "TrustedKeyGroups": {"Enabled": True, "Quantity": 1, "Items": ["KGBYO"]}},
            "CacheBehaviors": {"Quantity": 0, "Items": []}}}
    aws.key_groups["KGBYO"] = {"Name": "mojo-fm-manual", "Items": []}
    aws.oacs["OACBYO"] = {"Name": "theirs", "SigningBehavior": "always", "SigningProtocol": "sigv4",
                          "OriginAccessControlOriginType": "s3"}
    report = _provisioner(_fresh(opts.public), aws, apply=False).run()
    assert_eq(aws.mutations, [], "check on a BYO distribution must not mutate")
    codes = _codes(report)
    assert_true("distribution.behaviors.drift" in codes, f"BYO check reports the missing per-manager behaviors: {codes}")
    assert_true("bucket_policy.missing" in codes and "bucket_policy.public_statement" in codes,
                f"BYO check reports the missing OAC statement and the stray public one: {codes}")
    missing = [i for i in report["items"] if i["code"] == "bucket_policy.missing"][0]
    merged = json.loads(missing["remedy"].split(":\n", 1)[1])
    sids = [s["Sid"] for s in merged["Statement"]]
    assert_eq(sids, ["AllowPublicReadForPrefix_old", "AllowCloudFrontServicePrincipalReadOnly_EBYO"],
              "the printed bucket policy is the current one merged with our statement")
    assert_true("kms.missing" in codes, f"BYO check reports the missing KMS statement: {codes}")
    kms_missing = [i for i in report["items"] if i["code"] == "kms.missing"][0]
    merged = json.loads(kms_missing["remedy"].split(":\n", 1)[1])
    assert_true(any(s["Sid"] == "AllowCloudFrontDecrypt_EBYO" and s["Action"] == ["kms:Decrypt"]
                    for s in merged["Statement"]), "the printed KMS statement grants kms:Decrypt only")
    assert_true(CdnDistribution.objects.get(bucket_name=BUCKET).last_check.get("mode") == "check",
                "the findings are recorded on the row")


@th.tier("core")
@th.django_unit_test("CDN provision: the AWS-managed aws/s3 key is MANUAL with the dedicated-CMK remedy")
def test_aws_managed_key_is_manual(opts):
    _reset(opts)
    aws = _FakeAws(kms_manager="AWS")
    report = _provisioner(_fresh(opts.public), aws, apply=True).run()
    manual = [i for i in report["items"] if i["code"] == "kms.aws_managed"]
    assert_true(manual and manual[0]["status"] == "MANUAL", f"aws/s3 must be MANUAL: {report['items']}")
    assert_true("customer-managed" in manual[0]["remedy"] and "not KMS_KEY_ID" in manual[0]["remedy"],
                "the remedy recommends a dedicated media CMK, not the envelope key")
    assert_true("put_key_policy" not in aws.mutations, "no key policy write is attempted")
    assert_true(not report["converged"], "the run does not count as converged")
    from mojo.apps.fileman.models import CdnDistribution
    assert_true(not CdnDistribution.objects.filter(bucket_name=BUCKET, is_enabled=True).exists(),
                "delivery is not enabled while CloudFront cannot decrypt the objects")


@th.tier("core")
@th.django_unit_test("CDN provision: public→private tightens the edge before the save, and refuses on failure")
def test_visibility_flip_is_fail_closed(opts):
    from mojo import errors as me
    from mojo.apps.fileman.models import CdnDistribution, FileManager
    from mojo.apps.fileman.services import cdn_provision
    _reset(opts)
    aws = _FakeAws()
    _provisioner(_fresh(opts.public), aws, apply=True).run()
    row = CdnDistribution.objects.get(bucket_name=BUCKET)

    # The sync runs under the deployment identity; the fake is injected
    # through the per-instance seam on this test-owned row only.
    fm = _fresh(opts.public)
    fm._cdn_provision_kwargs = _seams(aws)
    try:
        aws.deny.add("update_distribution")
        fm.is_public = False
        err = None
        try:
            fm.on_rest_pre_save({"is_public": True}, False)
        except me.ValueException as exc:
            err = exc
        assert_true(err is not None, "a refused UpdateDistribution must refuse the public→private save")
        assert_true("signed delivery" in str(err), f"the refusal explains itself: {err}")
        assert_true(FileManager.objects.get(pk=fm.pk).is_public, "the row stays public")
        cfg = aws.distributions[row.distribution_id]["DistributionConfig"]
        behaviors = {b["PathPattern"]: b["TrustedKeyGroups"]["Enabled"] for b in cfg["CacheBehaviors"]["Items"]}
        assert_eq(behaviors[f"fileman/{PREFIX}public/*"], False, "the edge was not changed either")

        aws.deny.clear()
        fm = _fresh(opts.public)
        fm._cdn_provision_kwargs = _seams(aws)
        fm.is_public = False
        fm.on_rest_pre_save({"is_public": True}, False)
        fm.save()
        cfg = aws.distributions[row.distribution_id]["DistributionConfig"]
        behaviors = {b["PathPattern"]: b["TrustedKeyGroups"]["Enabled"] for b in cfg["CacheBehaviors"]["Items"]}
        assert_eq(behaviors[f"fileman/{PREFIX}public/*"], True, "the prefix is signed at the edge before the row is private")
        assert_true(not FileManager.objects.get(pk=fm.pk).is_public, "the row is now private")
    finally:
        FileManager.objects.filter(pk=opts.public).update(is_public=True)


@th.tier("core")
@th.django_unit_test("CDN provision: disable reverts links, retire disables the distribution, nothing is deleted")
def test_disable_and_retire(opts):
    from mojo.apps.fileman.models import CdnDistribution
    from mojo.apps.fileman.services import cdn_provision
    _reset(opts)
    aws = _FakeAws()
    _provisioner(_fresh(opts.public), aws, apply=True).run()
    assert_true(_fresh(opts.private).uses_cdn, "both managers deliver through the CDN")
    result = cdn_provision.disable(_fresh(opts.public), retire=True, **_seams(aws))
    assert_true(result["disabled"] and result["retired"], f"disable --retire reports both: {result}")
    assert_true(not _fresh(opts.private).uses_cdn, "every manager on the bucket reverts to S3 links")
    row = CdnDistribution.objects.get(bucket_name=BUCKET)
    assert_true(not row.is_enabled and row.distribution_id in aws.distributions,
                "the row is disabled and the distribution still exists")
    assert_true(not aws.distributions[row.distribution_id]["DistributionConfig"]["Enabled"],
                "retire sets Enabled=false")
    assert_true(not any(m.startswith("delete") for m in aws.mutations), "nothing is ever deleted")


@th.tier("core")
@th.django_unit_test("CDN provision: rotation adds a key and retires the old; revocation removes it now")
def test_rotate_and_revoke(opts):
    from mojo.apps.fileman.models import CdnDistribution, CdnSigningKey
    from mojo.apps.fileman.services import cdn_provision
    _reset(opts)
    aws = _FakeAws()
    _provisioner(_fresh(opts.public), aws, apply=True).run()
    row = CdnDistribution.objects.get(bucket_name=BUCKET)
    old = row.signing_key
    report = cdn_provision.rotate_key(manager=_fresh(opts.public), **_seams(aws))
    assert_true("key.rotated" in _codes(report, "PASS"), f"rotation must succeed: {report['items']}")
    row.refresh_from_db()
    old.refresh_from_db()
    assert_true(row.signing_key_id != old.pk, "the distribution now signs with the new key")
    assert_true(old.retire_after is not None and old.is_active, "the old key stays active until its links expire")
    group = aws.key_groups[row.key_group_id]["Items"]
    assert_eq(sorted(group), sorted([old.key_id, row.signing_key.key_id]), "the group holds both keys")

    result = cdn_provision.revoke_key(old.key_id, manager=_fresh(opts.public), **_seams(aws))
    assert_true(result["status"], f"revoke must succeed: {result}")
    old.refresh_from_db()
    assert_true(not old.is_active, "a revoked key is inactive")
    assert_eq(aws.key_groups[row.key_group_id]["Items"], [row.signing_key.key_id],
              "the revoked key left the key group")

    # The cron retires keys whose window passed.
    from django.utils import timezone
    from datetime import timedelta
    newer = row.signing_key
    report = cdn_provision.rotate_key(manager=_fresh(opts.public), **_seams(aws))
    newer.refresh_from_db()
    CdnSigningKey.objects.filter(pk=newer.pk).update(retire_after=timezone.now() - timedelta(seconds=1))
    retired = cdn_provision.retire_due_keys(manager=_fresh(opts.public), **_seams(aws))
    assert_eq(retired, [newer.key_id], "the cron removes keys whose retire window passed")

    # The group never grows past the CloudFront cap.
    for _ in range(3):
        cdn_provision.rotate_key(manager=_fresh(opts.public), **_seams(aws))
    report = cdn_provision.rotate_key(manager=_fresh(opts.public), **_seams(aws))
    assert_true("key_group.full" in _codes(report, "MANUAL") or len(aws.key_groups[row.key_group_id]["Items"]) <= 5,
                "rotation refuses before the key group would exceed five keys")


@th.tier("core")
@th.django_unit_test("CDN provision: manager actions gate on superuser and report no secrets")
def test_manager_actions(opts):
    from mojo import errors as me
    from mojo.models.rest import ACTIVE_REQUEST
    _reset(opts)

    class _Req:
        def __init__(self, user):
            self.user = user

    fm = _fresh(opts.public)
    token = ACTIVE_REQUEST.set(_Req(opts.member))
    try:
        err = None
        try:
            fm.on_action_enable_cdn({})
        except me.PermissionDeniedException as exc:
            err = exc
        assert_true(err is not None, "a non-superuser may not enable the CDN")
        err = None
        try:
            fm.on_action_disable_cdn({})
        except me.PermissionDeniedException as exc:
            err = exc
        assert_true(err is not None, "a non-superuser may not disable the CDN")
    finally:
        ACTIVE_REQUEST.reset(token)
    encoded = json.dumps(fm.to_dict(graph="default"), default=str)
    assert_true("PRIVATE KEY" not in encoded and "cdn-prov-secret" not in encoded,
                "the manager graph carries neither key material nor AWS secrets")


@th.tier("core")
@th.django_unit_test("CDN provision: invalidation targets the bucket's distribution")
def test_invalidation(opts):
    from mojo.apps.fileman.models import CdnDistribution
    from mojo.apps.fileman.services import cdn_provision
    _reset(opts)
    aws = _FakeAws()
    _provisioner(_fresh(opts.public), aws, apply=True).run()
    row = CdnDistribution.objects.get(bucket_name=BUCKET)
    cdn_provision.invalidate_paths(row, ["fileman/x/thumb.jpg", "/fileman/x/thumb.jpg"], **_seams(aws))
    assert_eq(aws.invalidations, [(row.distribution_id, ["/fileman/x/thumb.jpg"])],
              "paths are normalised and de-duplicated before invalidation")
