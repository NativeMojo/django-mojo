"""Deployment-level AWS readiness audit and create-missing bootstrap."""

import os
import re
import uuid
from urllib.parse import urlparse

from botocore.exceptions import (
    BotoCoreError, ClientError, ConnectTimeoutError, EndpointConnectionError,
    NoCredentialsError, PartialCredentialsError, ReadTimeoutError,
)
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from mojo.helpers import logit
from mojo.helpers.aws.client import get_client, get_session
from mojo.helpers.settings import settings


logger = logit.get_logger("aws_check", "aws.log")
STATUSES = ("pass", "warn", "fail", "pending", "skip")
SECTIONS = ("prerequisites", "identity", "cron", "s3", "email", "monitoring", "rules")
OWNERSHIP_TAGS = {"managed-by": "django-mojo", "purpose": "cloudwatch-incidents"}


def _setting(name, default=None, kind=None):
    try:
        return settings.get_static(name, default, kind=kind) if kind else settings.get_static(name, default)
    except Exception:
        return default


def _error_code(exc):
    return str((getattr(exc, "response", {}).get("Error") or {}).get("Code") or "")


def _safe_slug(value):
    return (re.sub(r"[^a-z0-9-]+", "-", (value or "").lower()).strip("-")[:48] or "deployment")


class AWSCheckRunner:
    def __init__(self, region=None, profile=None, timeout=3, apply=False, yes=False,
                 probe_s3=False, bucket_name=None, mailbox_email=None,
                 adopt_bucket=False, confirm=None, clients=None, session=None, now=None):
        self.region = region or _setting("AWS_REGION", "us-east-1")
        self.profile = profile
        self.timeout = max(1, min(int(timeout or 3), 30))
        self.apply = bool(apply)
        self.yes = bool(yes)
        self.probe_s3 = bool(probe_s3)
        self.bucket_name = bucket_name
        self.mailbox_email = mailbox_email
        self.adopt_bucket = bool(adopt_bucket)
        self.confirm = confirm or (lambda message: False)
        self.clients = clients or {}
        self.session = session
        self.now = now or timezone.now
        self.results = []
        self.primary_identity = None
        self.monitoring_ready = False
        self.delivery_seen = False
        self._secrets = [value for value in (_setting("AWS_KEY"), _setting("AWS_SECRET")) if value]

    def _redact(self, value):
        text = str(value)
        for secret in self._secrets:
            text = text.replace(str(secret), "[REDACTED]")
        return re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}\b", "[REDACTED]", text)[:2000]

    def _add(self, section, status, code, message, details=None, remediation=None, changed=False):
        self.results.append({
            "section": section, "status": status, "code": code,
            "message": self._redact(message), "details": details or {},
            "remediation": remediation or "", "changed": bool(changed),
        })

    def _approve(self, message):
        return bool(self.apply and (self.yes or self.confirm(message)))

    def _session(self, access_key=None, secret_key=None, region=None):
        if access_key is None and secret_key is None and self.session is not None:
            return self.session
        return get_session(
            access_key=access_key, secret_key=secret_key, region=region or self.region,
            profile=self.profile if not access_key and not secret_key else None,
        )

    def _client(self, service, access_key=None, secret_key=None, region=None):
        if service in self.clients and access_key is None and secret_key is None:
            return self.clients[service]
        return get_client(
            service, session=self._session(access_key, secret_key, region),
            region=region or self.region, timeout=self.timeout,
        )

    def run(self, sections=None):
        selected = list(sections or SECTIONS)
        unknown = sorted(set(selected) - set(SECTIONS))
        if unknown:
            raise ValueError(f"unknown section(s): {', '.join(unknown)}")
        for section in SECTIONS:
            if section not in selected:
                continue
            try:
                getattr(self, f"check_{section}")()
            except Exception as exc:
                logger.exception("aws-check section failed: %s", section)
                self._add(section, "fail", f"{section}.internal_error",
                          f"Unexpected {type(exc).__name__}: {self._redact(exc)}",
                          remediation=f"Rerun: manage.py aws-check --check --section {section}")
        counts = {status: 0 for status in STATUSES}
        for item in self.results:
            counts[item["status"]] += 1
        return {
            "schema_version": 1, "generated_at": self.now().isoformat(),
            "region": self.region, "overall": "fail" if counts["fail"] else "pass",
            "counts": counts, "items": self.results,
        }

    def check_prerequisites(self):
        self._add("prerequisites", "pass", "region.configured", f"AWS region is {self.region}", {"region": self.region})
        parsed = urlparse(_setting("BASE_URL", "") or "")
        if parsed.scheme == "https" and parsed.hostname:
            self._add("prerequisites", "pass", "base_url.https", "Public HTTPS BASE_URL is configured", {"host": parsed.hostname})
        else:
            self._add("prerequisites", "fail", "base_url.invalid",
                      "BASE_URL must be a public HTTPS URL for SNS delivery",
                      remediation="Set BASE_URL in deployment configuration and restart Django.")

    def _identity(self, access_key=None, secret_key=None, label="selected", region=None):
        response = self._client("sts", access_key, secret_key, region).get_caller_identity()
        return {"context": label, "account": response.get("Account"), "arn": response.get("Arn"), "region": region or self.region}

    def check_identity(self):
        try:
            self.primary_identity = self._identity()
            self._add("identity", "pass", "sts.identity", "AWS credentials are valid", self.primary_identity)
        except (NoCredentialsError, PartialCredentialsError) as exc:
            self._add("identity", "fail", "credentials.missing", exc,
                      remediation="Configure a complete key pair, AWS profile, or task/instance role.")
        except (ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError) as exc:
            self._add("identity", "fail", "sts.timeout", exc,
                      remediation="Check AWS network access and the selected region.")
        except ClientError as exc:
            self._add("identity", "fail", "sts.denied", exc, {"aws_code": _error_code(exc)})
        except BotoCoreError as exc:
            self._add("identity", "fail", "sts.error", exc)

    def check_cron(self):
        from mojo.helpers.cron import get_cron_heartbeats
        try:
            records = get_cron_heartbeats(limit=10)
        except Exception as exc:
            self._add("cron", "fail", "redis.unavailable", exc,
                      remediation="Restore Redis connectivity before checking cron/jobs.")
            return
        max_age = int(_setting("AWS_CHECK_CRON_MAX_AGE", 180) or 180)
        latest = records[0] if records else None
        if not latest:
            self._add("cron", "fail", "cron.heartbeat_missing", "No django-mojo cron-dispatch heartbeat was found",
                      remediation=("Run every minute: python manage.py shell -c \"from mojo.helpers import cron; "
                                   "cron.load_app_cron(); cron.run_now()\""))
        else:
            observed = parse_datetime(latest.get("completed_at") or latest.get("started_at") or "")
            age = (self.now() - observed).total_seconds() if observed else max_age + 1
            if latest.get("state") == "completed" and age <= max_age:
                self._add("cron", "pass", "cron.heartbeat_fresh", "Cron dispatcher completed recently",
                          {"age_seconds": int(age), "run_id": latest.get("run_id")})
            elif latest.get("state") == "failed":
                self._add("cron", "fail", "cron.dispatch_failed", "The latest cron dispatcher run failed",
                          {"run_id": latest.get("run_id"), "failure": latest.get("failure")})
            else:
                self._add("cron", "fail", "cron.heartbeat_stale", "Cron heartbeat is stale or incomplete",
                          {"age_seconds": int(age), "state": latest.get("state")})
        try:
            from mojo.apps.jobs.keys import JobKeys
            from mojo.helpers.redis import get_connection
            redis = get_connection()
            redis.ping()
            keys = JobKeys()
            runners = list(redis.scan_iter(match=keys.runner_hb("*"), count=100))
            scheduler = redis.get(keys.scheduler_lock())
            status = "pass" if runners and scheduler else "warn"
            self._add("cron", status, "jobs.health", "Jobs Redis is reachable",
                      {"runner_heartbeats": len(runners), "scheduler_lock": bool(scheduler)},
                      remediation="Start both the jobs engine and scheduler." if status == "warn" else "")
        except Exception as exc:
            self._add("cron", "fail", "jobs.redis_error", exc)

    def _bucket_tags(self, s3, bucket):
        try:
            rows = s3.get_bucket_tagging(Bucket=bucket).get("TagSet", [])
            return {row.get("Key"): row.get("Value") for row in rows}
        except ClientError as exc:
            if _error_code(exc) in ("NoSuchTagSet", "NoSuchTagSetError"):
                return {}
            raise

    def _deployment_slug(self):
        configured = _setting("AWS_MONITORING_NAME", "") or ""
        host = urlparse(_setting("BASE_URL", "") or "").hostname or "deployment"
        return _safe_slug(configured or host)

    def _create_bucket(self, bucket):
        from mojo.apps.fileman.models import FileManager
        s3 = self._client("s3")
        params = {"Bucket": bucket}
        if self.region != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
        s3.create_bucket(**params)
        s3.put_public_access_block(Bucket=bucket, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        })
        tags = self._bucket_tags(s3, bucket)
        tags.update({**OWNERSHIP_TAGS, "deployment": self._deployment_slug()})
        s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": [
            {"Key": key, "Value": value} for key, value in sorted(tags.items())
        ]})
        verified = self._bucket_tags(s3, bucket)
        if any(verified.get(key) != value for key, value in OWNERSHIP_TAGS.items()):
            raise RuntimeError("bucket ownership tags did not verify")
        manager = FileManager.objects.create(
            name="django-mojo system S3", backend_type=FileManager.AWS_S3,
            backend_url=f"s3://{bucket}", is_active=True, is_default=True,
            is_public=False, supports_direct_upload=True,
        )
        manager.set_aws_region(self.region)
        manager.save()
        manager._update_default()
        return manager

    def _adopt_bucket(self, bucket):
        """Explicitly merge ownership tags after a verified interrupted create."""
        from mojo.apps.fileman.models import FileManager
        s3 = self._client("s3")
        owned_names = {row.get("Name") for row in s3.list_buckets().get("Buckets", [])}
        if bucket not in owned_names:
            raise RuntimeError("selected credentials do not prove ownership of the exact bucket")
        location = s3.get_bucket_location(Bucket=bucket).get("LocationConstraint") or "us-east-1"
        if location != self.region:
            raise RuntimeError(f"bucket is in {location}, not selected region {self.region}")
        tags = self._bucket_tags(s3, bucket)
        tags.update({**OWNERSHIP_TAGS, "deployment": self._deployment_slug()})
        s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": [
            {"Key": key, "Value": value} for key, value in sorted(tags.items())
        ]})
        manager = FileManager.objects.create(
            name="django-mojo system S3", backend_type=FileManager.AWS_S3,
            backend_url=f"s3://{bucket}", is_active=True, is_default=True,
            is_public=False, supports_direct_upload=True,
        )
        manager.set_aws_region(self.region)
        manager.save()
        manager._update_default()
        return manager

    def check_s3(self):
        from mojo.apps.fileman.models import FileManager
        managers = list(FileManager.objects.filter(
            user__isnull=True, group__isnull=True, is_active=True,
            is_default=True, backend_type=FileManager.AWS_S3,
        ).order_by("id")[:3])
        if len(managers) > 1:
            self._add("s3", "fail", "fileman.multiple_defaults", "Multiple active system-default S3 FileManagers exist",
                      {"ids": [row.pk for row in managers]})
            return
        if not managers:
            action = "Adopt" if self.adopt_bucket else "Create private"
            if self.bucket_name and self._approve(f"{action} S3 bucket {self.bucket_name} and a system FileManager?"):
                try:
                    manager = self._adopt_bucket(self.bucket_name) if self.adopt_bucket else self._create_bucket(self.bucket_name)
                    managers = [manager]
                    self._add("s3", "pass", "fileman.created", "Created a private S3 bucket and system FileManager",
                              {"bucket": self.bucket_name, "file_manager_id": manager.pk}, changed=True)
                except Exception as exc:
                    self._add("s3", "fail", "bucket.create_failed", exc, {"bucket": self.bucket_name},
                              remediation="Inspect the exact bucket and ownership tags before rerunning.")
                    return
            else:
                self._add("s3", "warn", "fileman.missing", "No active system-default S3 FileManager exists",
                          remediation="Rerun with --apply --bucket-name <globally-unique-name>.")
                return
        manager = managers[0]
        bucket = urlparse(manager.backend_url).netloc
        region = manager.get_setting("aws_region", None) or self.region
        stored_key = manager.get_setting("aws_key", None)
        stored_secret = manager.get_setting("aws_secret", None)
        if stored_key or stored_secret:
            try:
                primary = self.primary_identity or self._identity()
                context = self._identity(stored_key, stored_secret, f"fileman:{manager.pk}", region)
                self._add("s3", "pass", "fileman.credential_context", "FileManager AWS identity validated", context)
                if primary.get("account") != context.get("account") or self.region != region:
                    self._add("s3", "fail", "fileman.cross_context", "FileManager uses a different AWS account or region",
                              {"selected_account": primary.get("account"), "configured_account": context.get("account"),
                               "selected_region": self.region, "configured_region": region})
                    return
            except Exception as exc:
                self._add("s3", "fail", "fileman.identity_error", exc, {"file_manager_id": manager.pk})
                return
        s3 = self._client("s3", stored_key, stored_secret, region)
        try:
            response = s3.head_bucket(Bucket=bucket)
            actual = (response.get("ResponseMetadata", {}).get("HTTPHeaders", {}) or {}).get("x-amz-bucket-region")
            self._add("s3", "pass", "bucket.accessible", "S3 bucket is accessible", {"bucket": bucket, "region": actual or region})
        except ClientError as exc:
            code = _error_code(exc)
            status_code = "bucket.denied" if code in ("403", "AccessDenied") else "bucket.missing" if code in ("404", "NoSuchBucket") else "bucket.error"
            self._add("s3", "fail", status_code, exc, {"bucket": bucket, "aws_code": code})
            return
        try:
            block = s3.get_public_access_block(Bucket=bucket).get("PublicAccessBlockConfiguration", {})
            private = all(block.get(key) is True for key in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"))
            self._add("s3", "pass" if private else "warn", "bucket.public_access_block",
                      "S3 public-access block is enabled" if private else "S3 public-access block is incomplete",
                      {"bucket": bucket, "configuration": block})
        except ClientError as exc:
            self._add("s3", "warn", "bucket.public_posture_unknown", exc, {"bucket": bucket})
        try:
            cors = s3.get_bucket_cors(Bucket=bucket).get("CORSRules", [])
            self._add("s3", "pass" if cors else "warn", "bucket.cors", "S3 CORS configuration audited",
                      {"bucket": bucket, "rule_count": len(cors)})
        except ClientError as exc:
            code = _error_code(exc)
            self._add("s3", "warn" if code == "NoSuchCORSConfiguration" else "fail", "bucket.cors", exc, {"bucket": bucket})
        if self.probe_s3 and self._approve(f"Write/read/delete one sentinel object in {bucket}?"):
            key = f"__django_mojo_aws_check__/{uuid.uuid4().hex}"
            body = uuid.uuid4().hex.encode("ascii")
            probe_error = cleanup_error = None
            try:
                s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/octet-stream")
                if s3.get_object(Bucket=bucket, Key=key)["Body"].read() != body:
                    raise RuntimeError("S3 probe body did not round-trip")
            except Exception as exc:
                probe_error = exc
            finally:
                try:
                    s3.delete_object(Bucket=bucket, Key=key)
                except Exception as exc:
                    cleanup_error = exc
            if probe_error:
                self._add("s3", "fail", "bucket.probe_failed", probe_error, {"bucket": bucket, "key": key})
            if cleanup_error:
                self._add("s3", "fail", "bucket.probe_cleanup_failed", cleanup_error, {"bucket": bucket, "key": key})
            if not probe_error and not cleanup_error:
                self._add("s3", "pass", "bucket.probe_passed", "S3 object round-trip succeeded", {"bucket": bucket}, changed=True)

    def check_email(self):
        from mojo.apps.aws.models import EmailDomain, EmailTemplate, Mailbox
        from mojo.apps.aws.services.email_ops import audit_email_domain
        domains = list(EmailDomain.objects.all().order_by("id")[:100])
        if not domains:
            self._add("email", "warn", "email.domain_missing", "No SES EmailDomain is configured",
                      remediation="Create an EmailDomain, add DNS records, then rerun aws-check.")
        for domain in domains:
            try:
                if domain.aws_key or domain.aws_secret:
                    primary = self.primary_identity or self._identity()
                    context = self._identity(domain.aws_key, domain.aws_secret, f"email-domain:{domain.pk}", domain.aws_region)
                    self._add("email", "pass", "email.credential_context", "EmailDomain AWS identity validated", context)
                    if primary.get("account") != context.get("account") or self.region != domain.aws_region:
                        self._add("email", "fail", "email.cross_context", "EmailDomain uses a different AWS account or region",
                                  {"domain": domain.name, "configured_account": context.get("account"),
                                   "selected_account": primary.get("account")})
                        continue
                report = audit_email_domain(
                    domain.pk, persist=False,
                    client_factory=lambda service, **kwargs: get_client(service, timeout=self.timeout, **kwargs),
                )
                pending = sorted(key for key, value in report.checks.items() if value is False)
                self._add("email", "pass" if report.audit_pass else "pending", "email.domain_audit",
                          f"SES audit completed for {domain.name}", {"domain": domain.name, "pending_checks": pending})
            except Exception as exc:
                self._add("email", "fail", "email.audit_error", exc, {"domain": domain.name})
        defaults = list(Mailbox.objects.filter(is_system_default=True).order_by("id")[:3])
        if len(defaults) == 1 and defaults[0].allow_outbound:
            self._add("email", "pass", "mailbox.system_default", "A system-default outbound Mailbox is configured", {"email": defaults[0].email})
        elif len(defaults) > 1:
            self._add("email", "fail", "mailbox.multiple_defaults", "Multiple system-default Mailboxes exist", {"ids": [row.pk for row in defaults]})
        elif self.mailbox_email and self._approve(f"Create system-default mailbox {self.mailbox_email}?"):
            domain = EmailDomain.objects.filter(name__iexact=self.mailbox_email.rsplit("@", 1)[-1]).first()
            if not domain:
                self._add("email", "fail", "mailbox.domain_missing", "Mailbox domain is not configured", {"email": self.mailbox_email})
            else:
                mailbox, created = Mailbox.objects.get_or_create(email=self.mailbox_email, defaults={
                    "domain": domain, "allow_outbound": True, "allow_inbound": False, "is_system_default": True,
                })
                self._add("email", "pass", "mailbox.created", "System-default outbound Mailbox is configured",
                          {"email": mailbox.email}, changed=created)
        else:
            self._add("email", "warn", "mailbox.system_default_missing", "No system-default outbound Mailbox is configured",
                      remediation="Rerun with --apply --mailbox-email sender@example.com.")
        seed_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "seeds", "email_templates")
        seed_names = sorted(os.path.splitext(name)[0] for name in os.listdir(seed_dir) if name.endswith(".json"))
        existing = set(EmailTemplate.objects.filter(name__in=seed_names).values_list("name", flat=True))
        missing = [name for name in seed_names if name not in existing]
        if missing and self._approve(f"Create {len(missing)} missing shipped email templates?"):
            created = [name for name in missing if EmailTemplate.get_or_load_from_seed(name)]
            self._add("email", "pass", "templates.created", "Created missing shipped email templates", {"created": created}, changed=bool(created))
        elif missing:
            self._add("email", "warn", "templates.missing", "Shipped email templates are missing", {"missing": missing})
        else:
            self._add("email", "pass", "templates.complete", "All shipped email templates are present", {"count": len(seed_names)})

    def _list_paginated(self, client, method, result_key, **params):
        rows, token = [], None
        while True:
            request = dict(params)
            if token:
                request["NextToken"] = token
            response = getattr(client, method)(**request)
            rows.extend(response.get(result_key, []))
            token = response.get("NextToken")
            if not token:
                return rows

    def _desired_alarms(self):
        resources = []
        try:
            for page in self._client("ec2").get_paginator("describe_instances").paginate():
                for reservation in page.get("Reservations", []):
                    for row in reservation.get("Instances", []):
                        if row.get("State", {}).get("Name") == "running":
                            resources.append(("ec2", row.get("InstanceId"), "AWS/EC2", "InstanceId"))
            for page in self._client("rds").get_paginator("describe_db_instances").paginate():
                for row in page.get("DBInstances", []):
                    if row.get("DBInstanceStatus") == "available":
                        resources.append(("rds", row.get("DBInstanceIdentifier"), "AWS/RDS", "DBInstanceIdentifier"))
            for page in self._client("elasticache").get_paginator("describe_cache_clusters").paginate():
                for row in page.get("CacheClusters", []):
                    if row.get("CacheClusterStatus") == "available":
                        resources.append(("elasticache", row.get("CacheClusterId"), "AWS/ElastiCache", "CacheClusterId"))
        except ClientError as exc:
            self._add("monitoring", "fail", "resources.discovery_denied", exc, {"aws_code": _error_code(exc)})
            return []
        desired, slug = [], self._deployment_slug()
        for kind, resource_id, namespace, dimension in resources:
            profiles = [("cpu", "CPUUtilization", "Average", "GreaterThanOrEqualToThreshold", 90, 300, 3, 3)]
            if kind == "ec2":
                profiles.insert(0, ("status", "StatusCheckFailed", "Maximum", "GreaterThanOrEqualToThreshold", 1, 60, 2, 2))
            if kind == "rds":
                profiles.append(("free-storage", "FreeStorageSpace", "Average", "LessThanOrEqualToThreshold", 10 * 1024 ** 3, 300, 3, 3))
            for signal, metric, statistic, comparison, threshold, period, evaluation, datapoints in profiles:
                desired.append({
                    "AlarmName": f"django-mojo/{slug}/{kind}/{resource_id}/{signal}"[:255],
                    "Namespace": namespace, "MetricName": metric,
                    "Dimensions": [{"Name": dimension, "Value": resource_id}],
                    "Statistic": statistic, "ComparisonOperator": comparison,
                    "Threshold": threshold, "Period": period,
                    "EvaluationPeriods": evaluation, "DatapointsToAlarm": datapoints,
                    "TreatMissingData": "notBreaching",
                })
        return desired

    def check_monitoring(self):
        sns, cloudwatch = self._client("sns"), self._client("cloudwatch")
        slug = self._deployment_slug()
        topic_name = f"django-mojo-{slug}-operations"[:256]
        topic_arn = next((row.get("TopicArn") for row in self._list_paginated(sns, "list_topics", "Topics")
                          if row.get("TopicArn", "").rsplit(":", 1)[-1] == topic_name), None)
        if not topic_arn and self._approve(f"Create owned SNS topic {topic_name}?"):
            topic_arn = sns.create_topic(Name=topic_name, Tags=[
                {"Key": key, "Value": value} for key, value in sorted({**OWNERSHIP_TAGS, "deployment": slug}.items())
            ]).get("TopicArn")
            self._add("monitoring", "pass", "sns.topic_created", "Created operations SNS topic", {"topic_arn": topic_arn}, changed=True)
        elif not topic_arn:
            self._add("monitoring", "warn", "sns.topic_missing", "Operations SNS topic is missing",
                      remediation="Rerun with --apply after choosing AWS_MONITORING_NAME.")
            return
        else:
            tags = {row.get("Key"): row.get("Value") for row in sns.list_tags_for_resource(ResourceArn=topic_arn).get("Tags", [])}
            if not all(tags.get(key) == value for key, value in OWNERSHIP_TAGS.items()):
                self._add("monitoring", "fail", "sns.topic_conflict", "Same-name SNS topic is not django-mojo-owned", {"topic_arn": topic_arn})
                return
            self._add("monitoring", "pass", "sns.topic_owned", "Owned operations SNS topic exists", {"topic_arn": topic_arn})
        allowlist = _setting("AWS_CLOUDWATCH_ALARM_TOPIC_ARNS", [], kind="list") or []
        if topic_arn not in allowlist:
            self._add("monitoring", "pending", "sns.topic_not_allowlisted", "Operations topic ARN is not in the static receiver allowlist",
                      {"topic_arn": topic_arn}, remediation="Add the exact ARN to AWS_CLOUDWATCH_ALARM_TOPIC_ARNS, restart Django, then rerun.")
            return
        endpoint = (_setting("BASE_URL", "") or "").rstrip("/") + "/api/aws/cloudwatch/sns/alarm"
        subscriptions = self._list_paginated(sns, "list_subscriptions_by_topic", "Subscriptions", TopicArn=topic_arn)
        matching = [row for row in subscriptions if row.get("Protocol") == "https" and row.get("Endpoint") == endpoint]
        confirmed = any(row.get("SubscriptionArn") not in (None, "PendingConfirmation") for row in matching)
        if not matching and self._approve(f"Subscribe {endpoint} to {topic_arn}?"):
            arn = sns.subscribe(TopicArn=topic_arn, Protocol="https", Endpoint=endpoint, ReturnSubscriptionArn=True).get("SubscriptionArn")
            confirmed = arn != "PendingConfirmation"
            self._add("monitoring", "pass" if confirmed else "pending", "sns.subscription_created", "Created SNS HTTPS subscription", {"endpoint": endpoint}, changed=True)
        elif confirmed:
            self._add("monitoring", "pass", "sns.subscription_confirmed", "SNS HTTPS subscription is confirmed", {"endpoint": endpoint})
        elif matching:
            self._add("monitoring", "pending", "sns.subscription_pending", "SNS HTTPS subscription is pending confirmation", {"endpoint": endpoint})
        else:
            self._add("monitoring", "warn", "sns.subscription_missing", "SNS HTTPS subscription is missing", {"endpoint": endpoint})
        if not confirmed:
            return
        self.monitoring_ready = True
        desired, created, drifted, conflicts, uncreated = self._desired_alarms(), 0, [], [], 0
        for alarm in desired:
            alarm.update({"AlarmActions": [topic_arn], "OKActions": [topic_arn]})
            existing = cloudwatch.describe_alarms(AlarmNames=[alarm["AlarmName"]]).get("MetricAlarms", [])
            if existing:
                current = existing[0]
                tags = cloudwatch.list_tags_for_resource(ResourceARN=current.get("AlarmArn")).get("Tags", [])
                tag_map = {row.get("Key"): row.get("Value") for row in tags}
                if not all(tag_map.get(key) == value for key, value in OWNERSHIP_TAGS.items()):
                    conflicts.append(alarm["AlarmName"])
                else:
                    comparable = ("Namespace", "MetricName", "Statistic", "ComparisonOperator", "Threshold", "Period", "EvaluationPeriods", "DatapointsToAlarm", "TreatMissingData", "AlarmActions", "OKActions")
                    if any(current.get(key) != alarm.get(key) for key in comparable):
                        drifted.append(alarm["AlarmName"])
                continue
            if not self._approve(f"Create missing CloudWatch alarm {alarm['AlarmName']}?"):
                uncreated += 1
                continue
            if cloudwatch.describe_alarms(AlarmNames=[alarm["AlarmName"]]).get("MetricAlarms", []):
                continue
            cloudwatch.put_metric_alarm(**alarm, Tags=[
                {"Key": key, "Value": value} for key, value in sorted({**OWNERSHIP_TAGS, "deployment": slug}.items())
            ])
            created += 1
        if conflicts:
            self._add("monitoring", "fail", "alarms.name_conflict", "Non-owned alarms use reserved names", {"alarm_names": conflicts})
        if drifted:
            self._add("monitoring", "warn", "alarms.drifted", "Owned alarms differ from defaults and were preserved", {"alarm_names": drifted})
        self._add("monitoring", "warn" if uncreated else "pass", "alarms.profile", "CloudWatch alarm profile audited",
                  {"desired": len(desired), "created": created, "uncreated": uncreated}, changed=bool(created))
        from mojo.apps.aws.models import CloudWatchAlarmTransition
        received = CloudWatchAlarmTransition.objects.filter(topic_arn=topic_arn).order_by("-created").first()
        if received:
            self.delivery_seen = True
            self._add("monitoring", "pass", "receiver.delivery_seen", "A signed alarm transition has reached django-mojo", {"transition_id": received.pk})
        else:
            self._add("monitoring", "warn", "receiver.delivery_unverified", "No alarm transition receipt exists yet",
                      remediation="Use the documented AWS set-alarm-state ALARM then OK test on a disposable owned alarm.")

    def check_rules(self):
        from mojo.apps.aws.services.cloudwatch_alarms import SCOPE
        from mojo.apps.incident.models import RuleSet
        ruleset = RuleSet.objects.filter(category=SCOPE, name="AWS CloudWatch - Operations").first()
        if ruleset:
            self._add("rules", "pass", "rules.cloudwatch_present", "CloudWatch incident defaults are installed", {"ruleset_id": ruleset.pk})
        elif not (self.monitoring_ready and self.delivery_seen):
            self._add("rules", "pending", "rules.receiver_unverified",
                      "CloudWatch defaults wait until the receiver has a durable delivery receipt",
                      remediation="Run the controlled ALARM then OK probe and rerun all sections with --apply.")
        elif self._approve("Create the default CloudWatch incident RuleSet?"):
            ruleset, created = RuleSet.ensure_cloudwatch_rules()
            self._add("rules", "pass", "rules.cloudwatch_created", "Created CloudWatch incident defaults", {"ruleset_id": ruleset.pk}, changed=created)
        else:
            self._add("rules", "warn", "rules.cloudwatch_missing", "CloudWatch incident defaults are not installed",
                      remediation="Rerun with --apply after receiver delivery is verified.")
