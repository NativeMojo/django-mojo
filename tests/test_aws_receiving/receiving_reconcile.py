"""Receiving reconciliation regressions; AWS boundaries run entirely in memory."""

from testit import helpers as th


DOMAIN = "receiving-regression.example.com"
BUCKET = "receiving-regression-inbound"
PREFIX = "mail/"
REGION = "us-east-1"
ACCOUNT = "123456789012"
TOPIC = "arn:aws:sns:us-east-1:123456789012:receiving-inbound"
RULE_SET = "mojo-default-receiving"
RULE_NAME = f"mojo-{DOMAIN}-catchall"


def _desired_rule():
    return {
        "Name": RULE_NAME, "Enabled": True, "TlsPolicy": "Optional",
        "Recipients": [DOMAIN], "ScanEnabled": True,
        "Actions": [{"S3Action": {
            "BucketName": BUCKET, "ObjectKeyPrefix": PREFIX, "TopicArn": TOPIC,
        }}],
    }


def _provider_error(operation, code="AccessDenied"):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": "test provider refusal"},
                        "ResponseMetadata": {"HTTPStatusCode": 403}}, operation)


class ReceivingSES:
    def __init__(self, rule=None, active=RULE_SET, fail=None, discard_writes=False):
        from copy import deepcopy
        self.rules = {rule["Name"]: deepcopy(rule)} if rule else {}
        self.sets = {RULE_SET}
        self.active = active
        self.fail = fail
        self.discard_writes = discard_writes
        self.writes = []

    def _write(self, operation, kwargs):
        from copy import deepcopy
        self.writes.append((operation, deepcopy(kwargs)))
        if operation == self.fail:
            raise _provider_error(operation)

    def list_receipt_rule_sets(self, **kwargs):
        return {"RuleSets": [{"Name": name} for name in sorted(self.sets)]}

    def describe_active_receipt_rule_set(self, **kwargs):
        from copy import deepcopy
        if not self.active:
            return {}
        return {"Metadata": {"Name": self.active},
                "Rules": deepcopy(list(self.rules.values()))}

    def describe_receipt_rule_set(self, RuleSetName):
        from copy import deepcopy
        if RuleSetName not in self.sets:
            raise _provider_error("DescribeReceiptRuleSet", "RuleSetDoesNotExist")
        return {"Metadata": {"Name": RuleSetName},
                "Rules": deepcopy(list(self.rules.values()))}

    def describe_receipt_rule(self, RuleSetName, RuleName):
        from copy import deepcopy
        if RuleName not in self.rules:
            raise _provider_error("DescribeReceiptRule", "RuleDoesNotExist")
        return {"Rule": deepcopy(self.rules[RuleName])}

    def create_receipt_rule_set(self, **kwargs):
        self._write("create_receipt_rule_set", kwargs)
        self.sets.add(kwargs["RuleSetName"])
        return {}

    def set_active_receipt_rule_set(self, **kwargs):
        self._write("set_active_receipt_rule_set", kwargs)
        self.active = kwargs["RuleSetName"]
        return {}

    def create_receipt_rule(self, **kwargs):
        from copy import deepcopy
        self._write("create_receipt_rule", kwargs)
        if not self.discard_writes:
            self.rules[kwargs["Rule"]["Name"]] = deepcopy(kwargs["Rule"])
        return {}

    def update_receipt_rule(self, **kwargs):
        from copy import deepcopy
        self._write("update_receipt_rule", kwargs)
        if not self.discard_writes:
            self.rules[kwargs["Rule"]["Name"]] = deepcopy(kwargs["Rule"])
        return {}


class ReceivingS3:
    def __init__(self):
        self.policy = {"Version": "2012-10-17", "Statement": [{
            "Sid": "UnrelatedRead", "Effect": "Allow",
            "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:root"},
            "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{BUCKET}/public/*",
        }]}
        self.encryption = {"Rules": [{"ApplyServerSideEncryptionByDefault": {
            "SSEAlgorithm": "AES256"}}]}
        self.writes = []

    def head_bucket(self, **kwargs):
        return {"BucketRegion": REGION, "ResponseMetadata": {
            "HTTPStatusCode": 200, "HTTPHeaders": {"x-amz-bucket-region": REGION}}}

    def get_bucket_location(self, **kwargs):
        return {"LocationConstraint": None}

    def get_bucket_policy(self, **kwargs):
        import json
        return {"Policy": json.dumps(self.policy)}

    def put_bucket_policy(self, **kwargs):
        import json
        self.writes.append(("put_bucket_policy", kwargs))
        self.policy = json.loads(kwargs["Policy"])
        return {}

    def get_bucket_encryption(self, **kwargs):
        from copy import deepcopy
        return {"ServerSideEncryptionConfiguration": deepcopy(self.encryption)}

    def put_bucket_encryption(self, **kwargs):
        from copy import deepcopy
        self.writes.append(("put_bucket_encryption", kwargs))
        self.encryption = deepcopy(kwargs["ServerSideEncryptionConfiguration"])
        return {}


class ReceivingAWS:
    def __init__(self, ses):
        self.ses = ses
        self.s3 = ReceivingS3()

    def client(self, service_name, **kwargs):
        if service_name == "ses":
            return self.ses
        if service_name == "s3":
            return self.s3
        if service_name == "sts":
            return self
        raise AssertionError(f"Unexpected AWS service requested: {service_name}")

    def get_caller_identity(self, **kwargs):
        return {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:role/test-receiving"}

    def __enter__(self):
        from contextlib import ExitStack
        from unittest import mock
        from mojo.helpers.aws import ses_domain
        self.stack = ExitStack()
        bucket = mock.Mock()
        bucket._check_exists.return_value = True
        bucket.exists = True
        bucket.client = self.s3
        bucket._client = self.s3
        bucket.create.return_value = True
        self.stack.enter_context(mock.patch.object(ses_domain, "S3Bucket", return_value=bucket))
        self.stack.enter_context(mock.patch.object(ses_domain, "get_client", side_effect=self.client))
        self.stack.enter_context(mock.patch.object(ses_domain, "get_session", return_value=self))
        self.stack.enter_context(mock.patch.object(ses_domain.boto3, "client", side_effect=self.client))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)


def _ensure():
    from mojo.helpers.aws import ses_domain
    return ses_domain.ensure_receiving_catch_all(
        domain=DOMAIN, s3_bucket=BUCKET, s3_prefix=PREFIX,
        inbound_topic_arn=TOPIC, region=REGION,
        access_key="test-access-key", secret_key="test-secret-key",
    )


def _expect_failure(message):
    try:
        _ensure()
    except (AssertionError, AttributeError, TypeError):
        raise
    except Exception as error:
        assert str(error), "Receiving failure must provide a diagnostic message"
        return error
    raise AssertionError(message)


@th.django_unit_test()
def test_receiving_s3_action_publishes_the_object_location(opts):
    ses = ReceivingSES()
    with ReceivingAWS(ses):
        result = _ensure()
    assert result == (RULE_SET, RULE_NAME), "Successful reconciliation must identify the managed rule"
    actions = ses.rules[RULE_NAME]["Actions"]
    assert actions == _desired_rule()["Actions"], \
        f"Inbound notifications must originate from S3Action with the object location: {actions}"


@th.django_unit_test()
def test_receiving_create_failure_reaches_caller(opts):
    ses = ReceivingSES(fail="create_receipt_rule")
    with ReceivingAWS(ses):
        _expect_failure("A rejected receipt-rule creation must not report successful reconciliation")
    assert not ses.rules, "Failed receipt-rule creation must leave no rule"


@th.django_unit_test()
def test_receiving_update_failure_reaches_caller(opts):
    rule = _desired_rule()
    rule["Enabled"] = False
    ses = ReceivingSES(rule=rule, fail="update_receipt_rule")
    with ReceivingAWS(ses):
        _expect_failure("A rejected receipt-rule update must not report successful reconciliation")
    assert ses.rules[RULE_NAME]["Enabled"] is False, "Failed update must preserve provider state"


@th.django_unit_test()
def test_receiving_foreign_active_set_requires_resolution(opts):
    ses = ReceivingSES(active="other-application-receiving")
    with ReceivingAWS(ses):
        _expect_failure("A rule in an inactive set must not report successful receiving setup")
    assert ses.active == "other-application-receiving", \
        "Reconciliation must preserve another application's active receipt rule set"


@th.django_unit_test()
def test_receiving_readback_mismatch_reaches_caller(opts):
    rule = _desired_rule()
    rule["Enabled"] = False
    ses = ReceivingSES(rule=rule, discard_writes=True)
    with ReceivingAWS(ses):
        _expect_failure("Provider acknowledgement without the desired saved rule must fail reconciliation")
    assert any(operation == "update_receipt_rule" for operation, args in ses.writes), \
        "Readback test must exercise an acknowledged receipt-rule update"


@th.django_unit_test()
def test_receiving_second_reconcile_is_a_noop(opts):
    ses = ReceivingSES()
    with ReceivingAWS(ses) as aws:
        first = _ensure()
        writes = list(ses.writes), list(aws.s3.writes)
        second = _ensure()
        assert second == first, "Repeat reconciliation must return the same managed rule identity"
        assert (ses.writes, aws.s3.writes) == writes, \
            "Already-converged receiving resources must not be written again"



class ReceivingKMS:
    def __init__(self, denied=False):
        self.arn = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/receiving-test-key"
        self.policy = {"Version": "2012-10-17", "Statement": [{
            "Sid": "AccountAdministration", "Effect": "Allow",
            "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:root"},
            "Action": "kms:*", "Resource": "*",
        }]}
        self.writes = []
        self.denied = denied

    def describe_key(self, **kwargs):
        return {"KeyMetadata": {"Arn": self.arn, "KeyManager": "CUSTOMER", "KeyState": "Enabled"}}

    def get_key_policy(self, **kwargs):
        import json
        return {"Policy": json.dumps(self.policy)}

    def put_key_policy(self, **kwargs):
        import json
        self.writes.append(kwargs)
        if self.denied:
            raise _provider_error("PutKeyPolicy")
        self.policy = json.loads(kwargs["Policy"])
        return {}


class ReceivingKMSAWS(ReceivingAWS):
    def __init__(self, ses, denied=False):
        super().__init__(ses)
        self.kms = ReceivingKMS(denied=denied)
        self.s3.encryption = {"Rules": [{"ApplyServerSideEncryptionByDefault": {
            "SSEAlgorithm": "aws:kms", "KMSMasterKeyID": self.kms.arn,
        }}]}

    def client(self, service_name, **kwargs):
        if service_name == "kms":
            return self.kms
        return super().client(service_name, **kwargs)


def _assert_receiving_scope(statement):
    source = f"arn:aws:ses:{REGION}:{ACCOUNT}:receipt-rule-set/{RULE_SET}:receipt-rule/{RULE_NAME}"
    equals = {key.lower(): value for key, value in statement["Condition"]["StringEquals"].items()}
    assert equals.get("aws:sourceaccount") == ACCOUNT, \
        "Receiving permission must be restricted to the receiving account"
    assert equals.get("aws:sourcearn") == source, \
        "Receiving permission must be restricted to the exact managed receipt rule"
    assert statement["Principal"] == {"Service": "ses.amazonaws.com"}, \
        "Receiving permission must grant the SES service principal"
    assert statement["Effect"] == "Allow", "Receiving permission must grant its scoped action"


@th.django_unit_test()
def test_receiving_preserves_policy_and_limits_puts_to_prefix(opts):
    from copy import deepcopy
    ses = ReceivingSES()
    with ReceivingAWS(ses) as aws:
        original = deepcopy(aws.s3.policy["Statement"])
        _ensure()
        statements = aws.s3.policy["Statement"]
    assert all(statement in statements for statement in original), \
        "Receiving setup must preserve unrelated bucket-policy statements"
    grants = [statement for statement in statements if statement not in original]
    assert len(grants) == 1, f"Receiving setup must add one scoped bucket grant, got {grants}"
    _assert_receiving_scope(grants[0])
    assert grants[0]["Resource"] == f"arn:aws:s3:::{BUCKET}/{PREFIX}*", \
        "SES PutObject must be limited to the configured inbound prefix"
    assert grants[0]["Action"] == "s3:PutObject", "Bucket grant must permit only object writes"
    assert all(operation != "put_bucket_encryption" for operation, args in aws.s3.writes), \
        "AES256 receiving storage must preserve existing bucket encryption"


@th.django_unit_test()
def test_receiving_malformed_policy_is_never_overwritten(opts):
    from copy import deepcopy
    for malformed in [{"Statement": "invalid"}, {"Statement": ["invalid"]}, ["invalid"]]:
        ses = ReceivingSES()
        with ReceivingAWS(ses) as aws:
            aws.s3.policy = deepcopy(malformed)
            _expect_failure("Malformed bucket policy must stop receiving setup before any replacement")
        assert aws.s3.policy == malformed, "Malformed bucket-policy content must remain untouched"
        assert not aws.s3.writes, "Malformed bucket policy must not cause storage writes"
        assert not ses.rules, "Receiving must not be reported configured with unreadable bucket policy"


@th.django_unit_test()
def test_receiving_customer_kms_key_grant_preserves_administration(opts):
    from copy import deepcopy
    ses = ReceivingSES()
    with ReceivingKMSAWS(ses) as aws:
        original = deepcopy(aws.kms.policy["Statement"])
        encryption = deepcopy(aws.s3.encryption)
        _ensure()
        statements = aws.kms.policy["Statement"]
        assert all(statement in statements for statement in original), \
            "Adding SES KMS permission must preserve key-administration access"
        grants = [statement for statement in statements if statement not in original]
        assert len(grants) == 1, "Receiving setup must add one scoped KMS permission"
        _assert_receiving_scope(grants[0])
        assert set(grants[0]["Action"]) == {"kms:GenerateDataKey", "kms:Decrypt"}, \
            "SES SSE-KMS delivery requires GenerateDataKey and Decrypt"
        assert grants[0]["Resource"] == "*", "KMS policy permission must apply to its owning key"
        assert aws.s3.encryption == encryption, "Receiving setup must preserve the customer's KMS configuration"
        assert "KmsKeyArn" not in ses.rules[RULE_NAME]["Actions"][0]["S3Action"], \
            "Bucket SSE-KMS must not enable SES client-side message encryption"
        writes = list(aws.kms.writes)
        _ensure()
        assert aws.kms.writes == writes, "Converged KMS permissions must not be rewritten"


@th.django_unit_test()
def test_receiving_kms_permission_denial_reaches_caller(opts):
    ses = ReceivingSES()
    with ReceivingKMSAWS(ses, denied=True) as aws:
        error = _expect_failure("Denied KMS permission repair must fail receiving reconciliation")
    assert len(aws.kms.writes) == 1, "KMS denial test must exercise a key-policy write"
    assert not ses.rules, "A denied KMS grant must not create an unusable receiving rule"
    from mojo.helpers.aws.provider_call import safe_error_detail
    evidence = safe_error_detail(error)
    assert evidence["provider_code"] == "AccessDenied", "KMS denial must retain its safe provider code"
    assert evidence["operation"] == "kms.put_key_policy", "KMS denial must identify the failed operation"
    assert evidence["iam_action"] == "kms:PutKeyPolicy", "KMS denial must identify the required IAM action"


@th.django_unit_test()
def test_receiving_missing_inbound_topic_fails_before_provider_writes(opts):
    from mojo.helpers.aws import ses_domain
    ses = ReceivingSES()
    with ReceivingAWS(ses) as aws:
        try:
            ses_domain.ensure_receiving_catch_all(
                DOMAIN, BUCKET, PREFIX, None, REGION, "test-access-key", "test-secret-key")
        except ValueError as error:
            assert "topic" in str(error).lower(), "Missing inbound topic must have a clear diagnostic"
        else:
            raise AssertionError("Receiving setup without an inbound topic must fail")
    assert not ses.writes and not aws.s3.writes, "Missing topic must be rejected before AWS writes"


@th.django_unit_test()
def test_receiving_role_credentials_reach_provider_factory(opts):
    from types import SimpleNamespace
    from unittest import mock
    from mojo.helpers.aws import ses_domain

    class RoleAWS(ReceivingAWS):
        def __init__(self, ses):
            super().__init__(ses)
            self.requests = []

        def client(self, service_name, **kwargs):
            self.requests.append((service_name, kwargs))
            return super().client(service_name, **kwargs)

    with RoleAWS(ReceivingSES()) as aws:
        with mock.patch.object(ses_domain, "settings", SimpleNamespace(AWS_KEY=None, AWS_SECRET=None)):
            result = ses_domain.ensure_receiving_catch_all(
                DOMAIN, BUCKET, PREFIX, TOPIC, REGION, None, None)
    assert result == (RULE_SET, RULE_NAME), "Receiving setup must support IAM-role credentials"
    assert {service for service, kwargs in aws.requests} >= {"ses", "s3", "sts"}, \
        "IAM-role test must exercise SES, S3 and account-identity provider clients"
    assert all(not kwargs.get("access_key") and not kwargs.get("secret_key")
               for service, kwargs in aws.requests), \
        "Provider clients must retain the default credential chain when explicit credentials are absent"


class ReceivingAuditAWS(ReceivingAWS):
    def client(self, service_name, **kwargs):
        if service_name in ("sns", "sesv2"):
            return self
        return super().client(service_name, **kwargs)

    def get_account(self, **kwargs):
        return {"ProductionAccessEnabled": True}

    def get_topic_attributes(self, **kwargs):
        return {"Attributes": {"TopicArn": kwargs["TopicArn"]}}

    def list_subscriptions_by_topic(self, **kwargs):
        return {"Subscriptions": [{"Protocol": "https", "PendingConfirmation": "false",
                                   "SubscriptionArn": TOPIC + ":test-subscription"}]}


class ReceivingAuditSES(ReceivingSES):
    def get_identity_verification_attributes(self, **kwargs):
        return {"VerificationAttributes": {DOMAIN: {"VerificationStatus": "Success"}}}

    def get_identity_dkim_attributes(self, **kwargs):
        return {"DkimAttributes": {DOMAIN: {"DkimEnabled": True, "DkimVerificationStatus": "Success"}}}

    def get_identity_notification_attributes(self, **kwargs):
        return {"NotificationAttributes": {DOMAIN: {
            "BounceTopic": TOPIC, "ComplaintTopic": TOPIC, "DeliveryTopic": TOPIC}}}


def _audit_receiving(aws):
    from mojo.helpers.aws import ses_domain
    return ses_domain.audit_domain_config(
        DOMAIN, region=REGION, access_key="test-access-key", secret_key="test-secret-key",
        desired_receiving={"bucket": BUCKET, "prefix": PREFIX, "rule_set": RULE_SET,
                           "rule_name": RULE_NAME, "inbound_topic_arn": TOPIC},
        desired_topics={"bounce": TOPIC, "complaint": TOPIC, "delivery": TOPIC},
        client_factory=aws.client,
    )


@th.django_unit_test()
def test_receiving_audit_accepts_s3_notification_and_rejects_disabled_rule(opts):
    for enabled in (True, False):
        rule = _desired_rule()
        rule["Enabled"] = enabled
        report = _audit_receiving(ReceivingAuditAWS(ReceivingAuditSES(rule=rule)))
        assert report.checks["receiving_rule_sns_ok"] is True, \
            "Receiving audit must recognize the SNS topic carried by S3Action"
        if enabled:
            assert report.checks["receiving_rule_s3_ok"] is True, \
                "Active enabled S3 delivery must pass receiving audit"
            assert report.audit_pass is True, f"Fully configured receiving must pass audit: {report.items}"
        else:
            assert report.audit_pass is False, "Disabled receipt rules must fail audit"
            assert any(item.status != "ok" and "receipt" in item.resource for item in report.items), \
                "Disabled receiving must produce a detailed receipt-rule audit finding"


@th.django_unit_test()
def test_receiving_audit_rejects_foreign_active_set(opts):
    report = _audit_receiving(ReceivingAuditAWS(
        ReceivingAuditSES(rule=_desired_rule(), active="other-application-receiving")))
    assert report.audit_pass is False, "A matching rule inside an inactive rule set must fail audit"
    assert any(item.status != "ok" and "receipt" in item.resource for item in report.items), \
        "Inactive receiving must produce a detailed receipt-rule audit finding"


@th.django_unit_test()
def test_receiving_s3_notification_persists_real_incoming_email(opts):
    import io
    from types import SimpleNamespace
    from unittest import mock
    from mojo.apps.aws.models import IncomingEmail
    from mojo.apps.aws.rest import sns
    from mojo.helpers.aws import inbound_email

    key = PREFIX + "receiving-regression-message"
    url = f"s3://{BUCKET}/{key}"
    recipient = f"unmatched-envelope-recipient@{DOMAIN}"
    raw = (f"From: Sender <sender@example.org>\r\n"
           f"To: header-recipient@{DOMAIN}\r\n"
           "Message-ID: <receiving-regression-message@example.org>\r\n"
           "Subject: Receiving integration regression\r\n"
           "Content-Type: text/plain; charset=utf-8\r\n\r\n"
           "The real inbound parser stored this body.\r\n").encode()
    client = mock.Mock()
    client.get_object.return_value = {"Body": io.BytesIO(raw)}
    IncomingEmail.objects.filter(s3_object_url=url).delete()
    try:
        with mock.patch.object(inbound_email, "S3", SimpleNamespace(client=client)):
            sns._handle_inbound_notification({
                "notificationType": "Received",
                "mail": {"messageId": "receiving-regression-message", "destination": [recipient]},
                "receipt": {"recipients": [recipient], "action": {
                    "type": "S3", "bucketName": BUCKET, "objectKey": key}},
            })
        rows = list(IncomingEmail.objects.filter(s3_object_url=url))
        assert len(rows) == 1, "S3Action notification must persist one real IncomingEmail row"
        row = rows[0]
        assert row.subject == "Receiving integration regression", "Inbound row must contain parsed MIME subject"
        assert row.text_body.strip() == "The real inbound parser stored this body.", \
            "Inbound row must contain the downloaded and parsed MIME body"
        assert recipient in row.to_addresses, "Inbound ingestion must preserve authoritative envelope recipients"
        assert row.size_bytes == len(raw), "Inbound row must record the downloaded MIME size"
        client.get_object.assert_called_once_with(Bucket=BUCKET, Key=key)
    finally:
        IncomingEmail.objects.filter(s3_object_url=url).delete()


@th.django_unit_test()
def test_receiving_preserves_unsupported_existing_s3_actions(opts):
    from copy import deepcopy
    for field in ("KmsKeyArn", "IamRoleArn"):
        rule = _desired_rule()
        rule["Actions"][0]["S3Action"][field] = "existing-resource-arn"
        ses = ReceivingSES(rule=rule)
        with ReceivingAWS(ses) as aws:
            _expect_failure("Unsupported existing encryption/role must require an explicit operator decision")
            assert not aws.s3.writes, "Conflict must precede storage policy changes"
        assert ses.rules[RULE_NAME] == rule, "Existing receiving encryption and role must be preserved"
        assert not ses.writes, "Unsupported existing rule must not be replaced"


@th.django_unit_test()
def test_receiving_audit_rejects_standalone_sns_and_message_encryption(opts):
    for variant in ("standalone", "encrypted"):
        rule = _desired_rule()
        action = rule["Actions"][0]["S3Action"]
        if variant == "standalone":
            action.pop("TopicArn")
            rule["Actions"].append({"SNSAction": {"TopicArn": TOPIC, "Encoding": "UTF-8"}})
        else:
            action["KmsKeyArn"] = "existing-encryption-key"
        report = _audit_receiving(ReceivingAuditAWS(ReceivingAuditSES(rule=rule)))
        assert not report.audit_pass, "Audit must reject rules the inbox cannot ingest"


@th.django_unit_test()
def test_receiving_rule_set_create_race_and_denial(opts):
    class RacingSES(ReceivingSES):
        def create_receipt_rule_set(self, **kwargs):
            self.sets.add(kwargs["RuleSetName"])
            raise _provider_error("CreateReceiptRuleSet", "AlreadyExists")

    ses = RacingSES(active=None)
    ses.sets.clear()
    with ReceivingAWS(ses):
        assert _ensure() == (RULE_SET, RULE_NAME), "Confirmed already-exists race must converge"
    assert ses.active == RULE_SET, "New receiving configuration must activate its rule set"
    ses = ReceivingSES(active=None, fail="create_receipt_rule_set")
    ses.sets.clear()
    with ReceivingAWS(ses):
        error = _expect_failure("Rule-set creation denial must reach the caller")
    assert getattr(error, "provider_code", None) == "AccessDenied", "Denied create must preserve safe provider evidence"


@th.django_unit_test()
def test_receiving_bucket_owns_raw_provider_error_boundary(opts):
    from unittest import mock
    from mojo.helpers.aws import ses_domain
    from botocore.exceptions import ClientError

    with ReceivingAWS(ReceivingSES()) as aws:
        def bucket_factory(name, client_factory, region):
            client = client_factory("s3", access_key="wrong-global-key", secret_key="wrong-global-secret")
            assert client is aws.s3, "S3Bucket must receive raw clients for its existing error/region handling"
            bucket = mock.Mock()
            bucket._check_exists.return_value = False
            bucket.create.return_value = True
            return bucket
        with mock.patch.object(ses_domain, "S3Bucket", side_effect=bucket_factory) as construct:
            _ensure()
        assert construct.call_count == 1, "Missing-bucket creation must use the existing bucket helper"


@th.django_unit_test()
def test_receiving_rest_preserves_safe_configuration_and_dns_failures(opts):
    import inspect
    import json
    from types import SimpleNamespace
    from unittest import mock
    from mojo.apps.aws.rest import email_ops
    from mojo.helpers.aws.ses_domain import ReceivingConfigurationError
    from mojo.errors import ValueException

    request = SimpleNamespace(method="POST", DATA={})
    handler = inspect.unwrap(email_ops.on_email_domain_reconcile)
    for error, status in ((ReceivingConfigurationError("Resolve active receiving rule set"), 400),
                          (ValueException("Register the managed domain", code=404, status=404), 404)):
        with mock.patch.object(email_ops, "reconcile_email_domain", side_effect=error):
            response = handler(request, 1)
        assert response.status_code == status, "Admin must preserve the actionable configuration status"
        assert "error" in json.loads(response.content), "Admin must return the safe remediation message"


@th.django_unit_test()
def test_receiving_inbound_subscription_denial_and_pending_are_not_success(opts):
    from types import SimpleNamespace
    from unittest import mock
    from mojo.helpers.aws import ses_domain
    from mojo.helpers.aws.sns import SNSSubscription
    from mojo.helpers.aws.provider_call import ProviderCallError

    for outcome in ("denied", "pending", "confirmed"):
        client = mock.Mock()
        if outcome == "denied":
            client.subscribe.side_effect = _provider_error("Subscribe")
        else:
            client.subscribe.return_value = {
                "SubscriptionArn": TOPIC + ":subscription" if outcome == "confirmed" else "pending confirmation"}
        subscription = SNSSubscription.__new__(SNSSubscription)
        subscription.client = client
        subscription.topic_arn = TOPIC
        topic = SimpleNamespace(exists=True, arn=TOPIC, client=client)
        with mock.patch.object(ses_domain, "SNSTopic", return_value=topic), \
                mock.patch.object(ses_domain, "SNSSubscription", return_value=subscription):
            try:
                arns = ses_domain.ensure_sns_topics_and_subscriptions(
                    DOMAIN, ses_domain.SnsEndpoints(inbound="https://example.com/inbound"),
                    REGION, None, None)
            except ProviderCallError as error:
                assert outcome == "denied", "Only denied subscription should raise provider error"
                assert error.detail()["iam_action"] == "sns:Subscribe", "Subscription denial must name the required IAM action"
            except ses_domain.ReceivingConfigurationError as error:
                assert outcome == "pending", "Only pending confirmation should require webhook confirmation"
                assert "confirm" in str(error), "Pending subscription must report the confirmation requirement"
            else:
                assert outcome == "confirmed" and arns["inbound"] == TOPIC, "Only confirmed inbound subscription may report success"
