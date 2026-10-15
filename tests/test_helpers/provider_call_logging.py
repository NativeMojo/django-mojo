"""The AWS provider-call boundary logs a scrubbed AWS message (maestro #8346).

The message reaches the operator log only: never `detail()`, which feeds
API responses and stored reports, and never a chained exception.
"""

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "core"

REQUEST_ID = "c4ecc981-3ed2-4b26-9a1b-a0f68f2e982f"
SECRETS = (
    "SIG8346SECRET",
    "AKIAZZZZ8346SECRETS",
    "FwoGZXIvYXdzEBMaDTOKEN8346",
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC8346",
    "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH8346",
)
MESSAGE = (
    "The parameter TrustedKeyGroups is invalid. "
    "https://bucket.s3.amazonaws.com/k?X-Amz-Signature=SIG8346SECRET&X-Amz-Credential=AKIAZZZZ8346SECRETS "
    "X-Amz-Security-Token=FwoGZXIvYXdzEBMaDTOKEN8346 "
    "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC8346\n-----END PRIVATE KEY----- "
    "blob abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH8346"
)


def _client_error(message=MESSAGE, code="InvalidArgument"):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": message},
                        "ResponseMetadata": {"HTTPStatusCode": 400, "RequestId": REQUEST_ID}},
                       "UpdateDistribution")


class _Logger:
    def __init__(self):
        self.lines = []

    def warning(self, *args):
        self.lines.append("\n".join(str(arg) for arg in args))


def _call(exc, mutation=True):
    from mojo.helpers.aws.provider_call import ProviderCallError, ProviderCaller
    logger = _Logger()

    def boom():
        raise exc

    try:
        ProviderCaller(logger).call("cloudfront.update_distribution", boom,
                                    iam_action="cloudfront:UpdateDistribution", mutation=mutation)
    except ProviderCallError as error:
        return error, logger.lines
    raise AssertionError("the call must raise ProviderCallError")


@th.unit_test("provider call: AWS's message is logged, scrubbed, beside code and request id")
def test_message_logged_scrubbed(opts):
    error, lines = _call(_client_error())
    assert_eq(len(lines), 1, f"one log line: {lines}")
    line = lines[0]
    for part in ("operation=cloudfront.update_distribution", "provider_code=InvalidArgument",
                 f"request_id={REQUEST_ID}", "The parameter TrustedKeyGroups is invalid",
                 "https://bucket.s3.amazonaws.com/k?[redacted]"):
        assert_true(part in line, f"the line carries {part!r}: {line}")
    for secret in SECRETS:
        assert_true(secret not in line, f"{secret!r} is scrubbed from the log: {line}")
        assert_true(secret not in error.provider_message, f"{secret!r} is scrubbed from the field")


@th.unit_test("provider call: credentials outside a URL are scrubbed too")
def test_bare_credentials_scrubbed(opts):
    message = ("Signature=SIG8346SECRET Key-Pair-Id: K2 AWSAccessKeyId=AKIAZZZZ8346SECRETS "
               "SessionToken=FwoGZXIvYXdzEBMaDTOKEN8346 ASIAZZZZ8346SECRETS")
    error, lines = _call(_client_error(message))
    for secret in ("SIG8346SECRET", "AKIAZZZZ8346SECRETS", "FwoGZXIvYXdzEBMaDTOKEN8346", "ASIAZZZZ8346SECRETS"):
        assert_true(secret not in lines[0], f"{secret!r} is scrubbed: {lines[0]}")
    assert_true("Key-Pair-Id: [redacted]" in error.provider_message, error.provider_message)


@th.unit_test("provider call: Route53 hosted-zone and change ids are scrubbed, CloudFront ids kept")
def test_route53_ids_scrubbed(opts):
    message = ("No hosted zone found with ID: Z0123456789ABCDEFGHIJ; change C2682N5HXP0BZ4 pending; "
               "distribution E1ABCDEFGHIJKL key group 83050000-0000-0000-0000-000000000101")
    error, lines = _call(_client_error(message, code="NoSuchHostedZone"))
    for value in ("Z0123456789ABCDEFGHIJ", "C2682N5HXP0BZ4"):
        assert_true(value not in lines[0], f"{value!r} is scrubbed: {lines[0]}")
    for value in ("E1ABCDEFGHIJKL", "83050000-0000-0000-0000-000000000101"):
        assert_true(value in error.provider_message, f"{value!r} stays readable: {error.provider_message}")


@th.unit_test("provider call: the message never reaches detail() or safe_error_detail()")
def test_message_not_in_detail(opts):
    import json
    from mojo.helpers.aws.provider_call import map_error, safe_error_detail
    error = map_error(_client_error(), "cloudfront.update_distribution")
    assert_true(error.provider_message.startswith("The parameter TrustedKeyGroups is invalid"),
                error.provider_message)
    for detail in (error.detail(), safe_error_detail(_client_error(), "cloudfront.update_distribution")):
        assert_true("provider_message" not in detail, f"no message key: {detail}")
        assert_true("TrustedKeyGroups" not in json.dumps(detail), f"no message text: {detail}")
    assert_eq(str(error), "AWS provider operation failed", "the exception text stays generic")


@th.unit_test("provider call: a long message is cut to 256 characters on one line")
def test_message_truncated(opts):
    from mojo.helpers.aws.provider_call import map_error
    message = "Invalid request:\n" + " ".join(f"word{i}" for i in range(200))
    error = map_error(_client_error(message), "cloudfront.update_distribution")
    assert_true(len(error.provider_message) <= 256, f"{len(error.provider_message)} characters")
    assert_true(error.provider_message.endswith("..."), "a cut message says so")
    assert_true("\n" not in error.provider_message, "one log line")


@th.unit_test("provider call: no message is '-', and the raw exception is never chained")
def test_no_message_and_no_chain(opts):
    from botocore.exceptions import NoCredentialsError
    error, lines = _call(NoCredentialsError(), mutation=False)
    assert_eq(error.provider_message, "", "a credentials failure carries no message")
    assert_true(lines[0].endswith("provider_message='-'"), lines[0])
    error, _ = _call(_client_error())
    assert_true(error.__cause__ is None and error.__suppress_context__, "raised from None")


class _Sink:
    """The stdlib-logger end of a real logit.Logger, built here: records
    what the wrapper renders, writes nothing."""

    def __init__(self):
        self.lines = []

    def warning(self, message):
        self.lines.append(message)


def _logged_through_logit(message):
    from mojo.helpers import logit
    from mojo.helpers.aws.provider_call import ProviderCallError, ProviderCaller
    sink = _Sink()

    def boom():
        raise _client_error(message)

    try:
        ProviderCaller(logit.Logger("aws_provider_8346", None, sink)).call(
            "cloudfront.update_distribution", boom, iam_action="cloudfront:UpdateDistribution",
            mutation=True)
    except ProviderCallError as error:
        assert_eq(len(sink.lines), 1, f"one rendered log line: {sink.lines}")
        return error, sink.lines[0]
    raise AssertionError("the call must raise ProviderCallError")


@th.unit_test("provider call: quoted, JSON and percent-encoded credentials are scrubbed through logit")
def test_quoted_and_encoded_credentials_scrubbed(opts):
    import urllib.parse
    encoded = urllib.parse.quote("synthetic+encoded/token/8346" * 3, safe="")
    cases = {
        "single-quoted encoded token": ("Invalid X-Amz-Security-Token='" + encoded + "'", encoded),
        "double-quoted token with a space": ('Invalid X-Amz-Security-Token="QTOK8346 SECRETPART"',
                                             "SECRETPART"),
        "JSON signature": ('{"Signature": "SIGJSON8346SECRET", "Key-Pair-Id": "K2"}',
                           "SIGJSON8346SECRET"),
        "quoted signature": ("Signature='SIGQ8346SECRET' rejected", "SIGQ8346SECRET"),
        "URL userinfo": ("fetch https://alice:PASS8346WORD@origin.example.com/p failed",
                         "PASS8346WORD"),
        "bare encoded blob": ("blob " + encoded, encoded),
        "encoded fragment in a bare token": ("SessionToken=" + encoded[:30] + " rest", encoded[:30]),
    }
    for name, (message, secret) in cases.items():
        error, line = _logged_through_logit(message)
        assert_true(secret not in line, f"{name}: {secret!r} is scrubbed from the log: {line}")
        assert_true(secret not in error.provider_message,
                    f"{name}: {secret!r} is scrubbed from the field: {error.provider_message}")
    error, line = _logged_through_logit(
        "The parameter TrustedKeyGroups is invalid for X-Amz-Security-Token='" + encoded + "'")
    assert_true("The parameter TrustedKeyGroups is invalid" in line,
                f"the diagnostic text stays readable: {line}")
    assert_true("X-Amz-Security-Token=[redacted]" in line, f"the field name stays, its value goes: {line}")

