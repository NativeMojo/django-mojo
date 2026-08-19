"""Convergent AWS setup used by the Admin System Setup registry."""

import json
import re
import secrets
from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mojo.apps.account.services import system_readiness, system_settings
from mojo.apps.aws.services.aws_check import (
    AWSCheckRunner, OWNERSHIP_TAGS, SYSTEM_FILEMAN_ALLOWED_ORIGINS,
    _alarm_name, _cors_supports_direct_upload, _direct_upload_cors_rule,
)
from mojo.apps.aws.services.email_templates import install_missing, shipped_status
from mojo.helpers.aws.client import get_client
from mojo.helpers.aws.provider_call import ProviderCallError, ProviderCaller
from mojo.helpers.settings import settings


PRIVATE_BLOCK = {
    "BlockPublicAcls": True,
    "IgnorePublicAcls": True,
    "BlockPublicPolicy": True,
    "RestrictPublicBuckets": True,
}
_PROBE_CHALLENGE = re.compile(r"^[a-f0-9]{32}$")


def _region():
    return settings.get_static("AWS_REGION", "us-east-1") or "us-east-1"


def _identity():
    identity = system_settings.read_installation_identity()
    if not identity:
        raise system_readiness.DefinitiveSetupFailure(
            "Freeze the installation identity before configuring AWS resources")
    return identity


def delivery_probe_alarm_name(challenge):
    identity = system_settings.read_installation_identity()
    slug = identity["slug"] if identity else "unconfigured"
    challenge = str(challenge or "")
    if not _PROBE_CHALLENGE.fullmatch(challenge):
        raise ValueError("A valid setup delivery challenge is required")
    return f"django-mojo/{slug}/delivery-probe/{challenge}"[:255]


def _active_probe_identity():
    from mojo.apps.account.models import SystemSetupOperation
    operation = SystemSetupOperation.objects.filter(
        mode="fix", status__in=SystemSetupOperation.ACTIVE_STATUSES,
    ).order_by("created").first()
    if operation is None or operation.cursor >= len(operation.steps or []):
        return None
    step = (operation.steps or [])[operation.cursor]
    if step.get("id") != "section:aws_monitoring":
        return None
    challenge = str(step.get("probe_challenge") or "")
    cutoff = parse_datetime(str(step.get("probe_cutoff") or ""))
    if not _PROBE_CHALLENGE.fullmatch(challenge) or cutoff is None:
        return None
    return {"challenge": challenge, "cutoff": cutoff, "operation": operation}


def is_owned_delivery_probe(envelope, data):
    """Classify only an active per-operation challenge or its exact duplicate."""
    identity = system_settings.read_installation_identity()
    if not identity:
        return False
    topic_arn = str(envelope.get("TopicArn") or "")
    alarm_arn = str(data.get("alarm_arn") or "")
    message_id = str(envelope.get("MessageId") or "")
    if message_id:
        from mojo.apps.aws.models import CloudWatchAlarmTransition
        if CloudWatchAlarmTransition.objects.filter(
                topic_arn=topic_arn, sns_message_id=message_id,
                alarm__alarm_arn=alarm_arn, is_delivery_probe=True).exists():
            return True
    active = _active_probe_identity()
    if not active:
        return False
    state_changed_at = data.get("state_changed_at")
    if (state_changed_at is None or not hasattr(state_changed_at, "tzinfo")
            or state_changed_at.tzinfo is None
            or state_changed_at < active["cutoff"]):
        return False
    expected_name = delivery_probe_alarm_name(active["challenge"])
    if data.get("alarm_name") != expected_name:
        return False
    topic = topic_arn.split(":", 5)
    alarm = alarm_arn.split(":", 5)
    if len(topic) != 6 or len(alarm) != 6:
        return False
    expected_topic = f"django-mojo-{identity['slug']}-operations"
    expected_alarm_resource = f"alarm:{expected_name}"
    return bool(
        topic[0] == alarm[0] == "arn" and topic[1] == alarm[1]
        and topic[1] in ("aws", "aws-us-gov", "aws-cn")
        and topic[2] == "sns" and alarm[2] == "cloudwatch"
        and topic[3] == alarm[3] == data.get("region") == _region()
        and topic[4] == alarm[4] == data.get("account")
        and topic[5] == expected_topic and alarm[5] == expected_alarm_resource
        and topic_arn in (system_settings.get_value(
            system_settings.MONITORING_TOPICS, []) or [])
    )


def _policy_statements(policy):
    if not isinstance(policy, dict):
        return None
    statements = policy.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    if not isinstance(statements, list) or any(
            not isinstance(statement, dict) for statement in statements):
        return None
    return statements


def _actions(statement):
    actions = statement.get("Action", [])
    actions = [actions] if isinstance(actions, str) else actions
    return [str(action).lower() for action in actions] if isinstance(actions, list) else []


def _s3_policy_is_safe(policy, account):
    statements = _policy_statements(policy)
    if statements is None:
        return False
    for statement in statements:
        if str(statement.get("Effect") or "").lower() != "allow":
            continue
        if ("NotPrincipal" in statement or "NotAction" in statement
                or "NotResource" in statement or not _actions(statement)):
            return False
        if not any(action == "*" or action == "s3:*" or action.startswith("s3:")
                   for action in _actions(statement)):
            continue
        principal = statement.get("Principal")
        if not isinstance(principal, dict) or set(principal) != {"AWS"}:
            return False
        values = principal["AWS"]
        values = [values] if isinstance(values, str) else values
        if not isinstance(values, list) or not values:
            return False
        for value in values:
            value = str(value)
            if value == "*":
                return False
            parts = value.split(":", 5)
            resource = parts[5] if len(parts) == 6 else ""
            if (len(parts) < 6 or parts[2] != "iam" or parts[4] != account
                    or not (resource == "root" or resource.startswith("role/")
                            or resource.startswith("user/"))):
                return False
    return True


def _condition_value(statement, operator, key):
    condition = statement.get("Condition")
    if not isinstance(condition, dict):
        return None
    values = condition.get(operator)
    return values.get(key) if isinstance(values, dict) else None


def _sns_publish_policy_is_safe(policy, account, region, slug, topic_arn):
    statements = _policy_statements(policy)
    if statements is None:
        return False
    arn_parts = str(topic_arn or "").split(":", 5)
    partition = arn_parts[1] if len(arn_parts) == 6 else ""
    if partition not in ("aws", "aws-us-gov", "aws-cn"):
        return False
    source_prefix = (
        f"arn:{partition}:cloudwatch:{region}:{account}:alarm:django-mojo/{slug}/")
    for statement in statements:
        if statement.get("Sid") == "DjangoMojoCloudWatchPublish":
            continue
        if str(statement.get("Effect") or "").lower() != "allow":
            continue
        actions = _actions(statement)
        if "NotAction" in statement or "NotResource" in statement or not actions:
            return False
        # A topic policy is an authority boundary, not merely a Publish ACL.
        # Validate every Allow so adoption cannot preserve a public Subscribe,
        # SetTopicAttributes, AddPermission, or other administrative foothold.
        if any(not re.fullmatch(r"sns:(?:\*|[A-Za-z][A-Za-z0-9]*)", action)
               for action in actions):
            return False
        if "NotPrincipal" in statement:
            return False
        resources = statement.get("Resource", [])
        resources = [resources] if isinstance(resources, str) else resources
        if not isinstance(resources, list) or resources != [topic_arn]:
            return False
        principal = statement.get("Principal")
        if not isinstance(principal, dict) or len(principal) != 1:
            return False
        if "AWS" in principal:
            values = principal["AWS"]
            values = [values] if isinstance(values, str) else values
            if not isinstance(values, list) or not values:
                return False
            for value in values:
                value = str(value)
                if value == "*":
                    owner = _condition_value(
                        statement, "StringEquals", "AWS:SourceOwner")
                    if str(owner or "") != account:
                        return False
                    continue
                parts = value.split(":", 5)
                resource = parts[5] if len(parts) == 6 else ""
                if (len(parts) != 6 or parts[0] != "arn" or parts[1] != partition
                        or parts[2] != "iam" or parts[4] != account
                        or not (resource == "root" or resource.startswith("role/")
                                or resource.startswith("user/"))):
                    return False
        elif principal.get("Service") == "cloudwatch.amazonaws.com":
            if actions != ["sns:publish"]:
                return False
            source_account = _condition_value(
                statement, "StringEquals", "AWS:SourceAccount")
            source_arn = (_condition_value(statement, "ArnLike", "AWS:SourceArn")
                          or _condition_value(
                              statement, "ArnEquals", "AWS:SourceArn"))
            if (str(source_account or "") != account
                    or not isinstance(source_arn, str)
                    or not source_arn.startswith(source_prefix)
                    or source_arn == source_prefix
                    or "*" in source_arn[:-1]
                    or ("*" in source_arn and source_arn != source_prefix + "*")):
                return False
        else:
            return False
    return True


_ALARM_COMPARE_FIELDS = (
    "Namespace", "MetricName", "Statistic", "ComparisonOperator", "Threshold",
    "Period", "EvaluationPeriods", "DatapointsToAlarm", "TreatMissingData",
    "ActionsEnabled", "AlarmActions", "OKActions", "InsufficientDataActions",
)


def _alarm_matches(current, desired):
    if not isinstance(current, dict):
        return False
    current_dimensions = sorted(
        (str(row.get("Name")), str(row.get("Value")))
        for row in current.get("Dimensions", []) if isinstance(row, dict))
    desired_dimensions = sorted(
        (str(row.get("Name")), str(row.get("Value")))
        for row in desired.get("Dimensions", []) if isinstance(row, dict))
    if current_dimensions != desired_dimensions:
        return False
    if not all(current.get(key) == desired.get(key) for key in _ALARM_COMPARE_FIELDS):
        return False
    normalize_unit = lambda value: None if value in (None, "", "None") else value
    return normalize_unit(current.get("Unit")) == normalize_unit(desired.get("Unit"))


def _complete_alarm_config(alarm, topic_arn):
    desired = dict(alarm)
    desired.update({
        "ActionsEnabled": True,
        "AlarmActions": [topic_arn],
        "OKActions": [topic_arn],
        "InsufficientDataActions": [],
    })
    if desired.get("Unit") in (None, "", "None"):
        desired.pop("Unit", None)
    return desired


class AWSSetupService:
    def __init__(self, clients=None, client_factory=None, provider=None, region=None):
        self.clients = clients or {}
        self.client_factory = client_factory or get_client
        self.provider = provider or ProviderCaller()
        self.region = region or _region()

    def client(self, service):
        if service in self.clients:
            return self.clients[service]
        return self.client_factory(service, region=self.region)

    def call(self, operation, callback, iam_action="", mutation=False):
        return self.provider.call(operation, callback, iam_action, mutation)

    def _list_paginated(self, client, method, result_key, operation, iam_action,
                        **params):
        rows = []
        token = None
        seen = set()
        for _ in range(100):
            request = dict(params)
            if token:
                request["NextToken"] = token
            response = self.call(
                operation, lambda request=request: getattr(client, method)(**request),
                iam_action)
            rows.extend(response.get(result_key, []) or [])
            token = response.get("NextToken")
            if not token:
                return rows
            if token in seen:
                break
            seen.add(token)
        raise system_readiness.DefinitiveSetupFailure(
            f"{operation} did not return a complete bounded inventory")

    def _desired_monitoring_alarms(self, cloudwatch):
        runner_clients = dict(self.clients)
        for service in ("ec2", "rds", "elasticache", "elbv2"):
            runner_clients.setdefault(service, self.client(service))
        runner = AWSCheckRunner(
            region=self.region, clients=runner_clients, provider=self.provider)
        desired = runner._desired_alarms() + runner._desired_deployment_alarms(cloudwatch)
        failed = next((row for row in runner.results if row.get("status") == "fail"), None)
        if failed:
            details = failed.get("details") if isinstance(failed.get("details"), dict) else {}
            raise ProviderCallError(
                details.get("operation") or "cloudwatch.discover_alarm_profile",
                details.get("provider_code") or "provider_error",
                details.get("iam_action") or "")
        return desired

    def identity(self):
        return self.call(
            "sts.get_caller_identity", self.client("sts").get_caller_identity,
            "sts:GetCallerIdentity")

    def _bucket_tags(self, s3, bucket):
        try:
            rows = self.call(
                "s3.get_bucket_tagging",
                lambda: s3.get_bucket_tagging(Bucket=bucket),
                "s3:GetBucketTagging").get("TagSet", [])
        except ProviderCallError as exc:
            if exc.provider_code in ("NoSuchTagSet", "NoSuchTagSetError"):
                return {}
            raise
        return {str(row.get("Key")): str(row.get("Value")) for row in rows
                if row.get("Key") is not None}

    def _bucket_policy(self, s3, bucket):
        try:
            raw = self.call(
                "s3.get_bucket_policy", lambda: s3.get_bucket_policy(Bucket=bucket),
                "s3:GetBucketPolicy").get("Policy", "")
        except ProviderCallError as exc:
            if exc.provider_code == "NoSuchBucketPolicy":
                return {}
            raise
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            raise system_readiness.DefinitiveSetupFailure(
                "The selected bucket policy could not be classified safely")

    def _list_buckets(self, s3):
        rows = []
        owner_id = None
        token = None
        seen = set()
        for _ in range(100):
            request = {"ContinuationToken": token} if token else {}
            response = self.call(
                "s3.list_buckets", lambda request=request: s3.list_buckets(**request),
                "s3:ListAllMyBuckets")
            owner = response.get("Owner") if isinstance(response.get("Owner"), dict) else {}
            current_owner = str(owner.get("ID") or "")
            if not current_owner or (owner_id and owner_id != current_owner):
                raise system_readiness.DefinitiveSetupFailure(
                    "S3 bucket inventory did not prove one authoritative canonical owner")
            owner_id = current_owner
            rows.extend(response.get("Buckets", []) or [])
            token = response.get("NextContinuationToken") or response.get(
                "ContinuationToken") if response.get("IsTruncated") else None
            if not token:
                return rows, owner_id
            if token in seen:
                break
            seen.add(token)
        raise system_readiness.DefinitiveSetupFailure(
            "S3 bucket inventory exceeded the bounded pagination limit")

    def _bucket_acl_is_safe(self, s3, bucket, owner_id):
        acl = self.call(
            "s3.get_bucket_acl", lambda: s3.get_bucket_acl(Bucket=bucket),
            "s3:GetBucketAcl")
        owner = acl.get("Owner") if isinstance(acl.get("Owner"), dict) else {}
        if str(owner.get("ID") or "") != owner_id:
            return False
        grants = acl.get("Grants", [])
        if not isinstance(grants, list):
            return False
        for grant in grants:
            if not isinstance(grant, dict):
                return False
            grantee = grant.get("Grantee")
            if (not isinstance(grantee, dict)
                    or grantee.get("Type") != "CanonicalUser"
                    or str(grantee.get("ID") or "") != owner_id):
                return False
        return True

    def _bucket_candidate(self, s3, name, account, owner_id, installation):
        location = self.call(
            "s3.get_bucket_location", lambda: s3.get_bucket_location(Bucket=name),
            "s3:GetBucketLocation").get("LocationConstraint") or "us-east-1"
        if location != self.region:
            return None
        try:
            self.call("s3.get_bucket_website", lambda: s3.get_bucket_website(Bucket=name),
                      "s3:GetBucketWebsite")
            return None
        except ProviderCallError as exc:
            if exc.provider_code not in ("NoSuchWebsiteConfiguration", "NoSuchWebsite"):
                raise
        tags = self._bucket_tags(s3, name)
        tenant_keys = {"tenant", "tenant-id", "group", "group-id", "user", "user-id"}
        if any(re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-") in tenant_keys
               for key in tags):
            return None
        expected_tags = {
            "managed-by": "django-mojo", "purpose": "system-media",
            "django-mojo-installation": installation["uuid"],
        }
        if any(key in tags and tags.get(key) != value
               for key, value in expected_tags.items()):
            return None
        if not self._bucket_acl_is_safe(s3, name, owner_id):
            return None
        policy = self._bucket_policy(s3, name)
        if not _s3_policy_is_safe(policy, account):
            return None
        try:
            policy_public = self.call(
                "s3.get_bucket_policy_status",
                lambda: s3.get_bucket_policy_status(Bucket=name),
                "s3:GetBucketPolicyStatus").get("PolicyStatus", {}).get("IsPublic")
        except ProviderCallError as exc:
            if exc.provider_code == "NoSuchBucketPolicy":
                policy_public = False
            else:
                raise
        if policy_public is not False:
            return None
        try:
            block = self.call(
                "s3.get_public_access_block",
                lambda: s3.get_public_access_block(Bucket=name),
                "s3:GetBucketPublicAccessBlock").get("PublicAccessBlockConfiguration", {})
        except ProviderCallError as exc:
            if exc.provider_code == "NoSuchPublicAccessBlockConfiguration":
                block = {}
            else:
                raise
        return {"name": name, "region": location, "private_block": all(
            block.get(key) is True for key in PRIVATE_BLOCK), "tags": tags}

    def discover_buckets(self, exact_name=""):
        s3 = self.client("s3")
        caller = self.identity()
        account = str(caller.get("Account") or "")
        if not re.fullmatch(r"\d{12}", account):
            raise system_readiness.DefinitiveSetupFailure(
                "The selected AWS identity did not prove a 12-digit account")
        installation = _identity()
        rows, owner_id = self._list_buckets(s3)
        names = sorted({str(row.get("Name")) for row in rows if row.get("Name")})
        if exact_name:
            names = [name for name in names if name == exact_name]
        candidates = []
        rejected = []
        for name in names:
            try:
                candidate = self._bucket_candidate(
                    s3, name, account, owner_id, installation)
            except system_readiness.DefinitiveSetupFailure:
                candidate = None
            if candidate is None:
                rejected.append(name)
            else:
                candidates.append(candidate)
        return {"candidates": candidates, "rejected": rejected, "complete": True}

    def _merge_cors(self, s3, bucket):
        desired = _direct_upload_cors_rule(SYSTEM_FILEMAN_ALLOWED_ORIGINS)
        preserved = []
        for attempt in range(2):
            try:
                current = self.call(
                    "s3.get_bucket_cors", lambda: s3.get_bucket_cors(Bucket=bucket),
                    "s3:GetBucketCORS").get("CORSRules", [])
            except ProviderCallError as exc:
                if exc.provider_code == "NoSuchCORSConfiguration":
                    current = []
                else:
                    raise
            for rule in current:
                if rule not in preserved:
                    preserved.append(rule)
            if (_cors_supports_direct_upload(current, SYSTEM_FILEMAN_ALLOWED_ORIGINS)
                    and all(rule in current for rule in preserved)):
                return False
            merged = list(preserved)
            if desired not in merged:
                merged.append(desired)
            self.call(
                "s3.put_bucket_cors",
                lambda: s3.put_bucket_cors(
                    Bucket=bucket, CORSConfiguration={"CORSRules": merged}),
                "s3:PutBucketCORS", mutation=True)
            verify = self.call(
                "s3.get_bucket_cors", lambda: s3.get_bucket_cors(Bucket=bucket),
                "s3:GetBucketCORS").get("CORSRules", [])
            if (_cors_supports_direct_upload(verify, SYSTEM_FILEMAN_ALLOWED_ORIGINS)
                    and all(rule in verify for rule in preserved)):
                return True
            if attempt:
                raise RuntimeError("Concurrent S3 CORS changes did not converge")

    def _adopt_bucket(self, bucket):
        from mojo.apps.fileman.models import FileManager
        identity = _identity()
        s3 = self.client("s3")
        with transaction.atomic():
            from mojo.apps.account.models import User
            User.objects.select_for_update().order_by("pk").first()
            candidates = self.discover_buckets(exact_name=bucket)["candidates"]
            if len(candidates) != 1:
                raise system_readiness.DefinitiveSetupFailure(
                    "The chosen bucket is not a safe private media-bucket candidate")
            candidate = candidates[0]
            existing = list(FileManager.objects.select_for_update().filter(
                user=None, group=None, is_active=True, is_default=True,
                backend_type=FileManager.AWS_S3).order_by("id")[:2])
            if len(existing) > 1:
                raise system_readiness.DefinitiveSetupFailure(
                    "Multiple active system-default S3 FileManagers require manual resolution")
            if existing and existing[0].root_location != bucket:
                raise system_readiness.DefinitiveSetupFailure(
                    "A different system-default S3 FileManager already exists")
            tags = candidate["tags"]
            if not candidate["private_block"]:
                self.call(
                    "s3.put_public_access_block",
                    lambda: s3.put_public_access_block(
                        Bucket=bucket, PublicAccessBlockConfiguration=PRIVATE_BLOCK),
                    "s3:PutBucketPublicAccessBlock", mutation=True)
                verified_block = self.call(
                    "s3.get_public_access_block",
                    lambda: s3.get_public_access_block(Bucket=bucket),
                    "s3:GetBucketPublicAccessBlock").get(
                        "PublicAccessBlockConfiguration", {})
                if any(verified_block.get(key) is not True for key in PRIVATE_BLOCK):
                    raise system_readiness.DefinitiveSetupFailure(
                        "S3 Block Public Access did not converge")
            expected_tags = {
                "managed-by": "django-mojo", "purpose": "system-media",
                "django-mojo-installation": identity["uuid"],
            }
            merged_tags = dict(tags)
            merged_tags.update(expected_tags)
            if any(tags.get(key) != value for key, value in expected_tags.items()):
                self.call(
                    "s3.put_bucket_tagging",
                    lambda: s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": [
                        {"Key": key, "Value": value}
                        for key, value in sorted(merged_tags.items())]}),
                    "s3:PutBucketTagging", mutation=True)
                verified_tags = self._bucket_tags(s3, bucket)
                if any(verified_tags.get(key) != value
                       for key, value in expected_tags.items()):
                    raise system_readiness.DefinitiveSetupFailure(
                        "S3 ownership tags did not converge")
            self._merge_cors(s3, bucket)
            if existing:
                manager = existing[0]
                manager.supports_direct_upload = True
                manager.is_public = False
                manager.set_aws_region(self.region)
                manager.set_allowed_origins(["*"])
                manager.save()
            else:
                manager = FileManager(
                    name="django-mojo system media", backend_type=FileManager.AWS_S3,
                    backend_url=f"s3://{bucket}", is_active=True, is_default=True,
                    is_public=False, supports_direct_upload=True)
                manager.set_aws_region(self.region)
                manager.set_allowed_origins(["*"])
                manager.save()
                manager._update_default()
        return manager

    def adopt_bucket(self, actor, bucket):
        system_settings.require_system_admin(actor)
        return self._adopt_bucket(bucket)

    def adopt_bucket_for_cli(self, bucket):
        """Use the portal's authoritative classifier for confirmed CLI adoption."""
        return self._adopt_bucket(bucket)

    def discover_verified_domains(self):
        ses = self.client("ses")
        identities = self._list_paginated(
            ses, "list_identities", "Identities", "ses.list_identities",
            "ses:ListIdentities", IdentityType="Domain", MaxItems=100)
        attributes = {}
        for offset in range(0, len(identities), 100):
            chunk = identities[offset:offset + 100]
            attributes.update(self.call(
                "ses.get_identity_verification_attributes",
                lambda chunk=chunk: ses.get_identity_verification_attributes(
                    Identities=chunk),
                "ses:GetIdentityVerificationAttributes").get(
                    "VerificationAttributes", {}))
        return sorted(name for name in identities if
                      (attributes.get(name) or {}).get("VerificationStatus") == "Success")

    def configure_email(self, actor, domain_name, sender):
        from mojo.apps.aws.models import EmailDomain, Mailbox
        from mojo.apps.aws.services import mailbox_defaults
        system_settings.require_system_admin(actor)
        if domain_name not in self.discover_verified_domains():
            raise system_readiness.DefinitiveSetupFailure(
                "The selected SES domain is not verified in the selected account and region")
        sender = str(sender or "").strip().lower()
        try:
            validate_email(sender)
        except ValidationError:
            raise system_readiness.DefinitiveSetupFailure(
                "Choose a valid sender mailbox address")
        local, sender_domain = sender.rsplit("@", 1)
        if not local or sender_domain != domain_name.lower():
            raise system_readiness.DefinitiveSetupFailure(
                "The sender address must belong to the selected verified SES domain")
        with transaction.atomic():
            domain, _ = EmailDomain.objects.select_for_update().get_or_create(
                name=domain_name, defaults={"region": self.region, "status": "verified"})
            if domain.region and domain.region != self.region:
                raise system_readiness.DefinitiveSetupFailure(
                    "The existing EmailDomain is configured for a different region")
            domain.region = self.region
            domain.status = "verified"
            domain.save(update_fields=["region", "status", "modified"])
            mailbox, _ = Mailbox.objects.update_or_create(
                email=sender, defaults={"domain": domain, "allow_outbound": True})
            # The locked helper is the ONE writer for the system-default
            # invariant (shared with Mailbox.on_rest_saved and the admin
            # mailbox-default endpoint). Still inside this transaction.
            mailbox_defaults.claim_system_default(mailbox)
            install_missing()
        return mailbox

    def _monitoring_topic(self):
        identity = _identity()
        sns = self.client("sns")
        topic_name = f"django-mojo-{identity['slug']}-operations"[:256]
        topics = self._list_paginated(
            sns, "list_topics", "Topics", "sns.list_topics", "sns:ListTopics")
        return sns, topic_name, next((row.get("TopicArn") for row in topics
                                     if row.get("TopicArn", "").rsplit(":", 1)[-1] == topic_name), None)

    def monitoring_topic_adoption(self):
        identity = _identity()
        sns, topic_name, topic_arn = self._monitoring_topic()
        if not topic_arn:
            return None
        rows = self.call(
            "sns.list_tags_for_resource",
            lambda: sns.list_tags_for_resource(ResourceArn=topic_arn),
            "sns:ListTagsForResource").get("Tags", [])
        tags = {row.get("Key"): row.get("Value") for row in rows}
        expected = {**OWNERSHIP_TAGS, "deployment": identity["slug"],
                    "django-mojo-installation": identity["uuid"]}
        if all(tags.get(key) == value for key, value in expected.items()):
            return None
        if any(key in tags for key in expected):
            raise system_readiness.DefinitiveSetupFailure(
                "The operations topic name is already owned by another configuration")
        return {"topic_arn": topic_arn, "topic_name": topic_name}

    def _expected_monitoring_tags(self, identity):
        return {**OWNERSHIP_TAGS, "deployment": identity["slug"],
                "django-mojo-installation": identity["uuid"]}

    def _topic_policy(self, sns, topic_arn):
        attributes = self.call(
            "sns.get_topic_attributes",
            lambda: sns.get_topic_attributes(TopicArn=topic_arn),
            "sns:GetTopicAttributes").get("Attributes", {})
        raw_policy = attributes.get("Policy")
        try:
            policy = json.loads(raw_policy) if raw_policy else {
                "Version": "2012-10-17", "Statement": []}
        except (TypeError, ValueError):
            raise system_readiness.DefinitiveSetupFailure(
                "The existing operations topic policy could not be preserved safely")
        if _policy_statements(policy) is None:
            raise system_readiness.DefinitiveSetupFailure(
                "The existing operations topic policy could not be preserved safely")
        return policy

    def _desired_topic_policy(self, policy, topic_arn, account, identity):
        if not _sns_publish_policy_is_safe(
                policy, account, self.region, identity["slug"], topic_arn):
            raise system_readiness.DefinitiveSetupFailure(
                "The operations topic policy contains an unsafe publish grant; "
                "django-mojo preserved it and refused adoption")
        statements = [row for row in _policy_statements(policy)
                      if row.get("Sid") != "DjangoMojoCloudWatchPublish"]
        partition = topic_arn.split(":", 2)[1]
        statements.append({
            "Sid": "DjangoMojoCloudWatchPublish",
            "Effect": "Allow",
            "Principal": {"Service": "cloudwatch.amazonaws.com"},
            "Action": "sns:Publish",
            "Resource": topic_arn,
            "Condition": {
                "StringEquals": {"AWS:SourceAccount": account},
                "ArnLike": {"AWS:SourceArn": (
                    f"arn:{partition}:cloudwatch:{self.region}:{account}:alarm:"
                    f"django-mojo/{identity['slug']}/*")},
            },
        })
        desired = dict(policy)
        desired["Statement"] = statements
        return desired

    def _alarm_owned(self, cloudwatch, alarm, tags):
        alarm_tags = self.call(
            "cloudwatch.list_tags_for_resource",
            lambda: cloudwatch.list_tags_for_resource(
                ResourceARN=alarm.get("AlarmArn")),
            "cloudwatch:ListTagsForResource").get("Tags", [])
        alarm_tag_map = {row.get("Key"): row.get("Value") for row in alarm_tags}
        return all(alarm_tag_map.get(key) == value for key, value in tags.items())

    def _converge_alarm(self, cloudwatch, desired, tags):
        found = self.call(
            "cloudwatch.describe_alarms",
            lambda: cloudwatch.describe_alarms(AlarmNames=[desired["AlarmName"]]),
            "cloudwatch:DescribeAlarms").get("MetricAlarms", [])
        if found and not self._alarm_owned(cloudwatch, found[0], tags):
            raise system_readiness.DefinitiveSetupFailure(
                "A reserved CloudWatch alarm name is not owned by this installation")
        if found and _alarm_matches(found[0], desired):
            return found[0]
        payload = dict(desired)
        if not found:
            payload["Tags"] = [
                {"Key": key, "Value": value} for key, value in sorted(tags.items())]
        self.call(
            "cloudwatch.put_metric_alarm",
            lambda: cloudwatch.put_metric_alarm(**payload),
            "cloudwatch:PutMetricAlarm", mutation=True)
        verified = self.call(
            "cloudwatch.describe_alarms",
            lambda: cloudwatch.describe_alarms(AlarmNames=[desired["AlarmName"]]),
            "cloudwatch:DescribeAlarms").get("MetricAlarms", [])
        if not verified or not _alarm_matches(verified[0], desired):
            raise system_readiness.DefinitiveSetupFailure(
                "CloudWatch did not confirm the exact alarm configuration")
        if not self._alarm_owned(cloudwatch, verified[0], tags):
            raise system_readiness.DefinitiveSetupFailure(
                "CloudWatch did not confirm alarm ownership")
        return verified[0]

    def _probe_evidence(self, topic_arn, alarm_arn, cutoff):
        from mojo.apps.aws.models import CloudWatchAlarmTransition
        rows = CloudWatchAlarmTransition.objects.filter(
            topic_arn=topic_arn, alarm__alarm_arn=alarm_arn,
            is_delivery_probe=True, created__gte=cutoff,
        ).order_by("created", "pk")
        alarm_transition = rows.filter(new_state="ALARM").first()
        if alarm_transition is None:
            return None, None
        ok_transition = rows.filter(
            new_state="OK", created__gte=alarm_transition.created,
            state_changed_at__gte=alarm_transition.state_changed_at,
        ).exclude(pk=alarm_transition.pk).first()
        return alarm_transition, ok_transition

    def reconcile_monitoring(self, actor, challenge, cutoff, send_probe=True,
                             choice=None):
        actor = system_settings.require_system_admin(actor)
        identity = _identity()
        sns = self.client("sns")
        cloudwatch = self.client("cloudwatch")
        caller = self.identity()
        account = str(caller.get("Account") or "")
        if not account.isdigit() or len(account) != 12:
            raise system_readiness.DefinitiveSetupFailure(
                "AWS identity did not return an authoritative account number")
        if not _PROBE_CHALLENGE.fullmatch(str(challenge or "")) or cutoff is None:
            raise system_readiness.DefinitiveSetupFailure(
                "The durable monitoring proof challenge is missing")
        topic_name = f"django-mojo-{identity['slug']}-operations"[:256]
        topics = self._list_paginated(
            sns, "list_topics", "Topics", "sns.list_topics", "sns:ListTopics")
        topic_arn = next((row.get("TopicArn") for row in topics
                          if row.get("TopicArn", "").rsplit(":", 1)[-1] == topic_name), None)
        tags = self._expected_monitoring_tags(identity)
        created_topic = not topic_arn
        if created_topic:
            topic_arn = self.call(
                "sns.create_topic",
                lambda: sns.create_topic(Name=topic_name, Tags=[
                    {"Key": key, "Value": value} for key, value in sorted(tags.items())]),
                "sns:CreateTopic", mutation=True).get("TopicArn")
        parts = str(topic_arn or "").split(":", 5)
        if (len(parts) != 6 or parts[0] != "arn"
                or parts[1] not in ("aws", "aws-us-gov", "aws-cn")
                or parts[2] != "sns" or parts[3] != self.region
                or parts[4] != account or parts[5] != topic_name):
            raise system_readiness.DefinitiveSetupFailure(
                "The operations topic identity does not match this AWS account and region")
        policy = self._topic_policy(sns, topic_arn)
        desired_policy = self._desired_topic_policy(
            policy, topic_arn, account, identity)
        topic_tags = self.call(
            "sns.list_tags_for_resource",
            lambda: sns.list_tags_for_resource(ResourceArn=topic_arn),
            "sns:ListTagsForResource").get("Tags", [])
        topic_tag_map = {row.get("Key"): row.get("Value") for row in topic_tags}
        ownership_keys_present = any(key in topic_tag_map for key in tags)
        if ownership_keys_present and any(
                topic_tag_map.get(key) != value for key, value in tags.items()):
            raise system_readiness.DefinitiveSetupFailure(
                "The operations topic name is already used by a resource not owned by this installation")
        if not ownership_keys_present:
            if not created_topic and not (
                    isinstance(choice, dict)
                    and choice.get("adopt_existing_topic") is True
                    and choice.get("topic_arn") == topic_arn):
                raise system_readiness.DefinitiveSetupFailure(
                    "Explicitly adopt the existing unowned operations topic before it can be changed")
            self.call(
                "sns.tag_resource",
                lambda: sns.tag_resource(ResourceArn=topic_arn, Tags=[
                    {"Key": key, "Value": value} for key, value in sorted(tags.items())]),
                "sns:TagResource", mutation=True)
        if desired_policy != policy:
            self.call(
                "sns.set_topic_attributes",
                lambda: sns.set_topic_attributes(
                    TopicArn=topic_arn, AttributeName="Policy",
                    AttributeValue=json.dumps(
                        desired_policy, sort_keys=True, separators=(",", ":"))),
                "sns:SetTopicAttributes", mutation=True)
            verified_policy = self._topic_policy(sns, topic_arn)
            if verified_policy != desired_policy:
                raise system_readiness.DefinitiveSetupFailure(
                    "SNS did not confirm the safe operations topic policy")
        existing_allowlist = system_settings.get_value(system_settings.MONITORING_TOPICS, []) or []
        system_settings.set_value(actor, system_settings.MONITORING_TOPICS,
                                  sorted(set(existing_allowlist + [topic_arn])))
        base_url = system_settings.get_value(system_settings.BASE_URL)
        endpoint = base_url.rstrip("/") + "/api/aws/cloudwatch/sns/alarm"
        subscriptions = self._list_paginated(
            sns, "list_subscriptions_by_topic", "Subscriptions",
            "sns.list_subscriptions_by_topic", "sns:ListSubscriptionsByTopic",
            TopicArn=topic_arn)
        matches = [row for row in subscriptions if row.get("Protocol") == "https"
                   and row.get("Endpoint") == endpoint]
        if not matches:
            self.call(
                "sns.subscribe",
                lambda: sns.subscribe(TopicArn=topic_arn, Protocol="https",
                                      Endpoint=endpoint, ReturnSubscriptionArn=True),
                "sns:Subscribe", mutation=True)
            subscriptions = self._list_paginated(
                sns, "list_subscriptions_by_topic", "Subscriptions",
                "sns.list_subscriptions_by_topic", "sns:ListSubscriptionsByTopic",
                TopicArn=topic_arn)
        confirmed = any(
            row.get("Protocol") == "https" and row.get("Endpoint") == endpoint
            and row.get("SubscriptionArn") not in (None, "PendingConfirmation")
            for row in subscriptions)
        probe_name = delivery_probe_alarm_name(challenge)
        probe_desired = _complete_alarm_config({
            "AlarmName": probe_name, "Namespace": "DjangoMojo/Setup",
            "MetricName": "DeliveryProbe", "Statistic": "Maximum", "Period": 60,
            "EvaluationPeriods": 1, "DatapointsToAlarm": 1, "Threshold": 0,
            "ComparisonOperator": "GreaterThanThreshold",
            "TreatMissingData": "notBreaching", "Dimensions": [],
        }, topic_arn)
        probe = self._converge_alarm(cloudwatch, probe_desired, tags)

        desired = self._desired_monitoring_alarms(cloudwatch)
        for alarm in desired:
            alarm = _complete_alarm_config(alarm, topic_arn)
            self._converge_alarm(cloudwatch, alarm, tags)
        if send_probe and confirmed:
            alarm_transition, ok_transition = self._probe_evidence(
                topic_arn, probe.get("AlarmArn"), cutoff)
            if (alarm_transition is None and probe.get("StateValue") == "ALARM"
                    and (timezone.now() - cutoff).total_seconds() > 300):
                raise system_readiness.DefinitiveSetupFailure(
                    "The confirmed SNS subscription did not deliver the setup ALARM "
                    "challenge within five minutes; start a new Fix Setup operation")
            states = (["ALARM"] if alarm_transition is None
                      and probe.get("StateValue") != "ALARM" else
                      ["OK"] if alarm_transition is not None
                      and ok_transition is None and probe.get("StateValue") != "OK"
                      else [])
            for state in states:
                self.call(
                    "cloudwatch.set_alarm_state",
                    lambda state=state: cloudwatch.set_alarm_state(
                        AlarmName=probe_name, StateValue=state,
                        StateReason="django-mojo System Setup delivery verification"),
                    "cloudwatch:SetAlarmState", mutation=True)
        return {"topic_arn": topic_arn, "endpoint": endpoint, "probe_alarm": probe_name}

    def monitoring_proven(self, challenge, cutoff):
        identity = _identity()
        sns = self.client("sns")
        cloudwatch = self.client("cloudwatch")
        topic_name = f"django-mojo-{identity['slug']}-operations"
        topics = self._list_paginated(
            sns, "list_topics", "Topics", "sns.list_topics", "sns:ListTopics")
        topic_arn = next((row.get("TopicArn") for row in topics
                          if row.get("TopicArn", "").rsplit(":", 1)[-1] == topic_name), None)
        if not topic_arn or topic_arn not in (
                system_settings.get_value(system_settings.MONITORING_TOPICS, []) or []):
            return False
        caller = self.identity()
        account = str(caller.get("Account") or "")
        topic_parts = topic_arn.split(":", 5)
        if (not _PROBE_CHALLENGE.fullmatch(str(challenge or "")) or cutoff is None
                or len(topic_parts) != 6 or topic_parts[0] != "arn"
                or topic_parts[1] not in ("aws", "aws-us-gov", "aws-cn")
                or topic_parts[2] != "sns" or topic_parts[3] != self.region
                or topic_parts[4] != account or topic_parts[5] != topic_name):
            return False
        expected_tags = self._expected_monitoring_tags(identity)
        topic_tags = self.call(
            "sns.list_tags_for_resource",
            lambda: sns.list_tags_for_resource(ResourceArn=topic_arn),
            "sns:ListTagsForResource").get("Tags", [])
        topic_tag_map = {row.get("Key"): row.get("Value") for row in topic_tags}
        if any(topic_tag_map.get(key) != value for key, value in expected_tags.items()):
            return False
        policy = self._topic_policy(sns, topic_arn)
        try:
            desired_policy = self._desired_topic_policy(
                policy, topic_arn, account, identity)
        except system_readiness.DefinitiveSetupFailure:
            return False
        if desired_policy != policy:
            return False
        endpoint = system_settings.get_value(system_settings.BASE_URL).rstrip(
            "/") + "/api/aws/cloudwatch/sns/alarm"
        subscriptions = self._list_paginated(
            sns, "list_subscriptions_by_topic", "Subscriptions",
            "sns.list_subscriptions_by_topic", "sns:ListSubscriptionsByTopic",
            TopicArn=topic_arn)
        if not any(row.get("Protocol") == "https" and row.get("Endpoint") == endpoint
                   and row.get("SubscriptionArn") not in (None, "PendingConfirmation")
                   for row in subscriptions):
            return False
        probe_name = delivery_probe_alarm_name(challenge)
        probe = self.call(
            "cloudwatch.describe_alarms",
            lambda: cloudwatch.describe_alarms(AlarmNames=[probe_name]),
            "cloudwatch:DescribeAlarms").get("MetricAlarms", [])
        probe_desired = _complete_alarm_config({
            "AlarmName": probe_name, "Namespace": "DjangoMojo/Setup",
            "MetricName": "DeliveryProbe", "Statistic": "Maximum", "Period": 60,
            "EvaluationPeriods": 1, "DatapointsToAlarm": 1, "Threshold": 0,
            "ComparisonOperator": "GreaterThanThreshold",
            "TreatMissingData": "notBreaching", "Dimensions": [],
        }, topic_arn)
        if not probe or not _alarm_matches(probe[0], probe_desired):
            return False
        probe_tags = self.call(
            "cloudwatch.list_tags_for_resource",
            lambda: cloudwatch.list_tags_for_resource(ResourceARN=probe[0].get("AlarmArn")),
            "cloudwatch:ListTagsForResource").get("Tags", [])
        probe_tag_map = {row.get("Key"): row.get("Value") for row in probe_tags}
        if any(probe_tag_map.get(key) != value for key, value in expected_tags.items()):
            return False
        desired = self._desired_monitoring_alarms(cloudwatch)
        for alarm in desired:
            alarm = _complete_alarm_config(alarm, topic_arn)
            found = self.call(
                "cloudwatch.describe_alarms",
                lambda alarm=alarm: cloudwatch.describe_alarms(
                    AlarmNames=[alarm["AlarmName"]]),
                "cloudwatch:DescribeAlarms").get("MetricAlarms", [])
            if not found or not _alarm_matches(found[0], alarm):
                return False
            alarm_tags = self.call(
                "cloudwatch.list_tags_for_resource",
                lambda found=found: cloudwatch.list_tags_for_resource(
                    ResourceARN=found[0].get("AlarmArn")),
                "cloudwatch:ListTagsForResource").get("Tags", [])
            alarm_tag_map = {row.get("Key"): row.get("Value") for row in alarm_tags}
            if any(alarm_tag_map.get(key) != value for key, value in expected_tags.items()):
                return False
        alarm_transition, ok_transition = self._probe_evidence(
            topic_arn, probe[0].get("AlarmArn"), cutoff)
        return alarm_transition is not None and ok_transition is not None


def _runner_rows(section, clients=None):
    report = AWSCheckRunner(clients=clients).run([section])
    rows = []
    for item in report["items"]:
        status = item["status"] if item["status"] in system_readiness.STATUSES else "pending"
        rows.append(system_readiness.result(
            f"aws.{item['code']}", status, item["message"], item.get("remediation", ""),
            fixable=status != "pass", details=item.get("details")))
    return rows


def check_identity(context):
    return _runner_rows("identity", context.get("aws_clients"))


def check_s3(context):
    rows = _runner_rows("s3", context.get("aws_clients"))
    try:
        discovery = AWSSetupService(
            clients=context.get("aws_clients")).discover_buckets()
    except ProviderCallError as exc:
        remediation = (f"Grant {exc.iam_action} to the selected AWS identity, then rerun."
                       if exc.iam_action else
                       "Verify the selected AWS identity and S3 inventory permissions.")
        rows.append(system_readiness.result(
            "aws.s3_discovery", "fail",
            "Existing S3 media buckets could not be classified safely.",
            remediation, True, details=exc.detail()))
    except system_readiness.DefinitiveSetupFailure as exc:
        rows.append(system_readiness.result(
            "aws.s3_discovery", "fail", str(exc),
            "Correct the conflicting bucket posture, then rerun.", True))
    else:
        rows.append(system_readiness.result(
            "aws.s3_discovery", "pass" if discovery["candidates"] else "warn",
            ("Safe existing private media-bucket candidates were discovered."
             if discovery["candidates"] else
             "No safe existing private media-bucket candidate was discovered."),
            ("" if discovery["candidates"] else
             "Create or prepare a private media bucket, then rerun Fix Setup."),
            True, details={"candidate_count": len(discovery["candidates"])}))
    return rows


def check_email(context):
    from mojo.apps.aws.models import EmailDomain, Mailbox

    service = AWSSetupService(clients=context.get("aws_clients"))
    try:
        verified = service.discover_verified_domains()
    except ProviderCallError as exc:
        remediation = (f"Grant {exc.iam_action} to the selected AWS identity, then rerun."
                       if exc.iam_action else
                       "Verify the selected AWS identity and region, then rerun.")
        return [system_readiness.result(
            "aws.ses_discovery", "fail",
            "SES verified identities could not be inspected safely.",
            remediation, True, details=exc.detail())]

    rows = [system_readiness.result(
        "aws.ses_verified_identity", "pass" if verified else "warn",
        ("At least one verified SES domain is available in the selected account and region."
         if verified else
         "No verified SES domain is available in the selected account and region."),
        ("" if verified else
         "Verify a domain through the platform DNS/SES setup, then rerun Fix Setup."),
        True, details={"verified_domain_count": len(verified)})]

    defaults = list(Mailbox.objects.select_related("domain").filter(
        is_system_default=True).order_by("pk")[:2])
    if len(defaults) > 1:
        rows.append(system_readiness.result(
            "aws.sender", "fail", "More than one system-default sender is configured.",
            "Choose one verified SES sender with Fix Setup.", True,
            details={"default_count": len(defaults)}))
    elif not defaults:
        rows.append(system_readiness.result(
            "aws.sender", "warn", "No system-default outbound sender is configured.",
            "Run Fix Setup and choose a sender on a verified SES domain.", True))
    else:
        mailbox = defaults[0]
        domain = mailbox.domain
        ready = bool(
            mailbox.allow_outbound and domain.name in verified
            and domain.region == service.region and domain.status in ("verified", "ready"))
        rows.append(system_readiness.result(
            "aws.sender", "pass" if ready else "warn",
            ("The system sender belongs to a verified SES domain."
             if ready else
             "The configured system sender is not fully aligned with the verified SES identity."),
            "Run Fix Setup and reselect the verified domain and sender.", True,
            details={"sender": mailbox.email, "domain": domain.name}))

    status = shipped_status()
    rows.append(system_readiness.result(
        "aws.email_templates", "pass" if not status["missing"] else "warn",
        "All shipped email templates are installed." if not status["missing"] else
        "One or more shipped email templates are missing.",
        "Run Fix Setup to install only missing templates.", True,
        details={"missing_count": len(status["missing"])}))
    return rows


def check_monitoring(context):
    return _runner_rows("monitoring", context.get("aws_clients"))


def s3_choice_schema(context):
    try:
        names = [row["name"] for row in AWSSetupService(
            clients=context.get("aws_clients")).discover_buckets()["candidates"]]
    except Exception:
        names = []
    return {"type": "object", "properties": {
        "bucket": {"type": "string", "enum": names},
        "adopt_existing": {"type": "boolean", "enum": [True]},
    }, "required": ["bucket", "adopt_existing"], "additionalProperties": False}


def email_choice_schema(context):
    try:
        domains = AWSSetupService(clients=context.get("aws_clients")).discover_verified_domains()
    except Exception:
        domains = []
    return {"type": "object", "properties": {
        "domain": {"type": "string", "enum": domains},
        "sender": {"type": "string", "format": "email"},
    }, "required": ["domain", "sender"], "additionalProperties": False}


def fix_s3(context, choice):
    AWSSetupService(clients=context.get("aws_clients")).adopt_bucket(
        context["actor"], choice["bucket"])


def fix_email(context, choice):
    AWSSetupService(clients=context.get("aws_clients")).configure_email(
        context["actor"], choice["domain"], choice["sender"])


def _monitoring_probe_intent(operation):
    """Persist the unpredictable proof identity before any provider mutation."""
    from mojo.apps.account.models import SystemSetupOperation
    if operation is None:
        raise system_readiness.DefinitiveSetupFailure(
            "Monitoring setup requires a durable System Setup operation")
    with transaction.atomic():
        current = SystemSetupOperation.objects.select_for_update().get(pk=operation.pk)
        if current.cursor >= len(current.steps or []):
            raise system_readiness.DefinitiveSetupFailure(
                "The monitoring setup step is no longer active")
        steps = list(current.steps)
        step = dict(steps[current.cursor])
        if step.get("id") != "section:aws_monitoring":
            raise system_readiness.DefinitiveSetupFailure(
                "The monitoring setup step is no longer active")
        challenge = str(step.get("probe_challenge") or "")
        cutoff = parse_datetime(str(step.get("probe_cutoff") or ""))
        if not _PROBE_CHALLENGE.fullmatch(challenge) or cutoff is None:
            challenge = secrets.token_hex(16)
            cutoff = timezone.now()
            step["probe_challenge"] = challenge
            step["probe_cutoff"] = cutoff.isoformat()
            steps[current.cursor] = step
            current.steps = steps
            current.save(update_fields=["steps", "modified"])
        return challenge, cutoff


def fix_monitoring(context, choice):
    challenge, cutoff = _monitoring_probe_intent(context.get("operation"))
    AWSSetupService(clients=context.get("aws_clients")).reconcile_monitoring(
        context["actor"], challenge, cutoff, choice=choice)


def reconcile_s3(context, choice):
    manager = AWSSetupService(clients=context.get("aws_clients")).adopt_bucket(
        context["actor"], choice.get("bucket", ""))
    return {"status": "proven" if manager and manager.supports_direct_upload
            and manager.allowed_origins == ["*"] and not manager.is_public else "pending"}


def reconcile_email(context, choice):
    from mojo.apps.aws.models import Mailbox
    exists = Mailbox.objects.filter(email=choice.get("sender"), is_system_default=True,
                                    allow_outbound=True).exists()
    return {"status": "proven" if exists and not shipped_status()["missing"] else "pending"}


def reconcile_monitoring(context, choice):
    operation = context.get("operation")
    challenge, cutoff = _monitoring_probe_intent(operation)
    service = AWSSetupService(clients=context.get("aws_clients"))
    try:
        proven = service.monitoring_proven(challenge, cutoff)
    except Exception:
        proven = False
    if not proven:
        service.reconcile_monitoring(
            context["actor"], challenge, cutoff, choice=choice)
    return {"status": "proven" if proven else "pending"}


def monitoring_choice_schema(context):
    try:
        adoption = AWSSetupService(
            clients=context.get("aws_clients")).monitoring_topic_adoption()
    except Exception:
        adoption = None
    if not adoption:
        return None
    return {"type": "object", "properties": {
        "topic_arn": {"type": "string", "enum": [adoption["topic_arn"]]},
        "adopt_existing_topic": {"type": "boolean", "enum": [True]},
    }, "required": ["topic_arn", "adopt_existing_topic"],
        "additionalProperties": False}


def register_sections():
    system_readiness.register_section(
        "aws_identity", "AWS identity", check_identity, order=30)
    system_readiness.register_section(
        "aws_s3", "System S3 storage", check_s3, fix_s3, reconcile_s3,
        s3_choice_schema, order=31)
    system_readiness.register_section(
        "aws_email", "SES email", check_email, fix_email, reconcile_email,
        email_choice_schema, order=32)
    system_readiness.register_section(
        "aws_monitoring", "SNS and CloudWatch", check_monitoring,
        fix_monitoring, reconcile_monitoring, monitoring_choice_schema, order=33)
