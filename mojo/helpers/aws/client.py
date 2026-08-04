import boto3
from botocore.config import Config
from botocore.exceptions import PartialCredentialsError


DEFAULT_TIMEOUT = 3
DEFAULT_MAX_ATTEMPTS = 3


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
    client_config = config or Config(
        connect_timeout=timeout,
        read_timeout=timeout,
        retries={"max_attempts": max_attempts, "mode": "standard"},
    )
    kwargs = {"config": client_config}
    if region:
        kwargs["region_name"] = region
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return session.client(service, **kwargs)
