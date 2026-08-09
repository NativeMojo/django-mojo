import boto3
from botocore.config import Config
from botocore.credentials import AssumeRoleCredentialFetcher, DeferredRefreshableCredentials
from botocore.exceptions import NoCredentialsError, PartialCredentialsError


DEFAULT_TIMEOUT = 3
DEFAULT_MAX_ATTEMPTS = 3

DEFAULT_ROLE_SESSION_NAME = "django-mojo"
# 12 hours. Deliberately not the 1 hour STS default: a presigned S3 URL signed
# with temporary credentials dies with those credentials no matter what its own
# ExpiresIn says, so a short role duration silently truncates every signed URL.
DEFAULT_ROLE_DURATION = 43200

# Shared across every fetcher in this process. botocore's _create_cache_key
# hashes RoleArn/ExternalId/DurationSeconds/RoleSessionName but NOT the source
# credentials, so two callers with different source identities and identical
# role arguments would share an entry. That is safe here only because callers
# derive the session name per-owner (the fileman backend uses the FileManager
# pk), which puts the owner into the cache key.
_ASSUME_ROLE_CACHE = {}


def get_session(access_key=None, secret_key=None, region=None, profile=None):
    """Build a boto3 session without bypassing its normal credential chain."""
    if bool(access_key) != bool(secret_key):
        missing = "AWS_SECRET" if access_key else "AWS_KEY"
        raise PartialCredentialsError(provider="django-settings", cred_var=missing)
    kwargs = {}
    if access_key and secret_key:
        kwargs.update({
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
        })
    if region:
        kwargs["region_name"] = region
    if profile:
        kwargs["profile_name"] = profile
    return boto3.Session(**kwargs)


def get_assumed_session(role_arn, region=None, external_id=None, session_name=None,
                        duration=None, access_key=None, secret_key=None, profile=None):
    """Build a boto3 session whose clients act as an assumed IAM role.

    The source identity is resolved by ``get_session``, so it may be static
    keys, a named profile, or whatever the botocore default chain finds. The
    returned session refreshes the role credentials on its own; clients created
    from it after this call pick up the assumed identity.
    """
    base = get_session(
        access_key=access_key,
        secret_key=secret_key,
        region=region,
        profile=profile,
    )
    botocore_session = base._session
    source = botocore_session.get_credentials()
    if source is None:
        # Without this the fetcher fails much later with an opaque
        # AttributeError on None.get_frozen_credentials().
        raise NoCredentialsError()

    extra_args = {"RoleSessionName": session_name or DEFAULT_ROLE_SESSION_NAME}
    if external_id:
        # STS rejects an empty/None ExternalId — it must be omitted entirely.
        extra_args["ExternalId"] = external_id
    extra_args["DurationSeconds"] = int(duration or DEFAULT_ROLE_DURATION)

    fetcher = AssumeRoleCredentialFetcher(
        client_creator=botocore_session.create_client,
        source_credentials=source,
        role_arn=role_arn,
        extra_args=extra_args,
        cache=_ASSUME_ROLE_CACHE,
    )
    botocore_session._credentials = DeferredRefreshableCredentials(
        method="assume-role",
        refresh_using=fetcher.fetch_credentials,
    )
    return base


def get_client(service, access_key=None, secret_key=None, region=None,
               profile=None, session=None, timeout=DEFAULT_TIMEOUT,
               max_attempts=DEFAULT_MAX_ATTEMPTS, endpoint_url=None,
               config=None):
    """Create a bounded AWS client, with an injection seam for tests/callers."""
    session = session or get_session(
        access_key=access_key,
        secret_key=secret_key,
        region=region,
        profile=profile,
    )
    if config is None:
        config_kwargs = {
            "connect_timeout": timeout,
            "read_timeout": timeout,
            "retries": {"max_attempts": max_attempts, "mode": "standard"},
        }
        if service == "s3":
            # Pinned because botocore's presigner falls back to SigV2 for S3
            # when the signature version is unset (API calls are v4 either
            # way). SigV2 signs Content-Type into presigned URLs, and the
            # platform presigns without one, so any uploader whose HTTP
            # client adds a Content-Type gets 403 SignatureDoesNotMatch.
            config_kwargs["signature_version"] = "s3v4"
        config = Config(**config_kwargs)
    kwargs = {"config": config}
    if region:
        kwargs["region_name"] = region
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return session.client(service, **kwargs)
