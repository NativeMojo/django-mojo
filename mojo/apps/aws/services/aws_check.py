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
from objict import objict

from mojo.helpers import logit
from mojo.helpers.aws.client import get_client, get_session
from mojo.helpers.settings import settings


logger = logit.get_logger("aws_check", "aws.log")
STATUSES = ("pass", "warn", "fail", "pending", "skip")
SECTIONS = ("prerequisites", "identity", "cron", "s3", "email", "monitoring", "dns", "rules")
# `versions` is opt-in: it is selectable with --section but never part of a
# default run, and it can only ever report pass/warn/pending — never fail.
ALL_SECTIONS = SECTIONS + ("versions",)
OWNERSHIP_TAGS = {"managed-by": "django-mojo", "purpose": "cloudwatch-incidents"}

# The custom certificate-expiry metric. The publisher (dnsman) and the alarm
# (here) must agree on all three of namespace, name and the deployment
# dimension, or the alarm watches a metric nobody writes.
CERT_METRIC_NAMESPACE = "DjangoMojo/Certificates"
CERT_METRIC_NAME = "MinDaysToExpiry"


def _setting(name, default=None, kind=None):
    try:
        return settings.get_static(name, default, kind=kind) if kind else settings.get_static(name, default)
    except Exception:
        return default


def _error_code(exc):
    return str((getattr(exc, "response", {}).get("Error") or {}).get("Code") or "")


def _safe_slug(value):
    return (re.sub(r"[^a-z0-9-]+", "-", (value or "").lower()).strip("-")[:48] or "deployment")


def _alarm_name(slug, kind, resource_id, signal):
    """The single source of the owned alarm name format."""
    return f"django-mojo/{slug}/{kind}/{resource_id}/{signal}"[:255]


def _is_aurora_engine(engine):
    """True for every AWS Aurora engine id. Aurora publishes FreeLocalStorage, not FreeStorageSpace."""
    return str(engine or "").strip().lower().startswith("aurora")


# Burstable families throttle instead of failing when credits run out, and only
# they publish CPUCreditBalance. Alarming on a non-burstable instance would
# create an alarm whose metric never reports, which reads as permanently green.
BURSTABLE_EC2_PREFIXES = ("t2.", "t3.", "t3a.", "t4g.")


def _is_burstable_ec2(instance_type):
    return str(instance_type or "").strip().lower().startswith(BURSTABLE_EC2_PREFIXES)


def _is_burstable_rds(db_class):
    return str(db_class or "").strip().lower().startswith("db.t")


def _arn_suffix(arn, *prefixes):
    """
    The ELBv2 dimension value CloudWatch expects — the ARN *suffix*, not the ARN.

    A target group is `targetgroup/<name>/<id>` and a load balancer is
    `net/<name>/<id>` (or `app/...`). Passing the full ARN creates an alarm
    successfully that then never receives a datapoint, so it sits green forever.
    """
    text = str(arn or "")
    for prefix in prefixes:
        index = text.find(f"{prefix}/")
        if index != -1:
            return text[index:]
    return ""


def deployment_slug():
    """
    The deployment's stable identity, used as an owning tag, an alarm-name
    segment, and the dimension of the certificate-expiry metric.

    Module-level and public on purpose: the metric publisher lives in another app
    and MUST derive the same value from the same code. Two copies that drift
    produce an alarm watching a metric nobody publishes — green forever.
    """
    configured = _setting("AWS_MONITORING_NAME", "") or ""
    host = urlparse(_setting("BASE_URL", "") or "").hostname or "deployment"
    return _safe_slug(configured or host)


def _profile(signal, metric, statistic, comparison, threshold,
             period=300, evaluation=3, datapoints=3, treat_missing="notBreaching"):
    """
    One alarm profile.

    `treat_missing` defaults to notBreaching — the right answer for a metric that
    is simply quiet. Signals where the ABSENCE of data is itself the failure pass
    "breaching" explicitly.
    """
    return objict(
        signal=signal, metric=metric, statistic=statistic, comparison=comparison,
        threshold=threshold, period=period, evaluation=evaluation,
        datapoints=datapoints, treat_missing=treat_missing,
    )


class AWSCheckRunner:
    def __init__(self, region=None, profile=None, timeout=3, apply=False, yes=False,
                 probe_s3=False, bucket_name=None, mailbox_email=None,
                 adopt_bucket=False, confirm=None, clients=None, session=None, now=None,
                 dns_domain=None, dns_group=None):
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
        self.aurora_instance_ids = []
        self.delivery_seen = False
        self.dns_domain = dns_domain
        self.dns_group = dns_group
        # DNSMAN_ACME_HUB_API_KEY belongs here too: the dns section surfaces hub
        # client errors through _add(), and #1423 requires that key never be
        # logged. Without it a hub failure reaches stdout and --json in the clear.
        self._secrets = [value for value in (
            _setting("AWS_KEY"), _setting("AWS_SECRET"),
            _setting("DNSMAN_ACME_HUB_API_KEY"),
        ) if value]

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

    def _ensure_mutation_identity(self, section):
        """Fail closed before any apply-mode AWS mutation, even in section runs."""
        if self.primary_identity:
            return True
        try:
            self.primary_identity = self._identity()
            self._add(section, "pass", "mutation.identity_verified",
                      "Selected AWS identity is verified for apply mode", self.primary_identity)
            return True
        except (NoCredentialsError, PartialCredentialsError) as exc:
            code = "credentials.missing"
            error = exc
        except (ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError) as exc:
            code = "sts.timeout"
            error = exc
        except (ClientError, BotoCoreError) as exc:
            code = "sts.denied" if isinstance(exc, ClientError) else "sts.error"
            error = exc
        self._add(section, "fail", f"mutation.{code}", error,
                  remediation="Verify the selected AWS identity before rerunning apply mode.")
        return False

    def _session(self, access_key=None, secret_key=None, region=None):
        if access_key is None and secret_key is None and self.session is not None:
            return self.session
        if access_key is None and secret_key is None and not self.profile:
            access_key = _setting("AWS_KEY")
            secret_key = _setting("AWS_SECRET")
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
        unknown = sorted(set(selected) - set(ALL_SECTIONS))
        if unknown:
            raise ValueError(f"unknown section(s): {', '.join(unknown)}")
        # Iterate ALL_SECTIONS, not SECTIONS: `selected` still DEFAULTS to
        # SECTIONS, but an explicitly requested opt-in section must actually
        # execute rather than validate and then do nothing.
        for section in ALL_SECTIONS:
            if section not in selected:
                continue
            try:
                getattr(self, f"check_{section}")()
            except (NoCredentialsError, PartialCredentialsError) as exc:
                self._add(section, "fail", f"{section}.credentials_missing", exc,
                          remediation="Configure a complete key pair, profile, or task/instance role.")
            except (ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError) as exc:
                self._add(section, "fail", f"{section}.service_unreachable", exc,
                          remediation="Check AWS network access, endpoint and selected region, then rerun.")
            except ClientError as exc:
                aws_code = _error_code(exc)
                code = "denied" if aws_code in ("403", "AccessDenied", "AccessDeniedException") else "service_error"
                self._add(section, "fail", f"{section}.{code}", exc, {"aws_code": aws_code})
            except BotoCoreError as exc:
                self._add(section, "fail", f"{section}.service_error", exc)
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
        return deployment_slug()

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
        block = s3.get_public_access_block(Bucket=bucket).get("PublicAccessBlockConfiguration", {})
        if not all(block.get(key) is True for key in (
            "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets",
        )):
            raise RuntimeError("bucket adoption requires all public-access block controls")
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
        if self.apply and not self._ensure_mutation_identity("s3"):
            return
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
            if actual and actual != region:
                self._add("s3", "fail", "bucket.region_mismatch",
                          "S3 bucket region differs from the FileManager configuration",
                          {"bucket": bucket, "bucket_region": actual, "configured_region": region},
                          remediation="Correct the FileManager AWS region; aws-check will not rewrite it.")
                return
            self._add("s3", "pass", "bucket.accessible", "S3 bucket is accessible",
                      {"bucket": bucket, "region": actual or region})
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
                    client_factory=self._email_client_factory(domain),
                )
                pending = sorted(key for key, value in report.checks.items() if value is False)
                self._add("email", "pass" if report.audit_pass else "pending", "email.domain_audit",
                          f"SES audit completed for {domain.name}", {"domain": domain.name, "pending_checks": pending})
                if self.apply and self._email_has_create_candidates(domain, report):
                    if not self._ensure_mutation_identity("email"):
                        continue
                    if self._approve(f"Create only missing SES identity/topic wiring for {domain.name}?"):
                        changed = self._create_missing_email_resources(domain, report)
                        self._add(
                            "email", "pending" if changed else "warn", "email.missing_resources_created",
                            "Created missing SES resources; external verification may still be pending"
                            if changed else "No unambiguous SES resources were eligible for creation",
                            {"domain": domain.name, "created": changed}, changed=bool(changed),
                        )
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

    def _email_has_create_candidates(self, domain, report):
        by_resource = {item.resource: item for item in report.items}
        identity = by_resource.get("ses.identity.verification")
        dkim = by_resource.get("ses.identity.dkim")
        identity_missing = bool(identity and identity.current is None)
        dkim_missing = bool(dkim and not (dkim.current or {}).get("VerificationStatus")) \
            if dkim and isinstance(dkim.current, dict) else False
        topic_fields = (
            "sns_topic_bounce_arn", "sns_topic_complaint_arn", "sns_topic_delivery_arn",
        )
        if domain.receiving_enabled:
            topic_fields += ("sns_topic_inbound_arn",)
        return identity_missing or dkim_missing or any(not getattr(domain, field, None) for field in topic_fields)

    def _email_client_factory(self, domain):
        """Keep every email audit call in the record's or runner's selected context."""
        def factory(service, **kwargs):
            if domain.aws_key or domain.aws_secret:
                return self._client(
                    service, domain.aws_key, domain.aws_secret,
                    domain.aws_region or self.region,
                )
            return self._client(service)
        return factory

    def _create_missing_email_resources(self, domain, report):
        """Create only absent SES identity/SNS wiring; preserve every non-empty drift."""
        if any(item.status == "conflict" for item in report.items):
            return []
        ses, sns = self._client("ses"), self._client("sns")
        created = []
        by_resource = {item.resource: item for item in report.items}
        identity = by_resource.get("ses.identity.verification")
        if identity and identity.current is None:
            ses.verify_domain_identity(Domain=domain.name)
            created.append("ses-identity")
        dkim = by_resource.get("ses.identity.dkim")
        if dkim and isinstance(dkim.current, dict) and not dkim.current.get("VerificationStatus"):
            ses.verify_domain_dkim(Domain=domain.name)
            created.append("ses-dkim")

        attributes = ses.get_identity_notification_attributes(Identities=[domain.name])
        current = (attributes.get("NotificationAttributes", {}).get(domain.name, {}) or {})
        topic_rows = self._list_paginated(sns, "list_topics", "Topics")
        topics_by_name = {
            row.get("TopicArn", "").rsplit(":", 1)[-1]: row.get("TopicArn") for row in topic_rows
        }
        safe_domain = domain.name.replace(".", "-")
        fields = {
            "bounce": "sns_topic_bounce_arn", "complaint": "sns_topic_complaint_arn",
            "delivery": "sns_topic_delivery_arn",
        }
        if domain.receiving_enabled:
            fields["inbound"] = "sns_topic_inbound_arn"
        notification_names = {"bounce": "Bounce", "complaint": "Complaint", "delivery": "Delivery"}
        expected_tags = {
            "managed-by": "django-mojo", "purpose": "ses-notifications",
            "deployment": self._deployment_slug(), "domain": domain.name,
        }
        updates = []
        topic_arns = {}
        for kind, field in fields.items():
            configured = getattr(domain, field, None)
            if configured:
                topic_arns[kind] = configured
                continue
            notification = notification_names.get(kind)
            existing_mapping = current.get(f"{notification}Topic") if notification else None
            if existing_mapping:
                setattr(domain, field, existing_mapping)
                updates.append(field)
                topic_arns[kind] = existing_mapping
                created.append(f"topic-reference:{kind}")
                continue
            name = f"ses-{safe_domain}-{kind}"
            arn = topics_by_name.get(name)
            if arn:
                tags = {
                    row.get("Key"): row.get("Value") for row in
                    sns.list_tags_for_resource(ResourceArn=arn).get("Tags", [])
                }
                if not all(tags.get(key) == value for key, value in expected_tags.items()):
                    raise RuntimeError(
                        f"same-name SES topic {name} is not owned by this deployment/domain"
                    )
            if not arn:
                arn = sns.create_topic(Name=name, Tags=[
                    {"Key": key, "Value": value} for key, value in sorted(expected_tags.items())
                ]).get("TopicArn")
                created.append(f"sns-topic:{kind}")
            if arn:
                setattr(domain, field, arn)
                updates.append(field)
                topic_arns[kind] = arn
        if updates:
            domain.save(update_fields=updates + ["modified"])

        for notification, kind in (("Bounce", "bounce"), ("Complaint", "complaint"), ("Delivery", "delivery")):
            arn = topic_arns.get(kind)
            existing = current.get(f"{notification}Topic")
            if arn and not existing:
                ses.set_identity_notification_topic(
                    Identity=domain.name, NotificationType=notification, SnsTopic=arn,
                )
                created.append(f"ses-mapping:{kind}")
        metadata = domain.metadata if isinstance(domain.metadata, dict) else {}
        for kind, arn in topic_arns.items():
            endpoint = metadata.get(f"{kind}_endpoint") or metadata.get(f"sns_{kind}_endpoint")
            if not endpoint:
                continue
            subscriptions = self._list_paginated(
                sns, "list_subscriptions_by_topic", "Subscriptions", TopicArn=arn,
            )
            matching = [row for row in subscriptions if
                        row.get("Protocol") == "https" and row.get("Endpoint") == endpoint]
            if not matching:
                sns.subscribe(
                    TopicArn=arn, Protocol="https", Endpoint=endpoint,
                    ReturnSubscriptionArn=True,
                )
                created.append(f"sns-subscription:{kind}")
        return created

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

    def _discover_resources(self):
        """Every alarmable resource, as objict rows. Raises ClientError to the caller."""
        resources = []
        for page in self._client("ec2").get_paginator("describe_instances").paginate():
            for reservation in page.get("Reservations", []):
                for row in reservation.get("Instances", []):
                    if row.get("State", {}).get("Name") == "running":
                        resources.append(objict(
                            kind="ec2", resource_id=row.get("InstanceId"),
                            namespace="AWS/EC2", engine="",
                            instance_type=row.get("InstanceType", ""),
                            dimensions=[{"Name": "InstanceId", "Value": row.get("InstanceId")}],
                        ))
        for page in self._client("rds").get_paginator("describe_db_instances").paginate():
            for row in page.get("DBInstances", []):
                if row.get("DBInstanceStatus") == "available":
                    resources.append(objict(
                        kind="rds", resource_id=row.get("DBInstanceIdentifier"),
                        namespace="AWS/RDS", engine=row.get("Engine", ""),
                        instance_type=row.get("DBInstanceClass", ""),
                        dimensions=[{"Name": "DBInstanceIdentifier",
                                     "Value": row.get("DBInstanceIdentifier")}],
                    ))
        for page in self._client("elasticache").get_paginator("describe_cache_clusters").paginate():
            for row in page.get("CacheClusters", []):
                if row.get("CacheClusterStatus") == "available":
                    resources.append(objict(
                        kind="elasticache", resource_id=row.get("CacheClusterId"),
                        namespace="AWS/ElastiCache", engine="", instance_type="",
                        dimensions=[{"Name": "CacheClusterId", "Value": row.get("CacheClusterId")}],
                    ))
        resources.extend(self._discover_target_groups())
        return resources

    def _discover_target_groups(self):
        """
        ELBv2 target groups, which carry the health of the whole serving tier.

        `mojo.deploy.check_setup` also inspects load balancers, but it never
        creates anything — the alarm inventory is owned here so that every alarm
        reaches the single owned SNS topic and its one allowlist entry.
        """
        rows = []
        for page in self._client("elbv2").get_paginator("describe_target_groups").paginate():
            for row in page.get("TargetGroups", []):
                balancers = row.get("LoadBalancerArns") or []
                if not balancers:
                    # An unattached target group publishes no metrics at all, so
                    # an alarm on it would sit in INSUFFICIENT_DATA forever.
                    continue
                group_suffix = _arn_suffix(row.get("TargetGroupArn"), "targetgroup")
                # A target group shared by two load balancers is alarmed on the
                # first; the metric is per (TargetGroup, LoadBalancer) pair.
                balancer_suffix = _arn_suffix(balancers[0], "app", "net")
                if not group_suffix or not balancer_suffix:
                    continue
                namespace = ("AWS/ApplicationELB" if balancer_suffix.startswith("app/")
                             else "AWS/NetworkELB")
                rows.append(objict(
                    kind="elbv2",
                    # `/` would add path segments to the alarm name and stop it
                    # round-tripping through describe_alarms(AlarmNames=[...]).
                    resource_id=group_suffix.replace("/", "~"),
                    namespace=namespace, engine="", instance_type="",
                    dimensions=[
                        {"Name": "TargetGroup", "Value": group_suffix},
                        {"Name": "LoadBalancer", "Value": balancer_suffix},
                    ],
                ))
        return rows

    def _resource_profiles(self, resource):
        """The alarm profiles a single discovered resource earns."""
        if resource.kind == "elbv2":
            return [
                _profile("unhealthy-hosts", "UnHealthyHostCount", "Maximum",
                         "GreaterThanOrEqualToThreshold", 1, period=60,
                         evaluation=2, datapoints=2),
                # UnHealthyHostCount counts unhealthy REGISTERED targets, so a
                # target group whose targets were all deregistered reports 0 and
                # reads as healthy. HealthyHostCount is the only signal that sees
                # the total outage, and missing data means the balancer stopped
                # reporting — which is the outage too.
                _profile("healthy-hosts", "HealthyHostCount", "Minimum",
                         "LessThanThreshold", 1, period=60, evaluation=2,
                         datapoints=2, treat_missing="breaching"),
            ]

        profiles = [_profile("cpu", "CPUUtilization", "Average",
                             "GreaterThanOrEqualToThreshold", 90)]
        if resource.kind == "ec2":
            profiles.insert(0, _profile("status", "StatusCheckFailed", "Maximum",
                                        "GreaterThanOrEqualToThreshold", 1,
                                        period=60, evaluation=2, datapoints=2))
        if resource.kind == "rds" and not _is_aurora_engine(resource.engine):
            profiles.append(_profile("free-storage", "FreeStorageSpace", "Average",
                                     "LessThanOrEqualToThreshold", 10 * 1024 ** 3))
        elif resource.kind == "rds":
            self.aurora_instance_ids.append(resource.resource_id)

        burstable = (
            (resource.kind == "ec2" and _is_burstable_ec2(resource.instance_type))
            or (resource.kind == "rds" and _is_burstable_rds(resource.instance_type))
        )
        if burstable:
            profiles.append(_profile(
                "cpu-credits", "CPUCreditBalance", "Average", "LessThanOrEqualToThreshold",
                int(_setting("AWS_CHECK_CPU_CREDIT_FLOOR", 20) or 20)))
        if resource.kind == "elasticache":
            # Not a high-water mark: ANY sustained eviction means the working set
            # no longer fits and entries are being discarded. Deliberately not a
            # setting — a non-zero threshold would silence the signal itself.
            profiles.append(_profile("evictions", "Evictions", "Sum",
                                     "GreaterThanThreshold", 0))
        if resource.kind == "rds":
            profiles.append(_profile(
                "freeable-memory", "FreeableMemory", "Average", "LessThanOrEqualToThreshold",
                int(_setting("AWS_CHECK_RDS_FREEABLE_MEMORY_FLOOR", 256 * 1024 ** 2)
                    or 256 * 1024 ** 2)))
            # Deliberately forgiving: RDS derives max_connections from instance
            # memory (~112 on db.t3.micro, ~405 on db.t3.medium), so no single
            # default is right everywhere. Erring high means it never fires
            # spuriously — a chronically-firing alarm gets muted, and muting this
            # topic silences every other alarm with it. Operators on a small class
            # lower it; see the settings reference.
            profiles.append(_profile(
                "connections", "DatabaseConnections", "Maximum",
                "GreaterThanOrEqualToThreshold",
                int(_setting("AWS_CHECK_RDS_MAX_CONNECTIONS", 500) or 500)))
        return profiles

    def _desired_alarms(self):
        self.aurora_instance_ids = []
        try:
            resources = self._discover_resources()
        except ClientError as exc:
            self._add("monitoring", "fail", "resources.discovery_denied", exc, {"aws_code": _error_code(exc)})
            return []
        desired, slug = [], self._deployment_slug()
        for resource in resources:
            for profile in self._resource_profiles(resource):
                desired.append({
                    "AlarmName": _alarm_name(slug, resource.kind, resource.resource_id, profile.signal),
                    "Namespace": resource.namespace, "MetricName": profile.metric,
                    "Dimensions": [dict(row) for row in resource.dimensions],
                    "Statistic": profile.statistic, "ComparisonOperator": profile.comparison,
                    "Threshold": profile.threshold, "Period": profile.period,
                    "EvaluationPeriods": profile.evaluation, "DatapointsToAlarm": profile.datapoints,
                    "TreatMissingData": profile.treat_missing,
                })
        return desired

    def _desired_deployment_alarms(self, cloudwatch):
        """
        Deployment-wide alarms, which belong to no discovered resource.

        Today that is the certificate-expiry alarm: one signal that catches every
        cause of a stalled renewal at once — publisher down, challenge misrouted,
        credentials wrong, delegation record deleted.
        """
        slug = self._deployment_slug()
        dimensions = [{"Name": "Deployment", "Value": slug}]
        try:
            published = cloudwatch.list_metrics(
                Namespace=CERT_METRIC_NAMESPACE, MetricName=CERT_METRIC_NAME,
                Dimensions=dimensions,
            ).get("Metrics") or []
        except ClientError as exc:
            # Must not escape: this runs outside _discover_resources' guard, and
            # run()'s per-section handler would abort the ENTIRE monitoring
            # section — no SNS audit, no alarm inventory — on a deployment whose
            # IAM policy predates cloudwatch:ListMetrics.
            self._add("monitoring", "pending", "alarms.cert_metric_unknown", exc,
                      {"aws_code": _error_code(exc)},
                      remediation="Grant cloudwatch:ListMetrics to the selected identity, then rerun.")
            return []
        if not published:
            self._add("monitoring", "pending", "alarms.cert_metric_unpublished",
                      "No certificate-expiry metric has been published yet",
                      {"namespace": CERT_METRIC_NAMESPACE, "deployment": slug},
                      remediation=("Run the dnsman cron that publishes it "
                                   "(mojo.apps.dnsman.cronjobs.publish_certificate_expiry), "
                                   "then rerun. The alarm is deliberately not created first: "
                                   "it treats missing data as breaching and would page immediately."))
            return []
        return [{
            "AlarmName": _alarm_name(slug, "certificates", slug, "expiry"),
            "Namespace": CERT_METRIC_NAMESPACE, "MetricName": CERT_METRIC_NAME,
            "Dimensions": dimensions,
            "Statistic": "Minimum", "ComparisonOperator": "LessThanOrEqualToThreshold",
            "Threshold": int(_setting("AWS_CHECK_CERT_EXPIRY_DAYS", 14) or 14),
            "Period": 3600, "EvaluationPeriods": 3, "DatapointsToAlarm": 3,
            # The point of the alarm, and deliberately opposite to every other
            # profile. A dead publisher must alarm, otherwise the monitoring
            # fails silently in exactly the scenario it exists to catch. Not a
            # setting: flipping this to notBreaching deletes the control.
            "TreatMissingData": "breaching",
        }]

    def _stale_aurora_storage_alarms(self, cloudwatch, expected_tags, slug):
        """Owned FreeStorageSpace alarms left on Aurora instances by earlier django-mojo versions."""
        names = [_alarm_name(slug, "rds", resource_id, "free-storage")
                 for resource_id in self.aurora_instance_ids]
        if not names:
            return []
        stale = []
        for start in range(0, len(names), 100):
            chunk, token = names[start:start + 100], None
            while True:
                request = {"AlarmNames": chunk, "AlarmTypes": ["MetricAlarm"]}
                if token:
                    request["NextToken"] = token
                response = cloudwatch.describe_alarms(**request)
                for alarm in response.get("MetricAlarms", []):
                    if alarm.get("MetricName") != "FreeStorageSpace" or alarm.get("Namespace") != "AWS/RDS":
                        continue
                    tags = cloudwatch.list_tags_for_resource(
                        ResourceARN=alarm.get("AlarmArn"),
                    ).get("Tags", [])
                    tag_map = {row.get("Key"): row.get("Value") for row in tags}
                    if all(tag_map.get(key) == value for key, value in expected_tags.items()):
                        stale.append(alarm.get("AlarmName"))
                token = response.get("NextToken")
                if not token:
                    break
        return stale

    def _report_aurora_storage(self, cloudwatch, expected_tags, slug):
        """Aurora has no per-instance free-space metric, so the gap must never read as pass."""
        if self.aurora_instance_ids:
            self._add("monitoring", "warn", "alarms.aurora_storage_unmonitored",
                      "Aurora instances have no storage alarm because Aurora publishes no FreeStorageSpace metric",
                      {"instance_ids": list(self.aurora_instance_ids)},
                      remediation=("Aurora storage is a shared auto-scaling cluster volume. Track local "
                                   "storage with a FreeLocalStorage alarm sized to each instance class, "
                                   "or watch the cluster volume in the RDS console until django-mojo "
                                   "ships that profile."))
        stale = self._stale_aurora_storage_alarms(cloudwatch, expected_tags, slug)
        if stale:
            self._add("monitoring", "warn", "alarms.stale_aurora_storage",
                      "Owned FreeStorageSpace alarms on Aurora instances can never report and stay green",
                      {"alarm_names": stale},
                      remediation=("aws-check never deletes AWS resources. Remove them manually: "
                                   f"aws cloudwatch delete-alarms --alarm-names {' '.join(stale)}"))

    def check_monitoring(self):
        if self.apply and not self._ensure_mutation_identity("monitoring"):
            return
        sns, cloudwatch = self._client("sns"), self._client("cloudwatch")
        slug = self._deployment_slug()
        expected_tags = {**OWNERSHIP_TAGS, "deployment": slug}
        # Discovery runs above every early return below: an un-allowlisted or unconfirmed
        # deployment is the normal mid-setup state, and the Aurora storage gap plus any
        # already-created false alarm must still be reported there.
        desired = self._desired_alarms() + self._desired_deployment_alarms(cloudwatch)
        self._report_aurora_storage(cloudwatch, expected_tags, slug)
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
            if not all(tags.get(key) == value for key, value in expected_tags.items()):
                self._add("monitoring", "fail", "sns.topic_conflict",
                          "Same-name SNS topic is not owned by this django-mojo deployment",
                          {"topic_arn": topic_arn})
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
        created, drifted, conflicts, uncreated = 0, [], [], 0
        for alarm in desired:
            alarm.update({"AlarmActions": [topic_arn], "OKActions": [topic_arn]})
            existing = cloudwatch.describe_alarms(AlarmNames=[alarm["AlarmName"]]).get("MetricAlarms", [])
            if existing:
                current = existing[0]
                tags = cloudwatch.list_tags_for_resource(ResourceARN=current.get("AlarmArn")).get("Tags", [])
                tag_map = {row.get("Key"): row.get("Value") for row in tags}
                if not all(tag_map.get(key) == value for key, value in expected_tags.items()):
                    conflicts.append(alarm["AlarmName"])
                else:
                    comparable = ("Namespace", "MetricName", "Statistic", "ComparisonOperator", "Threshold", "Period", "EvaluationPeriods", "DatapointsToAlarm", "TreatMissingData", "AlarmActions", "OKActions")
                    current_dimensions = sorted(
                        (row.get("Name"), row.get("Value")) for row in current.get("Dimensions", [])
                    )
                    desired_dimensions = sorted(
                        (row.get("Name"), row.get("Value")) for row in alarm.get("Dimensions", [])
                    )
                    if current_dimensions != desired_dimensions or any(
                        current.get(key) != alarm.get(key) for key in comparable
                    ):
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

    def check_dns(self):
        """
        Audit dnsman's ACME state, and under --apply bootstrap one domain.

        This lives in aws-check rather than a separate dnsman_bootstrap command
        so an operator learns one tool: it inherits the audit/apply split,
        per-action confirmation, --json, the exit-code contract and secret
        redaction for free.
        """
        from django.apps import apps as django_apps

        if not django_apps.is_installed("mojo.apps.dnsman"):
            self._add("dns", "skip", "dnsman.not_installed",
                      "dnsman is not installed, so there is no DNS or ACME state to audit")
            return
        self._check_dns_acme_account()
        self._check_dns_delegations()
        self._check_dns_certificates()
        if self.apply and self.dns_domain:
            self._bootstrap_dns_domain()
        elif self.apply:
            self._add("dns", "warn", "dns.no_bootstrap_requested",
                      "No domain was named, so nothing was bootstrapped",
                      remediation="Rerun with --apply --dns-domain example.com --dns-group <id-or-name>.")

    def _check_dns_acme_account(self):
        from mojo.apps.dnsman.models import AcmeAccount
        from mojo.apps.dnsman.services import certs

        url = certs.directory_url()
        account = AcmeAccount.objects.filter(directory_url=url).first()
        # The default is Let's Encrypt STAGING, deliberately, so an unconfigured
        # deploy cannot burn production rate limits. Bootstrapping against
        # staging while believing you are live is the likeliest way to misread
        # this section, so name it rather than printing a bare URL.
        staging = "staging" in url
        details = {"directory_url": url, "staging": staging,
                   "account_status": account.status if account else None}
        if staging:
            self._add("dns", "warn", "acme.staging_directory",
                      "ACME is pointed at Let's Encrypt STAGING; certificates issued "
                      "here are not trusted by browsers", details,
                      remediation="Set DNSMAN_ACME_DIRECTORY_URL to the production directory when ready.")
        elif account is None:
            self._add("dns", "pending", "acme.account_missing",
                      "No ACME account is registered for this directory yet", details,
                      remediation="The account registers itself on first issuance; request a certificate.")
        else:
            self._add("dns", "pass", "acme.account_registered",
                      "A production ACME account is registered", details)

    def _check_dns_delegations(self):
        from mojo.apps.dnsman.models import AcmeDelegation
        from mojo.apps.dnsman.models.acme_delegation import STATE_BROKEN
        from mojo.apps.dnsman.services import delegation

        available = delegation.is_available()
        # An unconfigured hub is not a fault: direct Route53/GoDaddy issuance is
        # a fully supported path and #1423 is explicit that an absent hub only
        # means delegation is unavailable.
        self._add("dns", "pass",
                  "delegation.available" if available else "delegation.direct_only",
                  "Delegated ACME is configured" if available
                  else "No ACME hub is configured; issuance uses the domain's own DNS provider",
                  {"delegated": available})
        counts = {}
        for row in AcmeDelegation.objects.all().order_by("id")[:500]:
            counts[row.state] = counts.get(row.state, 0) + 1
        broken = list(AcmeDelegation.objects.filter(state=STATE_BROKEN)
                      .values_list("domain_name", "last_error_code")[:20])
        if broken:
            self._add("dns", "warn", "delegation.broken",
                      "Delegations are broken and will not renew",
                      {"counts": counts,
                       "broken": [{"domain": name, "error": code} for name, code in broken]},
                      remediation="Re-verify the _acme-challenge CNAME for each domain listed.")
        elif counts:
            self._add("dns", "pass", "delegation.states", "ACME delegations audited", {"counts": counts})

    def _check_dns_certificates(self):
        from mojo.apps.dnsman.models import Certificate
        from mojo.apps.dnsman.services import certs

        renew_days = certs.renew_days()
        rows = list(Certificate.objects.filter(not_after__isnull=False)
                    .order_by("not_after")[:100])
        total = Certificate.objects.filter(not_after__isnull=False).count()
        if not rows:
            self._add("dns", "pending", "certificates.none",
                      "No issued certificates exist yet", {"total": 0})
            return
        expired = [{"common_name": row.common_name, "days_remaining": row.days_remaining}
                   for row in rows if (row.days_remaining or 0) <= 0]
        renewing = [{"common_name": row.common_name, "days_remaining": row.days_remaining}
                    for row in rows if 0 < (row.days_remaining or 0) <= renew_days]
        details = {"total": total, "shown": len(rows), "renew_days": renew_days,
                   "soonest": rows[0].days_remaining}
        if expired:
            self._add("dns", "fail", "certificates.expired",
                      "Certificates have expired", dict(details, expired=expired),
                      remediation=("Fix the renewal cause, then revoke or delete any row that is "
                                   "permanently dead — an expired row keeps the expiry alarm firing."))
        if renewing:
            self._add("dns", "warn", "certificates.renewing",
                      "Certificates are inside the renewal window",
                      dict(details, renewing=renewing))
        if not expired and not renewing:
            self._add("dns", "pass", "certificates.healthy",
                      "Every certificate is outside its renewal window", details)

    def _resolve_dns_group(self):
        from mojo.apps.account.models import Group

        value = str(self.dns_group or "").strip()
        if not value:
            return None
        group = Group.objects.filter(pk=value).first() if value.isdigit() else None
        if group is None:
            matches = list(Group.objects.filter(name__iexact=value).order_by("id")[:2])
            if len(matches) > 1:
                return None
            group = matches[0] if matches else None
        return group

    def _bootstrap_dns_domain(self):
        """
        Allocate a delegation, surface the CNAME, and request a certificate.

        Deliberately does NOT call _ensure_mutation_identity: every other apply
        path does, but those mutate AWS. This one writes dnsman rows and talks to
        the ACME hub over HTTPS, so gating it on verified AWS credentials would
        refuse the bootstrap on a correctly-configured box that has no AWS keys.
        """
        from mojo.apps.dnsman.services import certs, delegation

        group = self._resolve_dns_group()
        if group is None:
            self._add("dns", "fail", "dns.group_required",
                      "A single active group must be named to own the delegation",
                      {"domain": self.dns_domain, "requested_group": self.dns_group},
                      remediation="Rerun with --dns-group <group id or exact name>.")
            return
        if not self._approve(f"Allocate an ACME delegation for {self.dns_domain} under group {group.name}?"):
            self._add("dns", "warn", "delegation.not_allocated",
                      "Delegation allocation was not approved", {"domain": self.dns_domain})
            return
        try:
            row = delegation.initiate(group, None, name=self.dns_domain)
        except Exception as exc:
            self._add("dns", "fail", "delegation.allocate_failed", exc, {"domain": self.dns_domain})
            return
        self._add("dns", "pending", "delegation.cname_required",
                  f"Publish this CNAME, then rerun: {row.source_name} -> {row.target_name}",
                  {"domain": row.domain_name, "source_name": row.source_name,
                   "target_name": row.target_name, "state": row.state},
                  remediation="The domain owner adds the CNAME above at their DNS provider.")
        try:
            delegation.prove_alias(row)
        except Exception as exc:
            # Stop here on purpose. Let's Encrypt rate-limits failed validations
            # at 5 per account per hostname per hour, so firing at an
            # unpropagated record burns the budget and blocks retries for an
            # hour — during a bootstrap, when someone is iterating. The lookup
            # is free; the rate limit costs an hour.
            self._add("dns", "pending", "delegation.cname_unverified", exc,
                      {"domain": row.domain_name, "source_name": row.source_name,
                       "target_name": row.target_name},
                      remediation=("Wait for the CNAME to propagate and rerun. No certificate was "
                                   "requested, so no Let's Encrypt validation budget was consumed."))
            return
        try:
            row = delegation.verify(row)
        except Exception as exc:
            self._add("dns", "fail", "delegation.verify_failed", exc, {"domain": row.domain_name})
            return
        self._add("dns", "pass", "delegation.verified",
                  "The delegation CNAME is authoritatively verified",
                  {"domain": row.domain_name}, changed=True)
        if not self._approve(f"Request a certificate for {row.domain_name}?"):
            self._add("dns", "warn", "certificate.not_requested",
                      "Certificate issuance was not approved", {"domain": row.domain_name})
            return
        try:
            certificate = certs.request_certificate(row.domain)
        except Exception as exc:
            self._add("dns", "fail", "certificate.request_failed", exc, {"domain": row.domain_name})
            return
        # Never `pass`: issuance runs on the job runner, so this command cannot
        # observe the outcome — only that the work was queued.
        self._add("dns", "pending", "certificate.requested",
                  "Certificate issuance is queued",
                  {"domain": row.domain_name, "certificate_id": certificate.pk,
                   "status": certificate.status}, changed=True,
                  remediation="Rerun --section dns once the job runner has processed it.")

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
        # Version-drift events are generated in-process, so this RuleSet does
        # NOT sit behind the monitoring_ready/delivery_seen gate above — that
        # gate exists because CloudWatch alarms need a confirmed SNS receiver.
        from mojo.apps.aws.services.version_drift import RULESET_CATEGORY
        versions = RuleSet.objects.filter(
            category=RULESET_CATEGORY, name="Health - AWS Version Drift").first()
        if versions:
            self._add("rules", "pass", "rules.aws_versions_present",
                      "AWS version drift incident defaults are installed", {"ruleset_id": versions.pk})
        elif self._approve("Create the AWS version drift incident RuleSet?"):
            versions, created = RuleSet.ensure_aws_version_rules()
            self._add("rules", "pass", "rules.aws_versions_created",
                      "Created AWS version drift incident defaults",
                      {"ruleset_id": versions.pk}, changed=created)
        else:
            self._add("rules", "warn", "rules.aws_versions_missing",
                      "AWS version drift incident defaults are not installed",
                      remediation=("Rerun with --apply --section rules and set "
                                   "AWS_VERSION_DRIFT_ENABLED=True to schedule the scan."))

    def check_versions(self):
        """Opt-in managed-service version inventory. Never reports `fail`.

        AccessDenied is the expected first-run outcome given the extra IAM
        actions this needs, so every failure path returns early with `warn` or
        `pending` BEFORE run()'s per-section wrapper — which would call
        _add(..., "fail", ...) and make `aws-check --check --section versions`
        exit 1 in CI.
        """
        from mojo.apps.aws.services.version_drift import VersionDriftScanner
        try:
            report = VersionDriftScanner(
                region=self.region, profile=self.profile,
                timeout=self.timeout, clients=self.clients,
            ).scan()
        except Exception as exc:
            self._add("versions", "warn", "versions.scan_error", exc,
                      remediation="Rerun: manage.py aws-check --check --section versions")
            return
        if report.get("status") != "ok":
            self._add("versions", "pending", "versions.unavailable",
                      report.get("reason") or "AWS was not reachable for a version inventory",
                      {"region": report.get("region")},
                      remediation="Configure AWS credentials for this box, then rerun.")
            return
        for warning in report.get("warnings") or []:
            self._add("versions", "warn", "versions.denied", warning.get("message"),
                      {"iam_action": warning.get("iam_action"), "aws_code": warning.get("aws_code")},
                      remediation=f"Grant {warning.get('iam_action')} to the selected identity.")
        findings = report.get("findings") or []
        if not findings:
            # Only claim "current" when the inventory was actually complete —
            # a denied API must never read as a clean bill of health.
            if not report.get("warnings"):
                self._add("versions", "pass", "versions.current",
                          "No managed service has a major version upgrade available",
                          {"region": report.get("region")})
            return
        for finding in findings:
            days = finding.get("days_remaining")
            near = days is not None
            self._add("versions", "warn" if near else "pending",
                      "versions.deadline" if near else "versions.upgrade_available",
                      (f"{finding.get('kind')} {finding.get('resource_id')} runs "
                       f"{finding.get('engine')} {finding.get('current_version')}; "
                       f"{finding.get('available_major')} is available"),
                      finding,
                      remediation=f"Plan the major version upgrade: {finding.get('release_notes_url')}")
