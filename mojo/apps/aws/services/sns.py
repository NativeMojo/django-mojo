import base64
import json
import re
import time
from urllib.parse import parse_qs, urlparse

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from mojo.helpers import logit
from mojo.helpers.settings import settings


logger = logit.get_logger(__name__, "aws.log")

MAX_ENVELOPE_BYTES = 300 * 1024
CERT_TTL_SECONDS = settings.get_static("SNS_CERT_TTL_SECONDS", 3600, kind="int")
_CERT_CACHE = {}
_CERT_PATH_RE = re.compile(r"^/SimpleNotificationService-[A-Za-z0-9_-]+\.pem$")
_SNS_HOST_RE = re.compile(
    r"^sns(?:\.[a-z0-9-]+)?\.amazonaws\.com(?:\.cn)?$"
)


class SNSPayloadError(ValueError):
    pass


class SNSAuthorizationError(ValueError):
    pass


class SNSTransientError(RuntimeError):
    pass


def parse_request(request):
    body = getattr(request, "body", b"")
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not isinstance(body, bytes) or not body or len(body) > MAX_ENVELOPE_BYTES:
        raise SNSPayloadError("invalid SNS envelope")
    try:
        envelope = json.loads(body.decode("utf-8"))
    except Exception:
        raise SNSPayloadError("invalid SNS envelope")
    if not isinstance(envelope, dict):
        raise SNSPayloadError("invalid SNS envelope")
    return envelope


def _sns_url(value, certificate=False):
    try:
        parsed = urlparse(value)
        port = parsed.port
    except Exception:
        raise SNSAuthorizationError("invalid SNS URL")
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or not _SNS_HOST_RE.match(host)
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise SNSAuthorizationError("invalid SNS URL")
    if certificate:
        if parsed.query or not _CERT_PATH_RE.match(parsed.path or ""):
            raise SNSAuthorizationError("invalid SNS certificate URL")
    return parsed


def _required(envelope, names):
    for name in names:
        value = envelope.get(name)
        if not isinstance(value, str) or not value:
            raise SNSAuthorizationError("invalid SNS envelope")


def _canonical(envelope):
    msg_type = envelope.get("Type")
    if msg_type == "Notification":
        names = ["Message", "MessageId", "Timestamp", "TopicArn", "Type"]
        _required(envelope, names + ["Signature", "SignatureVersion", "SigningCertURL"])
        ordered = ["Message", "MessageId"]
        if envelope.get("Subject") is not None:
            if not isinstance(envelope.get("Subject"), str):
                raise SNSAuthorizationError("invalid SNS envelope")
            ordered.append("Subject")
        ordered.extend(["Timestamp", "TopicArn", "Type"])
    elif msg_type in ("SubscriptionConfirmation", "UnsubscribeConfirmation"):
        ordered = [
            "Message", "MessageId", "SubscribeURL", "Timestamp", "Token",
            "TopicArn", "Type",
        ]
        _required(
            envelope,
            ordered + ["Signature", "SignatureVersion", "SigningCertURL"],
        )
    else:
        raise SNSAuthorizationError("invalid SNS message type")
    return "".join(f"{name}\n{envelope[name]}\n" for name in ordered).encode("utf-8")


def _certificate(url):
    _sns_url(url, certificate=True)
    now = time.time()
    cached = _CERT_CACHE.get(url)
    if cached and now - cached[0] < CERT_TTL_SECONDS:
        return cached[1]
    try:
        response = requests.get(url, timeout=10, allow_redirects=False)
        if response.status_code != 200:
            raise SNSTransientError("SNS certificate fetch failed")
        pem = response.content
        cert = x509.load_pem_x509_certificate(pem)
    except SNSAuthorizationError:
        raise
    except SNSTransientError:
        raise
    except Exception as exc:
        logger.warning("SNS certificate fetch failed: %s", exc)
        raise SNSTransientError("SNS certificate fetch failed")
    _CERT_CACHE[url] = (now, cert)
    return cert


def _headers_match(request, envelope):
    if request is None:
        return True
    expected = {
        "HTTP_X_AMZ_SNS_MESSAGE_TYPE": "Type",
        "HTTP_X_AMZ_SNS_MESSAGE_ID": "MessageId",
        "HTTP_X_AMZ_SNS_TOPIC_ARN": "TopicArn",
    }
    for header, field in expected.items():
        value = request.META.get(header)
        if value is not None and value != envelope.get(field):
            return False
    return True


def verify_envelope(envelope, allowed_topics, request=None, allow_debug=False):
    topic_arn = envelope.get("TopicArn")
    if not isinstance(allowed_topics, (list, tuple, set)) or topic_arn not in allowed_topics:
        raise SNSAuthorizationError("SNS authorization failed")
    if not _headers_match(request, envelope):
        raise SNSAuthorizationError("SNS authorization failed")
    if (
        allow_debug
        and getattr(settings, "DEBUG", False)
        and settings.get_static("SNS_VALIDATION_BYPASS_DEBUG", False, kind="bool")
    ):
        return envelope

    canonical = _canonical(envelope)
    version = envelope.get("SignatureVersion")
    if version == "1":
        hash_algorithm = hashes.SHA1()
    elif version == "2":
        hash_algorithm = hashes.SHA256()
    else:
        raise SNSAuthorizationError("SNS authorization failed")
    try:
        signature = base64.b64decode(envelope["Signature"], validate=True)
        cert = _certificate(envelope["SigningCertURL"])
        cert.public_key().verify(
            signature, canonical, padding.PKCS1v15(), hash_algorithm,
        )
    except SNSTransientError:
        raise
    except SNSAuthorizationError:
        raise
    except Exception:
        raise SNSAuthorizationError("SNS authorization failed")
    return envelope


def confirm_subscription(envelope):
    parsed = _sns_url(envelope.get("SubscribeURL", ""))
    if parsed.path not in ("", "/"):
        raise SNSAuthorizationError("invalid SNS confirmation URL")
    query = parse_qs(parsed.query, keep_blank_values=True)
    expected = {
        "Action": "ConfirmSubscription",
        "TopicArn": envelope.get("TopicArn"),
        "Token": envelope.get("Token"),
    }
    for key, value in expected.items():
        if query.get(key) != [value]:
            raise SNSAuthorizationError("invalid SNS confirmation URL")
    if set(query) != set(expected):
        raise SNSAuthorizationError("invalid SNS confirmation URL")
    try:
        response = requests.get(
            envelope["SubscribeURL"], timeout=10, allow_redirects=False,
        )
    except Exception as exc:
        logger.warning("SNS confirmation failed: %s", exc)
        raise SNSTransientError("SNS confirmation failed")
    if response.status_code < 200 or response.status_code >= 300:
        raise SNSTransientError("SNS confirmation failed")
    return response.status_code
