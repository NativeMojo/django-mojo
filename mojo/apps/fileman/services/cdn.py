"""
CDN delivery for S3 file managers (maestro #8250): link building and signing.

A `CdnDistribution` row describes CloudFront in front of ONE bucket; every S3
file manager on that bucket delivers through it unless the manager (or its
top parent) sets `cdn = "off"`. Public managers hand out unsigned CDN URLs
that never expire; private managers hand out CloudFront canned-policy signed
URLs whose lifetime is the manager's `cdn_url_expires_in` (default 7 days)
capped by the platform's `FILEMAN_CDN_MAX_EXPIRES_IN` (default 90 days).

Provisioning (creating the distribution and its policies) lives beside this
in `cdn_provision.py`; this module is what the request path touches, so it
does no AWS calls at all — signing is local, and the signer for a key is
cached per process so a private link costs one RSA signature, not a KMS
round trip.
"""

import math
import re
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

from mojo.helpers import logit
from mojo.helpers.settings import settings


MAX_EXPIRES_IN_KEY = "FILEMAN_CDN_MAX_EXPIRES_IN"
DEFAULT_EXPIRES_IN = 7 * 24 * 3600          # 7 days
DEFAULT_MAX_EXPIRES_IN = 90 * 24 * 3600     # 90 days
MIN_EXPIRES_IN = 60
ABSOLUTE_MAX_EXPIRES_IN = 366 * 24 * 3600   # the validator's ceiling for the cap

# The response headers policy on every cache behavior of a distribution
# fileman provisions (maestro #8286): AWS's managed SimpleCORS unless the
# deployment names another policy id, or "none" for no policy at all.
RESPONSE_HEADERS_POLICY_KEY = "FILEMAN_CDN_RESPONSE_HEADERS_POLICY"
SIMPLE_CORS_POLICY_ID = "60669652-455b-4ae9-85a4-c4c02393f86c"
POLICY_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")

CDN_SWITCH_KEY = "cdn"
CDN_EXPIRES_KEY = "cdn_url_expires_in"
MANAGER_SETTING_KEYS = (CDN_SWITCH_KEY, CDN_EXPIRES_KEY)

RSA_KEY_BITS = 2048

# A manager root path that can be a CloudFront path pattern literally: no
# `*` or `?` (wildcards), no `%`, space or quote (encoding ambiguity), only
# the characters CloudFront accepts in a pattern.
PREFIX_SAFE_RE = re.compile(r"^[A-Za-z0-9_\-./~@:+]+\Z")

_signer_cache = {}
_signer_lock = threading.Lock()


# ----------------------------------------------------------------------
# settings and validation
# ----------------------------------------------------------------------

def max_expires_in():
    """The platform cap on a signed link's lifetime, in seconds."""
    value = settings.get(MAX_EXPIRES_IN_KEY, DEFAULT_MAX_EXPIRES_IN)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_EXPIRES_IN
    return max(MIN_EXPIRES_IN, min(value, ABSOLUTE_MAX_EXPIRES_IN))


def validate_max_expires_in(key, parsed):
    """`Setting.register_validator` hook for FILEMAN_CDN_MAX_EXPIRES_IN."""
    if isinstance(parsed, bool) or not isinstance(parsed, int):
        raise ValueError(f"{key} must be an integer number of seconds")
    if not MIN_EXPIRES_IN <= parsed <= ABSOLUTE_MAX_EXPIRES_IN:
        raise ValueError(
            f"{key} must be between {MIN_EXPIRES_IN} and {ABSOLUTE_MAX_EXPIRES_IN} seconds")


def parse_response_headers_policy(value):
    """A policy id, or None for "none" (any case, spaces ignored).
    Anything else raises ValueError."""
    if isinstance(value, str):
        text = value.strip()
        if text.lower() == "none":
            return None
        if POLICY_ID_RE.match(text):
            return text.lower()
    raise ValueError(f"{RESPONSE_HEADERS_POLICY_KEY} must be a CloudFront response headers "
                     f"policy id or \"none\"")


def response_headers_policy_id():
    """The response headers policy `enable` writes, or None for no policy."""
    value = settings.get(RESPONSE_HEADERS_POLICY_KEY, SIMPLE_CORS_POLICY_ID)
    try:
        return parse_response_headers_policy(value)
    except ValueError:
        logit.warning("fileman", f"{RESPONSE_HEADERS_POLICY_KEY} is not a policy id or \"none\"; "
                                 f"using SimpleCORS")
        return SIMPLE_CORS_POLICY_ID


def validate_response_headers_policy(key, parsed):
    """`Setting.register_validator` hook for FILEMAN_CDN_RESPONSE_HEADERS_POLICY."""
    parse_response_headers_policy(parsed)


def validate_switch(value):
    """The per-manager opt-out: None, 'on' or 'off'. Returns the normalised value."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return "on" if value else "off"
    text = str(value).strip().lower()
    if text in ("on", "true", "1", "yes"):
        return "on"
    if text in ("off", "false", "0", "no"):
        return "off"
    raise ValueError("cdn must be 'on' or 'off'")


def validate_expires_in(value):
    """A manager's cdn_url_expires_in: an integer within [60, cap]."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("cdn_url_expires_in must be an integer number of seconds")
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise ValueError("cdn_url_expires_in must be an integer number of seconds")
    cap = max_expires_in()
    if not MIN_EXPIRES_IN <= seconds <= cap:
        raise ValueError(
            f"cdn_url_expires_in must be between {MIN_EXPIRES_IN} and {cap} seconds "
            f"(the FILEMAN_CDN_MAX_EXPIRES_IN cap)")
    return seconds


def clamp_lifetime(expires_in):
    """The lifetime a signed link actually gets for a requested one."""
    cap = max_expires_in()
    if expires_in is None:
        return min(DEFAULT_EXPIRES_IN, cap)
    try:
        requested = int(expires_in)
    except (TypeError, ValueError):
        requested = DEFAULT_EXPIRES_IN
    return max(MIN_EXPIRES_IN, min(requested, cap))


def link_expires_in(manager):
    """What File/FileRendition pass for a private manager in CDN mode."""
    configured = manager.get_setting(CDN_EXPIRES_KEY)
    return clamp_lifetime(configured if configured not in (None, "") else None)


# ----------------------------------------------------------------------
# resolution
# ----------------------------------------------------------------------

def distribution_for_bucket(bucket_name):
    """The enabled CdnDistribution for a bucket, or None."""
    if not bucket_name:
        return None
    from mojo.apps.fileman.models import CdnDistribution
    return (CdnDistribution.objects
            .filter(bucket_name=bucket_name, is_enabled=True)
            .select_related("signing_key")
            .first())


def resolve_for_manager(manager):
    """The CdnDistribution a manager delivers through, or None.

    None when the manager is not S3, it is public at the top of its bucket
    (no folder of its own, so CloudFront has no unsigned behavior for it and
    it keeps S3 links), its bucket has no enabled distribution, the
    distribution has no host yet, or the manager (own setting, then top
    parent — `FileManager.get_setting` with no default) opts out.
    """
    if not getattr(manager, "is_s3", False):
        return None
    if manager.is_public and not (manager.root_path or "").strip("/"):
        return None
    try:
        if validate_switch(manager.get_setting(CDN_SWITCH_KEY)) == "off":
            return None
    except ValueError:
        return None
    distribution = distribution_for_bucket(manager.root_location)
    if distribution is None or not distribution.base_url:
        return None
    return distribution


# ----------------------------------------------------------------------
# URLs and signing
# ----------------------------------------------------------------------

def build_url(distribution, file_path):
    """The unsigned CDN URL for a stored key — same encoding as get_public_url."""
    encoded = quote(str(file_path).lstrip("/"), safe="/~")
    return f"{distribution.base_url.rstrip('/')}/{encoded}"


def ceil_to_hour(moment):
    """Round a datetime up to the next whole hour so repeated calls inside an
    hour produce the same URL and client caches keep working."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    epoch = moment.timestamp()
    return datetime.fromtimestamp(math.ceil(epoch / 3600.0) * 3600, tz=timezone.utc)


def expiry_for(expires_in, now=None):
    now = now or datetime.now(tz=timezone.utc)
    return ceil_to_hour(now + timedelta(seconds=clamp_lifetime(expires_in)))


def generate_private_key_pem():
    """A fresh RSA-2048 private key as PEM text (what CloudFront signers take)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_KEY_BITS)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()).decode()


def public_key_pem(private_key):
    from cryptography.hazmat.primitives import serialization

    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def load_private_key(pem):
    """Parse a PEM private key and refuse anything but RSA-2048.

    CloudFront accepts RSA-2048 (and ECDSA-256); botocore's documented
    CloudFrontSigner example is RSA with PKCS#1 v1.5 / SHA-1, which is what
    `Signer.sign` does.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    if isinstance(pem, str):
        pem = pem.encode()
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except Exception:
        raise ValueError("the signing key is not a readable unencrypted PEM private key")
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("the CloudFront signing key must be RSA")
    if key.key_size != RSA_KEY_BITS:
        raise ValueError(f"the CloudFront signing key must be RSA-{RSA_KEY_BITS}")
    return key


class Signer:
    """A CloudFront key id plus the private key that signs for it."""

    def __init__(self, key_id, private_key):
        self.key_id = key_id
        self.private_key = private_key

    def sign(self, message):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        return self.private_key.sign(message, padding.PKCS1v15(), hashes.SHA1())


def signer_for(signing_key):
    """The Signer for a CdnSigningKey, cached per process.

    The cache key includes the ciphertext, so a rotated or re-encrypted row
    never serves a stale key, and the KMS decrypt happens once per process
    per key rather than once per link.
    """
    if signing_key is None or not signing_key.key_id or not signing_key.mojo_secrets:
        return None
    cache_key = (signing_key.key_id, signing_key.mojo_secrets)
    signer = _signer_cache.get(cache_key)
    if signer is not None:
        return signer
    with _signer_lock:
        signer = _signer_cache.get(cache_key)
        if signer is None:
            signer = Signer(signing_key.key_id, signing_key.load_private_key())
            _signer_cache.clear()
            _signer_cache[cache_key] = signer
    return signer


def sign_url(url, signer, expires_at):
    """A CloudFront canned-policy signed URL (one resource, one expiry)."""
    from botocore.signers import CloudFrontSigner

    cf = CloudFrontSigner(signer.key_id, signer.sign)
    return cf.generate_presigned_url(url, date_less_than=expires_at)


def cdn_url(distribution, file_path, expires_in=None, signer=None, now=None):
    """The link a backend hands out in CDN mode.

    `expires_in=None` → unsigned (a public manager). Otherwise a signed link
    for `clamp_lifetime(expires_in)` seconds, rounded up to the hour. With
    no signer available the unsigned URL is returned: a private prefix's
    cache behavior requires a signature at the edge, so nothing leaks — the
    link just fails until a key exists, which `check_cdn` reports.
    """
    url = build_url(distribution, file_path)
    if expires_in is None:
        return url
    signer = signer or signer_for(distribution.signing_key)
    if signer is None:
        logit.warning("fileman", f"CDN for {distribution.bucket_name} has no signing key; "
                                 "a private link was issued unsigned")
        return url
    return sign_url(url, signer, expiry_for(expires_in, now=now))


def parse_signed_url(url):
    """Split a signed CDN URL into (resource, params) for tests and checks."""
    parsed = urlparse(url)
    resource = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    params = {}
    for part in parsed.query.split("&"):
        if "=" in part:
            name, _, value = part.partition("=")
            params[name] = value
    return resource, params
