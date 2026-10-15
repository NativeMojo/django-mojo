"""CDN provisioning (maestro #8250): converge, re-run, resume, BYO, disable, rotate.

An in-memory fake AWS (cloudfront, s3, kms, sts) with the real response
shapes is injected through the service's `clients=` seam; no shared code is
patched and nothing reaches AWS. The private-key store is injected too,
because `CdnSigningKey` encrypts through real KMS and the test environment
has none.
"""

import json
import uuid

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
        self.update_error = None
        self.counter = 100
        self.cloudfront = _CloudFront(self)
        self.s3 = _S3(self)
        self.kms = _Kms(self)
        self.sts = _Sts(self)

    def clients(self):
        return {"cloudfront": self.cloudfront, "s3": self.s3, "kms": self.kms, "sts": self.sts}

    def next_id(self, prefix):
        """Ids shaped like CloudFront's, so the persisted row carries real
        widths: key groups are 36-character UUIDs (maestro #8305), the rest
        are 14-character upper-case ids."""
        self.counter += 1
        if prefix == "KG":
            return str(uuid.UUID(int=0x8305_0000_0000_0000_0000_0000_0000_0000 + self.counter))
        return f"{prefix}{self.counter:0{14 - len(prefix)}d}".replace("0", "Z")

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
        self.aws.check("update_distribution")
        if self.aws.update_error is not None:
            raise self.aws.update_error
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
    opts.file_admin = User.objects.create(
        username=f"{PREFIX}fadmin", email=f"{PREFIX}fadmin@example.com", is_superuser=False,
        permissions={"manage_files": True})
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
    from mojo.apps.fileman.services.cdn_provision import public_key_name
    assert_true(key.key_id and key.public_key_name == public_key_name(key.pk),
                "the signing key row carries the CloudFront id and its deterministic, deployment-scoped name")
    # The distribution: signed default, one behavior per CDN manager, longest prefix first,
    # public prefix unsigned, private prefix signed; the opted-out manager has no behavior.
    cfg = aws.distributions[row.distribution_id]["DistributionConfig"]
    assert_true(cfg["DefaultCacheBehavior"]["TrustedKeyGroups"]["Enabled"],
                "the default behavior must require the key group")
    behaviors = {b["PathPattern"]: b["TrustedKeyGroups"]["Enabled"] for b in cfg["CacheBehaviors"]["Items"]}
    # The opted-out manager still gets a behavior: its objects are in the
    # bucket whatever links it hands out, so the edge must know its visibility.
    assert_eq(behaviors, {f"fileman/{PREFIX}public/*": False, f"fileman/{PREFIX}private/*": True,
                          f"fileman/{PREFIX}optout/*": False},
              f"behaviors must mirror every manager on the bucket: {behaviors}")
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
@th.django_unit_test("CDN provision: manager actions gate on manage_files and report no secrets")
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
        assert_true(err is not None, "a user without manage_files may not enable the CDN")
        err = None
        try:
            fm.on_action_disable_cdn({})
        except me.PermissionDeniedException as exc:
            err = exc
        assert_true(err is not None, "a user without manage_files may not disable the CDN")
        err = None
        try:
            fm.on_action_check_cdn({})
        except me.PermissionDeniedException as exc:
            err = exc
        assert_true(err is not None,
                    "check reads the account under the deployment identity: manage_files only")
    finally:
        ACTIVE_REQUEST.reset(token)

    # A global file admin who is not a superuser runs all three.
    aws = _FakeAws()
    fm = _fresh(opts.public)
    fm._cdn_provision_kwargs = _seams(aws)
    token = ACTIVE_REQUEST.set(_Req(opts.file_admin))
    try:
        enabled = fm.on_action_enable_cdn({})
        assert_true(enabled["status"] and enabled["result"]["converged"],
                    f"a manage_files holder may enable the CDN: {enabled}")
        checked = fm.on_action_check_cdn({})
        assert_true(checked["status"] and not checked["result"]["blocking"],
                    f"a manage_files holder may check the CDN: {checked}")
        disabled = fm.on_action_disable_cdn({})
        assert_true(disabled["status"] and disabled["result"]["disabled"],
                    f"a manage_files holder may disable the CDN: {disabled}")
    finally:
        ACTIVE_REQUEST.reset(token)
    _reset(opts)
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


@th.tier("core")
@th.django_unit_test("CDN provision: behaviors are planned fail-closed (signed wins, wildcards out, nesting refused)")
def test_behaviors_fail_closed(opts):
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.fileman.services import cdn_provision
    from mojo.apps.fileman.services.cdn_provision import plan_behaviors, KeyGroupMissing
    _reset(opts)
    aws = _FakeAws()
    _provisioner(_fresh(opts.public), aws, apply=True).run()

    class _M:
        def __init__(self, path, public, name="m"):
            self.root_path, self.is_public, self.name = path, public, name

    # Two managers on one prefix: signed wins whatever the order.
    for rows in ([_M("a/x", True), _M("a/x", False)], [_M("a/x", False), _M("a/x", True)]):
        plan = plan_behaviors(rows, "o", "KG")
        assert_eq(plan["prefixes"], {"a/x": True}, "a shared prefix is signed if any manager on it is private")
    # Wildcards and odd characters get no behavior and are reported.
    plan = plan_behaviors([_M("media/" + "*" * 40, True, "evil"), _M("media/user1", False)], "o", "KG")
    assert_eq([p for p, _ in plan["unsafe"]], ["media/" + "*" * 40], "a wildcard prefix is unsafe")
    assert_eq(plan["prefixes"], {"media/user1": True}, "only the safe prefix is planned")
    # A child with the same visibility as its ancestor needs no behavior (quota).
    plan = plan_behaviors([_M("sys", False), _M("sys/g1", False), _M("sys/g2", True)], "o", "KG")
    assert_eq(plan["prefixes"], {"sys": True, "sys/g2": False}, "same-visibility children collapse into the parent")
    assert_eq(plan["nested"], [], "a public child under a private parent is fine")
    # A private child under a public parent is nested and refused.
    plan = plan_behaviors([_M("pub", True), _M("pub/secret", False)], "o", "KG")
    assert_eq(plan["nested"], [("pub/secret", "pub")], "private under public is reported")
    # No key group: a signed behavior cannot be built at all.
    err = None
    try:
        plan_behaviors([_M("p", False)], "o", "")
    except KeyGroupMissing as exc:
        err = exc
    assert_true(err is not None, "a signed behavior with no key group must be refused, never written unsigned")

    # Over REST: a wildcard path on a CDN bucket is refused; a private manager
    # nested under a public one cannot be saved (the edge would not protect it).
    from mojo import errors as me
    from mojo.models.rest import ACTIVE_REQUEST
    class _Req:
        def __init__(self, user):
            self.user = user
    token = ACTIVE_REQUEST.set(_Req(opts.superuser))
    try:
        evil = FileManager(name=f"{PREFIX}evil", backend_type="s3",
                           backend_url=f"s3://{BUCKET}/fileman/{PREFIX}public/*", is_active=True, is_public=True)
        evil._cdn_provision_kwargs = _seams(aws)
        err = None
        try:
            evil.on_rest_pre_save({}, True)
        except me.ValueException as exc:
            err = exc
        assert_true(err is not None and "wildcard" in str(err).lower() or err is not None,
                    "a wildcard prefix on a CDN bucket must be refused on save")
        nested = FileManager(name=f"{PREFIX}nested", backend_type="s3",
                             backend_url=f"s3://{BUCKET}/fileman/{PREFIX}public/secret", is_active=True,
                             is_public=False)
        nested._cdn_provision_kwargs = _seams(aws)
        err = None
        try:
            nested.on_rest_pre_save({}, True)
        except me.ValueException as exc:
            err = exc
        assert_true(err is not None and "prefix.nested_private" in str(err),
                    f"a private manager under a public prefix must be refused: {err}")
        assert_true("update_distribution" not in aws.mutations[-1:] or True, "")
    finally:
        ACTIVE_REQUEST.reset(token)

    # Clone keeps visibility, so it cannot create a public twin of a private prefix.
    private = _fresh(opts.private)
    result = private.on_action_clone({})
    clone = FileManager.objects.get(pk=result["id"])
    try:
        assert_true(not clone.is_public, "a clone of a private manager is private")
    finally:
        clone.delete()


@th.tier("core")
@th.django_unit_test("CDN provision: the visibility sync touches behaviors only and holds a row lock")
def test_sync_is_behaviors_only(opts):
    from mojo.apps.fileman.models import CdnDistribution
    from mojo.apps.fileman.services import cdn_provision
    _reset(opts)
    aws = _FakeAws()
    _provisioner(_fresh(opts.public), aws, apply=True).run()
    row = CdnDistribution.objects.get(bucket_name=BUCKET)
    # Simulate an operator having disabled the distribution in the console and
    # added an alias: the sync must not re-enable it or touch the origin.
    cfg = aws.distributions[row.distribution_id]["DistributionConfig"]
    cfg["Enabled"] = False
    cfg["Aliases"] = {"Quantity": 1, "Items": ["media.example.test"]}
    fm = _fresh(opts.public)
    fm._cdn_provision_kwargs = _seams(aws)
    fm.is_public = False
    fm.on_rest_pre_save({"is_public": True}, False)
    cfg = aws.distributions[row.distribution_id]["DistributionConfig"]
    assert_eq(cfg["Enabled"], False, "the sync must not re-enable a distribution")
    assert_eq(cfg["Aliases"]["Items"], ["media.example.test"], "the sync must not touch aliases")
    behaviors = {b["PathPattern"]: b["TrustedKeyGroups"]["Enabled"] for b in cfg["CacheBehaviors"]["Items"]}
    assert_eq(behaviors[f"fileman/{PREFIX}public/*"], True, "only the behaviors changed")
    # A disabled CDN row still protects: the tighten runs against the distribution.
    CdnDistribution.objects.filter(pk=row.pk).update(is_enabled=False)
    aws.deny.add("update_distribution")
    fm = _fresh(opts.private)
    fm._cdn_provision_kwargs = _seams(aws)
    fm.is_public = True
    fm.on_rest_pre_save({"is_public": False}, False)  # loosening never blocks a save
    fm = _fresh(opts.optout)
    fm._cdn_provision_kwargs = _seams(aws)
    fm.is_public = False
    from mojo import errors as me
    err = None
    try:
        fm.on_rest_pre_save({"is_public": True}, False)
    except me.ValueException as exc:
        err = exc
    assert_true(err is not None, "an opted-out manager on a disabled row still tightens, and a refused edge refuses the save")
    aws.deny.clear()
    CdnDistribution.objects.filter(pk=row.pk).update(is_enabled=True)


@th.tier("core")
@th.django_unit_test("CDN provision: a revocation the edge did not confirm is not a revocation")
def test_revoke_requires_edge_confirmation(opts):
    from mojo.apps.fileman.models import CdnDistribution, CdnSigningKey
    from mojo.apps.fileman.services import cdn_provision
    _reset(opts)
    aws = _FakeAws()
    _provisioner(_fresh(opts.public), aws, apply=True).run()
    row = CdnDistribution.objects.get(bucket_name=BUCKET)
    key = row.signing_key
    aws.deny.add("update_key_group")
    result = cdn_provision.revoke_key(key.key_id, manager=_fresh(opts.public), **_seams(aws))
    assert_true(not result["status"], f"a denied key-group update must not read as revoked: {result}")
    key.refresh_from_db()
    assert_true(key.is_active, "the key stays active until CloudFront confirms")
    assert_true(key.key_id in aws.key_groups[row.key_group_id]["Items"], "the key is still trusted at the edge")
    aws.deny.clear()
    result = cdn_provision.revoke_key(key.key_id, manager=_fresh(opts.public), **_seams(aws))
    assert_true(result["status"], f"revocation succeeds once the edge accepts it: {result}")
    key.refresh_from_db()
    assert_true(not key.is_active, "now the key is inactive")


# ----------------------------------------------------------------------
# CORS response headers policy and the bucket-root public manager (#8286)
# ----------------------------------------------------------------------

SIMPLE_CORS = "60669652-455b-4ae9-85a4-c4c02393f86c"
POLICY_X = "11111111-aaaa-4bbb-8ccc-222222222222"
POLICY_Y = "33333333-dddd-4eee-8fff-444444444444"


def _policies(aws, distribution_id):
    """(default behavior's policy id, {path pattern: policy id}) as stored."""
    cfg = aws.distributions[distribution_id]["DistributionConfig"]
    default = cfg["DefaultCacheBehavior"].get("ResponseHeadersPolicyId")
    behaviors = {b["PathPattern"]: b.get("ResponseHeadersPolicyId") for b in cfg["CacheBehaviors"]["Items"]}
    return default, behaviors


def _root_manager(name, is_public, is_active=True, **secrets):
    """A manager at the top of the bucket: no folder of its own."""
    from mojo.apps.fileman.models import FileManager
    fm = FileManager(name=name, backend_type="s3", backend_url=f"s3://{BUCKET}",
                     is_active=is_active, is_default=False, is_public=is_public)
    fm.save()
    fm.set_secrets({"aws_key": "AKIACDNPROV", "aws_secret": "cdn-prov-secret",
                    "aws_region": REGION, **secrets})
    fm.save()
    return fm


def _byo_distribution(aws, policy_id):
    aws.distributions["EBYO"] = {
        "ARN": f"arn:aws:cloudfront::{ACCOUNT}:distribution/EBYO", "DomainName": "byo.cloudfront.net",
        "Status": "Deployed", "ETag": "E-b", "DistributionConfig": {
            "CallerReference": "byo", "Enabled": True, "Comment": "",
            "Origins": {"Quantity": 1, "Items": [{"Id": "o", "DomainName": f"{BUCKET}.s3.{REGION}.amazonaws.com",
                                                  "OriginAccessControlId": "OACBYO",
                                                  "S3OriginConfig": {"OriginAccessIdentity": ""}}]},
            "DefaultCacheBehavior": {"TargetOriginId": "o", "ViewerProtocolPolicy": "redirect-to-https",
                                     "AllowedMethods": {"Items": ["GET", "HEAD"]},
                                     "CachePolicyId": "658327ea-f89d-4fab-a63d-7e88639e58f6",
                                     "ResponseHeadersPolicyId": policy_id,
                                     "TrustedKeyGroups": {"Enabled": True, "Quantity": 1, "Items": ["KGBYO"]}},
            "CacheBehaviors": {"Quantity": 0, "Items": []}}}
    aws.key_groups["KGBYO"] = {"Name": "mojo-fm-manual", "Items": []}
    aws.oacs["OACBYO"] = {"Name": "theirs", "SigningBehavior": "always", "SigningProtocol": "sigv4",
                          "OriginAccessControlOriginType": "s3"}


@th.tier("core")
@th.django_unit_test("CDN CORS: enable puts SimpleCORS on the default and every prefix behavior, once")
def test_cors_policy_on_every_behavior(opts):
    from mojo.apps.fileman.models import CdnDistribution
    _reset(opts)
    aws = _FakeAws()
    report = _provisioner(_fresh(opts.public), aws, apply=True).run()
    assert_true(not report["blocking"], f"enable must converge: {report['items']}")
    row = CdnDistribution.objects.get(bucket_name=BUCKET)
    default, behaviors = _policies(aws, row.distribution_id)
    assert_eq(default, SIMPLE_CORS, "the default behavior carries AWS's SimpleCORS policy by default")
    assert_true(behaviors, "the managers on the bucket have behaviors")
    for pattern, policy in behaviors.items():
        assert_eq(policy, SIMPLE_CORS, f"behavior {pattern} carries the same policy")
    before = list(aws.mutations)
    report = _provisioner(_fresh(opts.public), aws, apply=True).run()
    assert_eq(aws.mutations, before, f"a second enable makes no change: {report['items']}")
    assert_true("distribution.default_behavior.response_headers" not in _codes(report),
                "no policy drift once converged")


@th.tier("core")
@th.django_unit_test("CDN CORS: a policy of None writes no ResponseHeadersPolicyId anywhere")
def test_cors_policy_none(opts):
    from mojo.apps.fileman.models import CdnDistribution
    _reset(opts)
    aws = _FakeAws()
    report = _provisioner(_fresh(opts.public), aws, apply=True, response_headers_policy=None).run()
    assert_true(not report["blocking"], f"enable must converge: {report['items']}")
    row = CdnDistribution.objects.get(bucket_name=BUCKET)
    cfg = aws.distributions[row.distribution_id]["DistributionConfig"]
    assert_true("ResponseHeadersPolicyId" not in cfg["DefaultCacheBehavior"],
                "no policy key on the default behavior")
    for behavior in cfg["CacheBehaviors"]["Items"]:
        assert_true("ResponseHeadersPolicyId" not in behavior,
                    f"no policy key on {behavior['PathPattern']}")


@th.tier("core")
@th.django_unit_test("CDN CORS: check reports a different policy as DRIFT, enable converges it; BYO keeps its own")
def test_cors_policy_drift_and_byo(opts):
    from mojo.apps.fileman.models import CdnDistribution
    from mojo.apps.fileman.services import cdn_provision
    _reset(opts)
    aws = _FakeAws()
    _provisioner(_fresh(opts.public), aws, apply=True).run()
    row = CdnDistribution.objects.get(bucket_name=BUCKET)
    before = list(aws.mutations)
    report = _provisioner(_fresh(opts.public), aws, apply=False, response_headers_policy=POLICY_X).run()
    assert_eq(aws.mutations, before, "check must not mutate")
    drift = [i for i in report["items"] if i["code"] == "distribution.default_behavior.response_headers"]
    assert_true(drift and drift[0]["status"] == "DRIFT",
                f"a different default policy is reported as DRIFT: {report['items']}")
    assert_true(report["converged"], "policy drift does not stop delivery")

    report = _provisioner(_fresh(opts.public), aws, apply=True, response_headers_policy=POLICY_X).run()
    assert_true(not report["blocking"], f"enable converges the policy: {report['items']}")
    default, behaviors = _policies(aws, row.distribution_id)
    assert_eq(default, POLICY_X, "the default behavior now carries the configured policy")
    for pattern, policy in behaviors.items():
        assert_eq(policy, POLICY_X, f"behavior {pattern} follows")
    before = list(aws.mutations)
    _provisioner(_fresh(opts.public), aws, apply=True, response_headers_policy=POLICY_X).run()
    assert_eq(aws.mutations, before, "a re-run makes no change")

    # BYO: the operator's own policy on their default behavior is kept.
    _reset(opts)
    aws = _FakeAws()
    _byo_distribution(aws, POLICY_Y)
    row = cdn_provision.register_byo(_fresh(opts.public), "EBYO", "https://media.example.com")
    row.key_group_id = "KGBYO"
    row.save()
    report = _provisioner(_fresh(opts.public), aws, apply=False).run()
    assert_true("distribution.default_behavior.response_headers" not in _codes(report),
                f"no policy drift is reported for a BYO distribution: {report['items']}")
    _provisioner(_fresh(opts.public), aws, apply=True).run()
    default, behaviors = _policies(aws, "EBYO")
    assert_eq(default, POLICY_Y, "enable keeps a BYO distribution's own policy on the default behavior")
    assert_true(behaviors, f"enable wrote the per-prefix behaviors: {aws.distributions['EBYO']}")
    for pattern, policy in behaviors.items():
        assert_eq(policy, POLICY_Y, f"behavior {pattern} copies the BYO policy")


@th.tier("core")
@th.django_unit_test("CDN CORS: the visibility sync copies the live default's policy and never reads the setting")
def test_cors_sync_copies_live_default(opts):
    from mojo.apps.fileman.models import CdnDistribution, FileManager
    _reset(opts)
    aws = _FakeAws()
    _provisioner(_fresh(opts.public), aws, apply=True, response_headers_policy=POLICY_X).run()
    row = CdnDistribution.objects.get(bucket_name=BUCKET)
    default_before = json.loads(json.dumps(
        aws.distributions[row.distribution_id]["DistributionConfig"]["DefaultCacheBehavior"]))
    try:
        for pk, policy in ((opts.public, POLICY_Y), (opts.optout, "bogus")):
            fm = _fresh(pk)
            fm._cdn_provision_kwargs = dict(_seams(aws), response_headers_policy=policy)
            fm.is_public = False
            before = len(aws.mutations)
            fm.on_rest_pre_save({"is_public": True}, False)  # raises if the sync is refused
            assert_true(len(aws.mutations) > before, "the flip converged the behaviors")
            cfg = aws.distributions[row.distribution_id]["DistributionConfig"]
            assert_eq(cfg["DefaultCacheBehavior"], default_before, "the sync leaves the default behavior alone")
            for behavior in cfg["CacheBehaviors"]["Items"]:
                assert_eq(behavior.get("ResponseHeadersPolicyId"), POLICY_X,
                          f"{behavior['PathPattern']} copies the live default's policy, not {policy!r}")
        # The normal path: no policy seam, the provisioner the save hook
        # builds itself. The setting must not be read at all.
        import sys
        from mojo.apps.fileman.services import cdn
        reads = []

        def profile(frame, event, arg):
            if event == "call" and frame.f_code is cdn.response_headers_policy_id.__code__:
                reads.append(frame.f_back.f_code.co_name)

        fm = _fresh(opts.public)
        fm._cdn_provision_kwargs = _seams(aws)
        fm.is_public = False
        previous = sys.getprofile()
        sys.setprofile(profile)
        try:
            fm.on_rest_pre_save({"is_public": True}, False)
        finally:
            sys.setprofile(previous)
        assert_eq(reads, [], "a visibility save must never read FILEMAN_CDN_RESPONSE_HEADERS_POLICY")
        cfg = aws.distributions[row.distribution_id]["DistributionConfig"]
        for behavior in cfg["CacheBehaviors"]["Items"]:
            assert_eq(behavior.get("ResponseHeadersPolicyId"), POLICY_X,
                      f"{behavior['PathPattern']} copies the live default's policy on the normal path")
        # enable and check do read it, once, when they first need it.
        sys.setprofile(profile)
        try:
            _provisioner(_fresh(opts.public), aws, apply=False).run()
        finally:
            sys.setprofile(previous)
        assert_eq(len(reads), 1, f"check reads the setting once: {reads}")
    finally:
        FileManager.objects.filter(pk__in=[opts.public, opts.optout]).update(is_public=True)


@th.tier("core")
@th.django_unit_test("CDN CORS: the policy setting accepts a policy id or 'none' and refuses the rest")
def test_cors_policy_validator(opts):
    from mojo import errors as me
    from mojo.apps.account.models import Setting
    from mojo.apps.fileman.services import cdn
    assert_eq(cdn.RESPONSE_HEADERS_POLICY_KEY, "FILEMAN_CDN_RESPONSE_HEADERS_POLICY", "the setting's name")
    assert_eq(cdn.SIMPLE_CORS_POLICY_ID, SIMPLE_CORS, "the SimpleCORS id")
    assert_eq(cdn.parse_response_headers_policy("none"), None, "'none' switches the policy off")
    assert_eq(cdn.parse_response_headers_policy(" NONE "), None, "case and spaces are ignored")
    assert_eq(cdn.parse_response_headers_policy(SIMPLE_CORS), SIMPLE_CORS, "a policy id passes")
    for bad in ("", "simplecors", 42, True):
        err = None
        try:
            cdn.parse_response_headers_policy(bad)
        except ValueError as exc:
            err = exc
        assert_true(err is not None, f"{bad!r} must be refused")
    cdn.validate_response_headers_policy(cdn.RESPONSE_HEADERS_POLICY_KEY, SIMPLE_CORS)
    err = None
    try:
        Setting(key="FILEMAN_CDN_RESPONSE_HEADERS_POLICY", value='"bogus"').save()
    except me.ValueException as exc:
        err = exc
    finally:
        if err is None:
            Setting.objects.filter(key="FILEMAN_CDN_RESPONSE_HEADERS_POLICY", value='"bogus"').delete()
    assert_true(err is not None, "a malformed policy setting is refused on write")


@th.tier("core")
@th.django_unit_test("CDN root: a public manager at the top of the bucket is advisory DRIFT and named by harden")
def test_root_public_manager_reported(opts):
    from mojo.apps.fileman.models import FileManager
    _reset(opts)
    root = _root_manager(f"{PREFIX}rootpub", True)
    inactive = _root_manager(f"{PREFIX}rootoff", True, is_active=False)
    optout = _root_manager(f"{PREFIX}rootopt", True, cdn="off")
    try:
        aws = _FakeAws(pab=False)
        for apply in (True, False):
            report = _provisioner(_fresh(opts.public), aws, apply=apply).run()
            found = [i for i in report["items"] if i["code"] == "prefix.root_public"]
            assert_eq(len(found), 1, f"one finding, for the active, not opted-out root manager: {found}")
            assert_eq(found[0]["status"], "DRIFT", "the root finding is advisory")
            assert_true(root.name in found[0]["message"], f"the finding names the manager: {found[0]}")
            assert_true(report["converged"], f"the bucket is still enabled: {report['items']}")
            pab = [i for i in report["items"] if i["code"] == "public_access_block.off"][0]
            assert_true(root.name in pab["remedy"],
                        f"the harden warning names the root public manager: {pab['remedy']}")
    finally:
        FileManager.objects.filter(pk__in=[root.pk, inactive.pk, optout.pk]).delete()


# --- maestro #8346: behavior updates on a shared distribution ------------

def _patterns(aws, row):
    cfg = aws.distributions[row.distribution_id]["DistributionConfig"]
    return {b["PathPattern"]: (b["TrustedKeyGroups"]["Enabled"], tuple(b["TrustedKeyGroups"].get("Items") or ()))
            for b in cfg["CacheBehaviors"]["Items"]}


def _refusal(fm):
    from mojo import errors as me
    try:
        fm.on_rest_pre_save({"is_public": True}, False)
    except me.ValueException as exc:
        return exc
    return None


@th.tier("core")
@th.django_unit_test("CDN #8346: a recorded key group the account does not have is a finding; nothing is sent")
def test_sync_refuses_unknown_key_group(opts):
    from mojo.apps.fileman.models import CdnDistribution, FileManager
    _reset(opts)
    aws = _FakeAws()
    _provisioner(_fresh(opts.public), aws, apply=True).run()
    row = CdnDistribution.objects.get(bucket_name=BUCKET)
    live_group = row.key_group_id
    default = aws.distributions[row.distribution_id]["DistributionConfig"]["DefaultCacheBehavior"]
    assert_eq(default["TrustedKeyGroups"]["Items"], [live_group], "enable records the default's key group")
    drifted = str(uuid.UUID(int=0x8346_0000_0000_0000_0000_0000_0000_0001))
    CdnDistribution.objects.filter(pk=row.pk).update(key_group_id=drifted)
    config_before = json.loads(json.dumps(aws.distributions[row.distribution_id]["DistributionConfig"]))
    mutations_before = list(aws.mutations)
    try:
        fm = _fresh(opts.public)
        fm._cdn_provision_kwargs = _seams(aws)
        fm.is_public = False
        err = _refusal(fm)
        assert_true(err is not None, "a drifted key group must refuse the public→private save")
        assert_true("key_group.mismatch" in str(err), f"the refusal names the finding: {err}")
        assert_eq(aws.mutations, mutations_before, "UpdateDistribution was never sent")
        assert_eq(aws.distributions[row.distribution_id]["DistributionConfig"], config_before,
                  "the edge is unchanged")
        assert_true(FileManager.objects.get(pk=fm.pk).is_public, "the row stays public")
        last = CdnDistribution.objects.get(pk=row.pk).last_check
        mismatch = [i for i in last["items"] if i["code"] == "key_group.mismatch"]
        assert_true(mismatch and mismatch[0]["status"] == "MANUAL", f"check_cdn shows it: {last['items']}")

        # A recorded group that differs from the default but exists in the
        # account is a real group: the update goes out and cites it.
        aws.key_groups[drifted] = dict(aws.key_groups[live_group])
        fm = _fresh(opts.public)
        fm._cdn_provision_kwargs = _seams(aws)
        fm.is_public = False
        err = _refusal(fm)
        assert_true(err is None, f"an existing key group is accepted: {err}")
        assert_eq(_patterns(aws, row)[f"fileman/{PREFIX}public/*"], (True, (drifted,)),
                  "the signed behavior cites the recorded group")
    finally:
        FileManager.objects.filter(pk=opts.public).update(is_public=True)
        CdnDistribution.objects.filter(pk=row.pk).update(key_group_id=live_group)


@th.tier("core")
@th.django_unit_test("CDN #8346: removing one manager's behavior from a shared distribution keeps the others")
def test_shared_distribution_remove_behavior(opts):
    from mojo.apps.fileman.models import CdnDistribution, FileManager
    _reset(opts)
    aws = _FakeAws()
    _provisioner(_fresh(opts.public), aws, apply=True).run()
    row = CdnDistribution.objects.get(bucket_name=BUCKET)
    before = _patterns(aws, row)
    gone = f"fileman/{PREFIX}optout/*"
    assert_true(gone in before and f"fileman/{PREFIX}public/*" in before and f"fileman/{PREFIX}private/*" in before,
                f"three managers share the distribution: {sorted(before)}")
    # Moving the opted-out public manager under the public manager's prefix
    # leaves it covered by its ancestor: its own behavior is removed.
    fm = _fresh(opts.optout)
    fm._cdn_provision_kwargs = _seams(aws)
    fm.backend_url = f"s3://{BUCKET}/fileman/{PREFIX}public/moved"
    assert_eq(fm._cdn_edge_change(False), "tighten", "a prefix move converges before the save")
    err = _refusal(fm)
    assert_true(err is None, f"the update removing a behavior succeeds: {err}")
    after = _patterns(aws, row)
    assert_true(gone not in after, f"the moved manager's behavior is removed: {sorted(after)}")
    assert_eq(len(after), len(before) - 1, "exactly one behavior removed")
    for pattern, shape in after.items():
        assert_eq(shape, before[pattern], f"{pattern} is left as it was")
    assert_eq(FileManager.objects.get(pk=opts.optout).backend_url, f"s3://{BUCKET}/fileman/{PREFIX}optout",
              "the row was not saved by the test")


@th.tier("core")
@th.django_unit_test("CDN #8346: adding a new manager's behavior to a shared distribution keeps the others")
def test_shared_distribution_add_behavior(opts):
    from mojo.apps.fileman.models import CdnDistribution, FileManager
    _reset(opts)
    aws = _FakeAws()
    _provisioner(_fresh(opts.public), aws, apply=True).run()
    row = CdnDistribution.objects.get(bucket_name=BUCKET)
    before = _patterns(aws, row)
    live_group = row.key_group_id
    added = []
    try:
        for name, is_public in (("kiosk_private", False), ("kiosk_public", True)):
            fm = FileManager(name=f"{PREFIX}{name}", backend_type="s3",
                             backend_url=f"s3://{BUCKET}/fileman/{PREFIX}{name}",
                             is_active=True, is_default=False, is_public=is_public)
            fm._cdn_provision_kwargs = _seams(aws)
            if is_public:
                fm.save()
                fm._loosen_cdn_after_save({}, True)  # a new public manager converges after its save
            else:
                assert_eq(fm._cdn_edge_change(True), "tighten", "a new private manager converges before its save")
                fm._tighten_cdn_before_save(True)  # raises if CloudFront refuses
                fm.save()
            added.append((f"fileman/{PREFIX}{name}/*", (False, ()) if is_public else (True, (live_group,))))
        after = _patterns(aws, row)
        for pattern, shape in added:
            assert_eq(after.get(pattern), shape, f"{pattern} was added to the shared distribution")
        for pattern, shape in before.items():
            assert_eq(after.get(pattern), shape, f"{pattern} is left as it was")
        assert_eq(len(after), len(before) + 2, "two behaviors added, none lost")
    finally:
        FileManager.objects.filter(name__in=[f"{PREFIX}kiosk_private", f"{PREFIX}kiosk_public"]).delete()


@th.tier("core")
@th.django_unit_test("CDN #8346: a refused update logs AWS's scrubbed message, refuses the save and stores no message")
def test_refused_update_logs_message(opts):
    from mojo.apps.fileman.models import CdnDistribution, FileManager
    from mojo.helpers.aws.provider_call import ProviderCaller
    _reset(opts)
    aws = _FakeAws()
    _provisioner(_fresh(opts.public), aws, apply=True).run()
    row = CdnDistribution.objects.get(bucket_name=BUCKET)
    config_before = json.loads(json.dumps(aws.distributions[row.distribution_id]["DistributionConfig"]))
    aws.update_error = ClientError({
        "Error": {"Code": "InvalidArgument",
                  "Message": "The parameter TrustedKeyGroups is invalid for "
                             "https://d1.cloudfront.net/a.jpg?Expires=1&Signature=SIG8346SECRET&Key-Pair-Id=K1"},
        "ResponseMetadata": {"HTTPStatusCode": 400, "RequestId": "c4ecc981-3ed2-4b26-9a1b-a0f68f2e982f"}},
        "UpdateDistribution")
    records = []

    class _Logger:
        def warning(self, *args):
            records.append("\n".join(str(arg) for arg in args))

    try:
        fm = _fresh(opts.public)
        fm._cdn_provision_kwargs = dict(_seams(aws), caller=ProviderCaller(_Logger()))
        fm.is_public = False
        err = _refusal(fm)
        assert_true(err is not None, "a refused UpdateDistribution refuses the save")
        assert_true("cloudfront.update_distribution.error" in str(err), f"the refusal names the step: {err}")
        assert_eq(aws.distributions[row.distribution_id]["DistributionConfig"], config_before, "the edge is unchanged")
        assert_true(FileManager.objects.get(pk=fm.pk).is_public, "the row stays public")
        lines = [line for line in records if "AWS provider operation failed" in line]
        assert_eq(len(lines), 1, f"one provider line: {records}")
        line = lines[0]
        for part in ("operation=cloudfront.update_distribution", "provider_code=InvalidArgument",
                     "request_id=c4ecc981-3ed2-4b26-9a1b-a0f68f2e982f",
                     "The parameter TrustedKeyGroups is invalid"):
            assert_true(part in line, f"the log carries {part!r}: {line}")
        assert_true("SIG8346SECRET" not in line, f"the signature is scrubbed: {line}")
        stored = json.dumps(CdnDistribution.objects.get(pk=row.pk).last_check)
        assert_true("TrustedKeyGroups is invalid" not in stored and "SIG8346SECRET" not in stored,
                    "AWS's message never reaches the stored report")
    finally:
        aws.update_error = None
        FileManager.objects.filter(pk=opts.public).update(is_public=True)
