"""Bounded, injectable AWS provider-call boundary.

Callers supply the IAM action because botocore operation names are not a
reliable IAM-action mapping.  Raw provider messages are deliberately never
stored, returned, or logged: they can contain credentials, signed URLs, and
request parameters.
"""

import re

from botocore.exceptions import (
    BotoCoreError, ClientError, ConnectTimeoutError, EndpointConnectionError,
    NoCredentialsError, PartialCredentialsError, ReadTimeoutError,
)

from mojo.helpers import logit


logger = logit.get_logger("aws_provider", "aws.log")

_SAFE_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9+=:/_.-]{8,128}$")
_SAFE_ACTION = re.compile(r"^[a-z0-9-]+:[A-Za-z0-9*_-]+$")
_DENIED = {
    "403", "AccessDenied", "AccessDeniedException", "AuthorizationError",
    "NotAuthorized", "NotAuthorizedException", "UnauthorizedOperation",
}
_RETRYABLE = {"RequestLimitExceeded", "RequestTimeout", "ServiceUnavailable", "SlowDown",
              "Throttling", "ThrottlingException", "TooManyRequestsException"}
_MUTATION_PREFIXES = (
    "create_", "delete_", "put_", "set_", "subscribe", "tag_", "untag_",
    "update_", "verify_",
)


def _safe_code(value):
    value = str(value or "provider_error")[:64]
    if value in ("400", "401", "403", "404", "409", "429", "500", "502", "503", "504"):
        return value
    return value if _SAFE_CODE.fullmatch(value) else "provider_error"


def _safe_request_id(value):
    value = str(value or "")[:128]
    return value if _SAFE_REQUEST_ID.fullmatch(value) else ""


def _safe_action(value):
    value = str(value or "")[:128]
    return value if _SAFE_ACTION.fullmatch(value) else ""


class ProviderCallError(Exception):
    """Wire-safe provider failure with no reference to the raw exception."""

    def __init__(self, operation, provider_code="provider_error", iam_action="",
                 request_id="", retryable=False, mutation_state="none", denied=False):
        self.operation = str(operation or "aws")[:80]
        self.provider_code = _safe_code(provider_code)
        self.iam_action = _safe_action(iam_action)
        self.request_id = _safe_request_id(request_id)
        self.retryable = bool(retryable)
        self.denied = bool(denied or self.provider_code in _DENIED)
        self.mutation_state = mutation_state if mutation_state in (
            "none", "attempted", "unknown") else "unknown"
        super().__init__("AWS provider operation failed")

    def __str__(self):
        return "AWS provider operation failed"

    def detail(self):
        detail = {
            "operation": self.operation,
            "provider_code": self.provider_code,
            "retryable": self.retryable,
            "mutation_state": self.mutation_state,
        }
        if self.request_id:
            detail["request_id"] = self.request_id
        if self.denied and self.iam_action:
            detail["iam_action"] = self.iam_action
        return detail


def map_error(exc, operation, iam_action="", mutation=False):
    if isinstance(exc, ProviderCallError):
        return exc
    code = "provider_error"
    request_id = ""
    retryable = False
    denied = False
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        code = "credentials_unavailable"
    elif isinstance(exc, (ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError)):
        code = "network_unavailable"
        retryable = True
    elif isinstance(exc, ClientError):
        response = exc.response if isinstance(exc.response, dict) else {}
        error = response.get("Error") if isinstance(response.get("Error"), dict) else {}
        metadata = response.get("ResponseMetadata") \
            if isinstance(response.get("ResponseMetadata"), dict) else {}
        code = error.get("Code") or metadata.get("HTTPStatusCode") or "provider_error"
        denied = metadata.get("HTTPStatusCode") == 403 or str(code) in _DENIED
        request_id = metadata.get("RequestId") or metadata.get("RequestID") or ""
        retryable = str(code) in _RETRYABLE or metadata.get("HTTPStatusCode") in (
            429, 500, 502, 503, 504)
    elif isinstance(exc, BotoCoreError):
        code = "provider_error"
    state = "unknown" if mutation and retryable else "attempted" if mutation else "none"
    return ProviderCallError(
        operation, code, iam_action, request_id, retryable, state, denied)


def safe_error_detail(exc, operation="aws", iam_action=""):
    """Return the only provider-exception shape suitable for API/DB/log output."""
    return map_error(exc, operation, iam_action).detail()


class ProviderCaller:
    """Small injectable wrapper used by setup and provider adapters."""

    def __init__(self, logger_instance=None):
        self.logger = logger_instance or logger

    def call(self, operation, callback, iam_action="", mutation=False):
        try:
            return callback()
        except Exception as exc:
            error = map_error(exc, operation, iam_action, mutation)
            self.logger.warning(
                "AWS provider operation failed operation=%s provider_code=%s "
                "iam_action=%s request_id=%s mutation_state=%s",
                error.operation, error.provider_code, error.iam_action or "-",
                error.request_id or "-", error.mutation_state,
            )
            # Do not retain the raw botocore exception as a chained cause.
            # Tracebacks and later ``logger.exception`` calls must remain safe.
            raise error from None


class ProviderClient:
    """Inject the safe call boundary around an existing boto-style client."""

    def __init__(self, client, service, caller=None, action_service=None):
        self.client = client
        self.service = str(service)
        self.action_service = str(action_service or service).removesuffix("v2")
        self.caller = caller or provider_caller

    def __getattr__(self, name):
        value = getattr(self.client, name)
        if not callable(value):
            return value
        action = "".join(part[:1].upper() + part[1:] for part in name.split("_"))
        mutation = name.startswith(_MUTATION_PREFIXES)

        def bounded(*args, **kwargs):
            return self.caller.call(
                f"{self.service}.{name}", lambda: value(*args, **kwargs),
                f"{self.action_service}:{action}", mutation=mutation)

        return bounded


provider_caller = ProviderCaller()
