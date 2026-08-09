from mojo.helpers.settings import settings
from mojo.helpers import logit
from mojo.helpers.aws.client import get_client
from objict import objict
import boto3
import botocore
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    PartialCredentialsError,
    ReadTimeoutError,
)
from urllib.parse import urlparse
import copy
import io
import tempfile
import json
import re
import threading
import time
from typing import Optional, Union, BinaryIO, Dict, List, Any, Tuple

logger = logit.get_logger(__name__)


PRIVATE_PUBLIC_ACCESS_BLOCK = {
    "BlockPublicAcls": True,
    "IgnorePublicAcls": True,
    "BlockPublicPolicy": True,
    "RestrictPublicBuckets": True,
}
PUBLIC_POLICY_ACCESS_BLOCK = {
    "BlockPublicAcls": True,
    "IgnorePublicAcls": True,
    "BlockPublicPolicy": False,
    "RestrictPublicBuckets": False,
}
MANAGED_PUBLIC_READ_SID = "DjangoMojoBucketPublicRead"

OPERATION_DEADLINE_SECONDS = 25
OPERATION_PROVIDER_CALL_LIMIT = 200
OPERATION_LIST_PAGE_LIMIT = 20
OPERATION_DELETE_BATCH_LIMIT = 50
OPERATION_UPLOAD_ATTEMPT_LIMIT = 250
ABORT_UPLOAD_RETRY_LIMIT = 3
INVENTORY_PAGE_LIMIT = 10
INVENTORY_ROW_LIMIT = 10000
DEFAULT_BUCKET_REGION = "us-east-1"

_SAFE_PROVIDER_CODES = {
    "AccessDenied",
    "AllAccessDisabled",
    "AuthorizationHeaderMalformed",
    "BucketAlreadyExists",
    "BucketAlreadyOwnedByYou",
    "Conflict",
    "ExpiredToken",
    "InvalidAccessKeyId",
    "InvalidBucketName",
    "InvalidRequest",
    "InvalidToken",
    "MethodNotAllowed",
    "NoSuchBucket",
    "NoSuchBucketPolicy",
    "NoSuchPublicAccessBlockConfiguration",
    "NoSuchUpload",
    "NotFound",
    "PermanentRedirect",
    "RequestTimeout",
    "ServiceUnavailable",
    "SignatureDoesNotMatch",
    "SlowDown",
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
    "credentials_unavailable",
    "network_unavailable",
    "region_unavailable",
    "work_limit",
}
_MISSING_BUCKET_CODES = {"404", "NoSuchBucket", "NotFound"}
_RETRYABLE_CODES = {
    "RequestTimeout",
    "ServiceUnavailable",
    "SlowDown",
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
}
_CREDENTIAL_CODES = {
    "ExpiredToken",
    "InvalidAccessKeyId",
    "InvalidToken",
    "SignatureDoesNotMatch",
}
_REGION_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


class S3OperationError(Exception):
    """A bounded, wire-safe S3 operation failure."""

    def __init__(self, operation, provider_code="provider_error", status=502,
                 retryable=False, mutation_state="none", message=None,
                 data=None, error_code=None):
        self.operation = str(operation or "s3")[:64]
        self.provider_code = _safe_provider_code(provider_code)
        self.status = int(status)
        self.retryable = bool(retryable)
        self.mutation_state = mutation_state if mutation_state in ("none", "partial", "unknown") else "unknown"
        self.message = message or "S3 provider operation failed"
        self.data = copy.deepcopy(data) if isinstance(data, dict) else {}
        self.error_code = error_code or (
            "s3_operation_incomplete" if self.status == 409 and self.mutation_state != "none"
            else "s3_provider_error"
        )
        super().__init__(self.message)

    def __str__(self):
        return "S3 operation failed"

    def __repr__(self):
        return "S3OperationError(operation=%r, provider_code=%r, status=%r)" % (
            self.operation,
            self.provider_code,
            self.status,
        )

    def failure(self):
        return {
            "operation": self.operation,
            "provider_code": self.provider_code,
            "retryable": self.retryable,
        }


def _safe_provider_code(code):
    code = str(code or "provider_error")[:64]
    if code in _SAFE_PROVIDER_CODES:
        return code
    if code.isdigit() and code in ("301", "400", "403", "404", "409", "429", "500", "502", "503", "504"):
        return code
    return "provider_error"


def _semantic_equal(left, right):
    try:
        return json.dumps(_semantic_value(left), sort_keys=True, separators=(",", ":")) == json.dumps(
            _semantic_value(right), sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        return False


def _semantic_value(value):
    if isinstance(value, dict):
        return {key: _semantic_value(item) for key, item in value.items()}
    if isinstance(value, list):
        items = [_semantic_value(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    return value


def _client_error_parts(exc):
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return "provider_error", None
    error = response.get("Error")
    code = error.get("Code") if isinstance(error, dict) else None
    metadata = response.get("ResponseMetadata")
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    return str(code or status or "provider_error"), status


def _region_hint(exc):
    response = getattr(exc, "response", None)
    metadata = response.get("ResponseMetadata") if isinstance(response, dict) else None
    headers = metadata.get("HTTPHeaders") if isinstance(metadata, dict) else None
    hint = headers.get("x-amz-bucket-region") if isinstance(headers, dict) else None
    if isinstance(hint, str) and _REGION_RE.fullmatch(hint) and len(hint) <= 32:
        return hint
    return None


def _response_region(response, fallback=None):
    metadata = response.get("ResponseMetadata") if isinstance(response, dict) else None
    headers = metadata.get("HTTPHeaders") if isinstance(metadata, dict) else None
    region = headers.get("x-amz-bucket-region") if isinstance(headers, dict) else None
    if region is None:
        region = fallback
    if isinstance(region, str) and _REGION_RE.fullmatch(region) and len(region) <= 32:
        return region
    return None


def _map_provider_error(exc, operation, mutation_state="none"):
    if isinstance(exc, S3OperationError):
        return exc
    if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
        return S3OperationError(
            operation, "credentials_unavailable",
            status=409 if mutation_state != "none" else 503,
            retryable=True,
            mutation_state=mutation_state,
        )
    if isinstance(exc, (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError)):
        state = "unknown" if mutation_state != "none" else "none"
        return S3OperationError(
            operation, "network_unavailable",
            status=409 if state != "none" else 503,
            retryable=True,
            mutation_state=state,
        )
    if isinstance(exc, ClientError):
        raw_code, http_status = _client_error_parts(exc)
        code = _safe_provider_code(raw_code)
        retryable = code in _RETRYABLE_CODES or http_status in (429, 500, 502, 503, 504)
        if code in ("AccessDenied", "AllAccessDisabled", "403") or http_status == 403:
            status = 403
        elif code in _MISSING_BUCKET_CODES or http_status == 404:
            status = 404
        elif code in ("BucketAlreadyExists", "BucketAlreadyOwnedByYou", "Conflict", "PermanentRedirect") or http_status in (301, 409):
            status = 409
        elif code in _CREDENTIAL_CODES or retryable:
            status = 503
        else:
            status = 502
        state = mutation_state
        if mutation_state != "none" and (retryable or http_status is None or http_status >= 500):
            state = "unknown"
        if state != "none":
            status = 409
        return S3OperationError(operation, code, status=status, retryable=retryable, mutation_state=state)
    return S3OperationError(operation, "provider_error", status=502, mutation_state=mutation_state)


class S3OperationBudget:
    """Finite provider-work budget for one logical bucket operation."""

    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.provider_calls = 0
        self.list_pages = 0
        self.delete_batches = 0
        self.upload_attempts = 0

    def _raise_limit(self, operation, mutation_state):
        raise S3OperationError(
            operation,
            "work_limit",
            status=409,
            retryable=True,
            mutation_state=mutation_state,
            error_code="s3_operation_incomplete",
        )

    def before_call(self, operation, mutation_state="none"):
        if self.clock() - self.started >= OPERATION_DEADLINE_SECONDS:
            self._raise_limit(operation, mutation_state)
        if self.provider_calls >= OPERATION_PROVIDER_CALL_LIMIT:
            self._raise_limit(operation, mutation_state)
        self.provider_calls += 1

    def count_list_page(self, operation, mutation_state="none"):
        if self.list_pages >= OPERATION_LIST_PAGE_LIMIT:
            self._raise_limit(operation, mutation_state)
        self.list_pages += 1

    def count_delete_batch(self, operation, mutation_state="none"):
        if self.delete_batches >= OPERATION_DELETE_BATCH_LIMIT:
            self._raise_limit(operation, mutation_state)
        self.delete_batches += 1

    def count_upload_attempt(self, operation, mutation_state="none"):
        if self.upload_attempts >= OPERATION_UPLOAD_ATTEMPT_LIMIT:
            self._raise_limit(operation, mutation_state)
        self.upload_attempts += 1


def _default_client_factory(service, access_key=None, secret_key=None, region=None):
    return get_client(
        service,
        access_key=access_key,
        secret_key=secret_key,
        region=region,
    )

class S3Config:
    """S3 configuration holder with lazy initialization of clients and resources."""
    def __init__(self, key: str, secret: str, region_name: str):
        self.key = key
        self.secret = secret
        self.region_name = region_name
        self._resource = None
        self._client = None

    @property
    def resource(self):
        if self._resource is None:
            self._resource = boto3.resource('s3',
                                           aws_access_key_id=self.key,
                                           aws_secret_access_key=self.secret,
                                           region_name=self.region_name)
        return self._resource

    @property
    def client(self):
        if self._client is None:
            self._client = get_client(
                "s3",
                access_key=self.key,
                secret_key=self.secret,
                region=self.region_name,
            )
        return self._client

    @staticmethod
    def get_bucket(bucket_name):
        if not bucket_name:
            raise ValueError("Bucket name cannot be empty")
        return S3.resource.Bucket(bucket_name)

    @staticmethod
    def list_all_buckets():
        """
        List all S3 buckets.

        Returns:
            List of bucket names
        """
        rows = []
        try:
            paginator = S3.client.get_paginator("list_buckets")
            pages = paginator.paginate(MaxBuckets=1000)
            for page_number, response in enumerate(pages, start=1):
                if page_number > INVENTORY_PAGE_LIMIT:
                    raise S3OperationError(
                        "list_buckets", "work_limit", status=503, retryable=True,
                    )
                buckets = response.get("Buckets", []) if isinstance(response, dict) else None
                if not isinstance(buckets, list):
                    raise S3OperationError("list_buckets", "provider_error", status=502)
                for bucket in buckets:
                    name = bucket.get("Name") if isinstance(bucket, dict) else None
                    created = bucket.get("CreationDate") if isinstance(bucket, dict) else None
                    if not isinstance(name, str) or not name or not hasattr(created, "timestamp"):
                        raise S3OperationError("list_buckets", "provider_error", status=502)
                    try:
                        created_value = created.timestamp()
                    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                        raise S3OperationError("list_buckets", "provider_error", status=502) from exc
                    rows.append({"id": name, "name": name, "created": created_value})
                    if len(rows) > INVENTORY_ROW_LIMIT:
                        raise S3OperationError(
                            "list_buckets", "work_limit", status=503, retryable=True,
                        )
                continuation = response.get("ContinuationToken") or response.get("NextContinuationToken")
                if page_number == INVENTORY_PAGE_LIMIT and continuation:
                    raise S3OperationError(
                        "list_buckets", "work_limit", status=503, retryable=True,
                    )
            return rows
        except S3OperationError:
            raise
        except Exception as exc:
            error = _map_provider_error(exc, "list_buckets")
            logger.warning("S3 inventory failed provider_code=%s", error.provider_code)
            raise error from exc

# Initialize the global S3 configuration
S3 = S3Config(settings.AWS_KEY, settings.AWS_SECRET, settings.AWS_REGION)


class S3Item:
    """Class representing an S3 object with operations for upload, download, and management."""

    S3_HOST = "https://s3.amazonaws.com"

    def __init__(self, url: str):
        """
        Initialize an S3Item from a URL.

        Args:
            url: S3 URL in the format s3://bucket-name/key
        """
        self.url = url
        parsed_url = urlparse(url)
        self.bucket_name = parsed_url.netloc
        self.key = parsed_url.path.lstrip('/')
        self.object = S3.resource.Object(self.bucket_name, self.key)
        self.exists = self._check_exists()

    def _check_exists(self) -> bool:
        """Check if the S3 object exists."""
        try:
            self.object.load()
            return True
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == "404":
                return False
            raise

    def upload(self, file_obj: Union[str, BinaryIO], background: bool = False) -> None:
        """
        Upload a file to S3.

        Args:
            file_obj: File path or file-like object to upload
            background: Currently unused, kept for backward compatibility
        """
        prepared_file = self._prepare_file(file_obj)
        self.object.upload_fileobj(prepared_file)

    def create_multipart_upload(self) -> str:
        """
        Initialize a multipart upload.

        Returns:
            Upload ID for the multipart upload
        """
        self.part_num = 0
        self.parts = []
        response = S3.client.create_multipart_upload(
            Bucket=self.bucket_name,
            Key=self.key
        )
        self.upload_id = response["UploadId"]
        return self.upload_id

    def upload_part(self, chunk: bytes) -> Dict:
        """
        Upload a part in a multipart upload.

        Args:
            chunk: Bytes to upload as part

        Returns:
            Dict with part information
        """
        self.part_num += 1
        response = S3.client.upload_part(
            Bucket=self.bucket_name,
            Key=self.key,
            PartNumber=self.part_num,
            UploadId=self.upload_id,
            Body=chunk
        )
        part_info = {"PartNumber": self.part_num, "ETag": response["ETag"]}
        self.parts.append(part_info)
        return part_info

    def complete_multipart_upload(self) -> Dict:
        """
        Complete a multipart upload.

        Returns:
            S3 response
        """
        return S3.client.complete_multipart_upload(
            Bucket=self.bucket_name,
            Key=self.key,
            UploadId=self.upload_id,
            MultipartUpload={"Parts": self.parts}
        )

    @property
    def public_url(self) -> str:
        """Get the public URL for the S3 object."""
        return f"{self.S3_HOST}/{self.bucket_name}/{self.key}"

    def generate_presigned_url(self, expires: int = 600) -> str:
        """
        Generate a presigned URL for the S3 object.

        Args:
            expires: Expiration time in seconds

        Returns:
            Presigned URL
        """
        return S3.client.generate_presigned_url(
            'get_object',
            ExpiresIn=expires,
            Params={'Bucket': self.bucket_name, 'Key': self.key}
        )

    def generate_presigned_put(self, expires: int = 3600,
                               sha256_b64: Optional[str] = None,
                               content_length: Optional[int] = None) -> str:
        """
        Generate a presigned URL that permits a PUT of THIS key and no other.

        Scope, stated precisely, because it is the security property callers
        rely on: the signature covers the bucket, the key, and any parameters
        named below. A holder can write that one object until the URL expires;
        they cannot list, cannot read, and cannot choose a different key.

        When `sha256_b64` is supplied it is bound into the signature as
        `x-amz-checksum-sha256`, so **S3 itself rejects a body that does not
        hash to it** — integrity stops depending on the client being honest,
        or on a later verification pass being remembered.

        Note the encoding: S3 wants the checksum base64-encoded, while a
        manifest naturally carries hex. Convert with
        `base64.b64encode(bytes.fromhex(digest))`; passing hex here produces a
        signature the upload can never satisfy.

        Args:
            expires: Expiration in seconds (default one hour)
            sha256_b64: Base64-encoded SHA-256 of the body, bound into the URL
            content_length: Exact byte length, bound into the URL

        Returns:
            Presigned PUT URL
        """
        params = {'Bucket': self.bucket_name, 'Key': self.key}
        if sha256_b64:
            params['ChecksumSHA256'] = sha256_b64
        if content_length is not None:
            params['ContentLength'] = int(content_length)
        return S3.client.generate_presigned_url(
            'put_object',
            ExpiresIn=expires,
            Params=params
        )

    def head(self) -> Optional[Dict]:
        """
        Object metadata, or None when the key is absent.

        Returns the raw HeadObject response so callers can read
        `ChecksumSHA256` and `ContentLength` — which is how an upload is
        verified without pulling the bytes back through this process.

        `ChecksumSHA256` is only present when the object was written with one;
        an upload that bypassed the presigned URL's bound checksum will not
        have it, and a caller treating "absent" as "matches" would undo the
        entire verification step.

        `ChecksumMode="ENABLED"` is not optional: S3 omits `ChecksumSHA256`
        from HeadObject unless the request asks for it, no matter what is
        stored on the object.
        """
        try:
            return S3.client.head_object(
                Bucket=self.bucket_name, Key=self.key,
                ChecksumMode="ENABLED")
        except botocore.exceptions.ClientError as err:
            code = err.response.get("Error", {}).get("Code")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def download(self, file_obj: Optional[BinaryIO] = None) -> BinaryIO:
        """
        Download the S3 object.

        Args:
            file_obj: Optional file-like object to download to

        Returns:
            File-like object containing the downloaded data
        """
        if file_obj is None:
            file_obj = tempfile.NamedTemporaryFile()
        self.object.download_fileobj(file_obj)
        if hasattr(file_obj, 'seek'):
            file_obj.seek(0)
        return file_obj

    def delete(self) -> None:
        """Delete the S3 object."""
        self.object.delete()

    def _prepare_file(self, file_obj: Union[str, BinaryIO]) -> BinaryIO:
        """
        Prepare a file object for upload.

        Args:
            file_obj: File path or file-like object

        Returns:
            A file-like object ready for upload
        """
        if hasattr(file_obj, "read"):
            return io.BytesIO(file_obj.read().encode() if isinstance(file_obj.read(), str) else file_obj.read())

        try:
            return open(str(file_obj), "rb")
        except (IOError, TypeError):
            pass

        return file_obj



# Utility functions for common S3 operations

def presigned_put_url(bucket: str, key: str, expires: int = 3600,
                      sha256_b64: Optional[str] = None,
                      content_length: Optional[int] = None) -> str:
    """A presigned PUT for exactly one key, with no round trip.

    Deliberately NOT routed through `S3Item`: its constructor calls
    `_check_exists()`, which is a HeadObject. Minting one URL per file in a
    manifest through the object wrapper would cost a network call per file
    before anything is uploaded — thousands, for a real front-end build.

    Presigning itself is pure local signing; boto3 contacts nothing. The
    signature covers the bucket, the key, and any parameter below, so a holder
    can write that one object until it expires and cannot choose another key.

    `sha256_b64` is BASE64 of the raw digest, not hex — bind a manifest's hex
    value with `base64.b64encode(bytes.fromhex(digest))`, or you produce a
    signature the upload can never satisfy.
    """
    params = {'Bucket': bucket, 'Key': key}
    if sha256_b64:
        params['ChecksumSHA256'] = sha256_b64
    if content_length is not None:
        params['ContentLength'] = int(content_length)
    return S3.client.generate_presigned_url(
        'put_object', ExpiresIn=expires, Params=params)


def head_object(bucket: str, key: str) -> Optional[Dict]:
    """HeadObject metadata, or None when the key is absent.

    Returns the raw response so a caller can read `ChecksumSHA256` and
    `ContentLength` — verifying an upload without pulling the bytes back
    through this process. `ChecksumSHA256` is present only when the object was
    written with one; treating its absence as a match would undo the whole
    verification.

    `ChecksumMode="ENABLED"` is not optional: S3 omits `ChecksumSHA256` from
    HeadObject unless the request asks for it, no matter what is stored on the
    object. On a checksum-less object the parameter is harmless — the field is
    simply absent, which is the case callers are written to catch.
    """
    try:
        return S3.client.head_object(
            Bucket=bucket, Key=key, ChecksumMode="ENABLED")
    except botocore.exceptions.ClientError as err:
        code = err.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def upload(url: str, file_obj: Union[str, BinaryIO], background: bool = False) -> None:
    """Upload a file to S3."""
    S3Item(url).upload(file_obj, background)


def view_url_noexpire(url: str, is_secure: bool = False) -> str:
    """Get a public URL for an S3 object."""
    return S3Item(url).public_url


def view_url(url: str, expires: Optional[int] = 600, is_secure: bool = True) -> str:
    """
    Get a URL for an S3 object.

    Args:
        url: S3 URL
        expires: Expiration time in seconds, or None for a public URL
        is_secure: Whether to use HTTPS (currently unused)

    Returns:
        URL for the S3 object
    """
    if expires is None:
        return view_url_noexpire(url, is_secure)
    return S3Item(url).generate_presigned_url(expires)


def exists(url: str) -> bool:
    """Check if an S3 object exists."""
    return S3Item(url).exists


def get_file(url: str, file_obj: Optional[BinaryIO] = None) -> BinaryIO:
    """Download an S3 object to a file."""
    return S3Item(url).download(file_obj)


def delete(url: str) -> None:
    """
    Delete an S3 object or prefix.

    If the URL ends with /, all objects under that prefix are deleted.
    """
    if url.endswith("/"):
        parsed_url = urlparse(url)
        prefix = parsed_url.path.lstrip("/")
        bucket_name = parsed_url.netloc

        response = S3.client.list_objects_v2(
            Bucket=bucket_name,
            Prefix=prefix,
            MaxKeys=100
        )

        if 'Contents' in response:
            for obj in response['Contents']:
                S3.client.delete_object(
                    Bucket=bucket_name,
                    Key=obj['Key']
                )
    else:
        S3Item(url).delete()


def path(url: str) -> str:
    """Extract the path component from a URL."""
    return urlparse(url).path


class S3Bucket:
    """
    Simple interface for S3 bucket management.

    This class provides methods to create, configure, and manage S3 buckets
    with sensible defaults.
    """

    def __init__(self, name: str, client_factory=None, clock=None, budget=None,
                 region=None):
        """
        Initialize a bucket manager for the specified bucket.

        Args:
            name: The name of the S3 bucket
        """
        self.name = name
        self.client_factory = client_factory or _default_client_factory
        # HeadBucket region discovery needs a deterministic bootstrap client.
        # Keep ambient credentials, but do not pass an indeterminate region to
        # the bucket operation factory.
        self.configured_region = region or S3.region_name or DEFAULT_BUCKET_REGION
        self.region = None
        self.owner_id = None
        self._s3_control = None
        self.budget = budget or S3OperationBudget(clock=clock)
        self._client = self._make_client("s3", self.configured_region)
        self._exists_resolved = False
        self._acknowledged_mutation = False
        self._unknown_mutation = False
        self.exists = self._resolve_exists()

    def _make_client(self, service, region):
        try:
            return self.client_factory(
                service,
                access_key=S3.key,
                secret_key=S3.secret,
                region=region,
            )
        except Exception as exc:
            raise _map_provider_error(exc, "create_client") from exc

    def _mutation_state(self, ambiguous=False):
        if ambiguous or self._unknown_mutation:
            return "unknown"
        if self._acknowledged_mutation:
            return "partial"
        return "none"

    def _combined_mutation_state(self, error_state=None):
        if error_state == "unknown" or self._unknown_mutation:
            return "unknown"
        if error_state == "partial" or self._acknowledged_mutation:
            return "partial"
        return "none"

    def _call(self, operation, method, mutation=False, **kwargs):
        prior_state = self._mutation_state()
        self.budget.before_call(operation, mutation_state=prior_state)
        try:
            response = method(**kwargs)
            if mutation:
                self._acknowledged_mutation = True
            return response
        except Exception as exc:
            issued_state = prior_state
            if mutation:
                ambiguous = not isinstance(exc, ClientError)
                if isinstance(exc, ClientError):
                    raw_code, http_status = _client_error_parts(exc)
                    safe_code = _safe_provider_code(raw_code)
                    ambiguous = safe_code in _RETRYABLE_CODES or http_status in (429, 500, 502, 503, 504)
                if ambiguous:
                    issued_state = "unknown"
                    self._unknown_mutation = True
            error = _map_provider_error(exc, operation, mutation_state=issued_state)
            logger.warning(
                "S3 operation failed action=%s bucket=%s provider_code=%s state=%s",
                operation,
                self.name,
                error.provider_code,
                error.mutation_state,
            )
            raise error from exc

    def _head_once(self, client, operation="head_bucket", expected_owner=None):
        kwargs = {"Bucket": self.name}
        if expected_owner:
            kwargs["ExpectedBucketOwner"] = expected_owner
        self.budget.before_call(operation, mutation_state=self._mutation_state())
        try:
            return client.head_bucket(**kwargs)
        except Exception:
            raise

    def _resolve_exists(self, refresh=False):
        if self._exists_resolved and not refresh:
            return self.exists
        initial = self._client
        try:
            response = self._head_once(initial)
            region = _response_region(response)
        except ClientError as exc:
            raw_code, status = _client_error_parts(exc)
            if raw_code in _MISSING_BUCKET_CODES or status == 404:
                self._exists_resolved = True
                self.exists = False
                return False
            region = _region_hint(exc)
            if not region:
                error = _map_provider_error(exc, "head_bucket")
                logger.warning(
                    "S3 operation failed action=head_bucket bucket=%s provider_code=%s state=none",
                    self.name,
                    error.provider_code,
                )
                raise error from exc
        except Exception as exc:
            error = _map_provider_error(exc, "head_bucket")
            raise error from exc

        if not region:
            raise S3OperationError("head_bucket", "region_unavailable", status=409)

        regional = self._make_client("s3", region)
        try:
            verified = self._head_once(regional)
        except ClientError as exc:
            raw_code, status = _client_error_parts(exc)
            if raw_code in _MISSING_BUCKET_CODES or status == 404:
                self._exists_resolved = True
                self.exists = False
                return False
            error = _map_provider_error(exc, "head_bucket")
            raise error from exc
        except Exception as exc:
            error = _map_provider_error(exc, "head_bucket")
            raise error from exc
        verified_region = _response_region(verified)
        if verified_region != region:
            raise S3OperationError("head_bucket", "region_unavailable", status=409)
        self._client = regional
        self.region = region
        self.exists = True
        self._exists_resolved = True
        return True

    def _check_exists(self) -> bool:
        """Check if the bucket exists."""
        return self._resolve_exists()

    def _bind_owner(self):
        if self.owner_id:
            return self.owner_id
        sts = self._make_client("sts", self.region or self.configured_region)
        response = self._call("get_caller_identity", sts.get_caller_identity)
        account = response.get("Account") if isinstance(response, dict) else None
        if not isinstance(account, str) or not re.fullmatch(r"[0-9]{12}", account):
            raise S3OperationError("get_caller_identity", "provider_error", status=502)
        self.owner_id = account
        self._s3_control = self._make_client("s3control", self.region or self.configured_region)
        return account

    def _expected(self, values=None):
        result = dict(values or {})
        if self.owner_id:
            result["ExpectedBucketOwner"] = self.owner_id
        return result

    def _verify_owner_bound_head(self):
        try:
            response = self._head_once(self._client, expected_owner=self.owner_id)
        except Exception as exc:
            error = _map_provider_error(exc, "head_bucket", self._mutation_state())
            raise error from exc
        if _response_region(response) != self.region:
            raise S3OperationError(
                "head_bucket", "region_unavailable", status=409,
                mutation_state=self._mutation_state(),
            )
        return response

    def _read_public_access_block(self):
        try:
            response = self._call(
                "get_public_access_block",
                self._client.get_public_access_block,
                **self._expected({"Bucket": self.name}),
            )
        except S3OperationError as error:
            if error.provider_code == "NoSuchPublicAccessBlockConfiguration":
                return None
            raise
        value = response.get("PublicAccessBlockConfiguration") if isinstance(response, dict) else None
        if not isinstance(value, dict):
            raise S3OperationError("get_public_access_block", "provider_error", status=502,
                                   mutation_state=self._mutation_state())
        return {key: value.get(key) is True for key in PRIVATE_PUBLIC_ACCESS_BLOCK}

    def _put_public_access_block(self, configuration):
        prior_unknown = self._unknown_mutation
        try:
            self._call(
                "put_public_access_block",
                self._client.put_public_access_block,
                mutation=True,
                **self._expected({
                    "Bucket": self.name,
                    "PublicAccessBlockConfiguration": dict(configuration),
                }),
            )
        except S3OperationError as error:
            if error.mutation_state != "unknown":
                raise
            try:
                if self._read_public_access_block() == configuration:
                    self._unknown_mutation = prior_unknown
                    self._acknowledged_mutation = True
                    return
            except S3OperationError:
                pass
            raise error

    def _verify_public_access_block(self, expected):
        actual = self._read_public_access_block()
        if actual != expected:
            raise S3OperationError(
                "verify_public_access_block",
                "provider_error",
                status=409,
                mutation_state=self._mutation_state(),
                error_code="s3_operation_incomplete",
            )
        return actual

    def create_private(self, region=None):
        """Create an idempotent private bucket and verify its safety lock."""
        if self.exists:
            self._bind_owner()
            self._verify_owner_bound_head()
            return {"id": self.name, "name": self.name, "created_new": False}

        create_region = region or self.configured_region
        if not isinstance(create_region, str) or not _REGION_RE.fullmatch(create_region):
            raise S3OperationError("create_bucket", "region_unavailable", status=409)
        self.region = create_region
        self._client = self._make_client("s3", create_region)
        params = {"Bucket": self.name}
        if create_region != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": create_region}

        created_new = True
        try:
            self._call("create_bucket", self._client.create_bucket, mutation=True, **params)
            self.exists = True
            self._exists_resolved = True
        except S3OperationError as error:
            if error.provider_code == "BucketAlreadyExists":
                raise
            if error.provider_code == "BucketAlreadyOwnedByYou":
                created_new = None
            elif error.mutation_state == "unknown":
                created_new = None
            else:
                raise
            try:
                self._exists_resolved = False
                if not self._resolve_exists(refresh=True):
                    raise error
            except S3OperationError:
                raise error

        try:
            self._bind_owner()
            self._verify_owner_bound_head()
            self._put_public_access_block(PRIVATE_PUBLIC_ACCESS_BLOCK)
            self._verify_public_access_block(PRIVATE_PUBLIC_ACCESS_BLOCK)
        except S3OperationError as error:
            state = self._combined_mutation_state(error.mutation_state)
            data = {"name": self.name, "action": "create", "created_new": created_new,
                    "complete": False, "mutation_state": state}
            raise S3OperationError(
                error.operation,
                error.provider_code,
                status=409,
                retryable=error.retryable,
                mutation_state=state,
                message="S3 bucket creation did not reach a verified private state",
                data=data,
                error_code="s3_operation_incomplete",
            ) from error
        return {"id": self.name, "name": self.name, "created_new": created_new}

    def create(self, region: Optional[str] = None, public_access: bool = False) -> bool:
        """
        Create the S3 bucket with optional configuration.

        Args:
            region: AWS region for the bucket. If None, uses configured region.
            public_access: Whether to allow public access to the bucket.

        Returns:
            True if bucket was created, False if it already exists
        """
        try:
            if self.exists:
                return False
            if public_access:
                create_region = region or self.configured_region
                self.region = create_region
                self._client = self._make_client("s3", create_region)
                params = {"Bucket": self.name}
                if create_region != "us-east-1":
                    params["CreateBucketConfiguration"] = {"LocationConstraint": create_region}
                self._call("create_bucket", self._client.create_bucket, mutation=True, **params)
                self.exists = True
                self._exists_resolved = True
                return True
            result = self.create_private(region=region)
            return result.get("created_new") is not False
        except S3OperationError as error:
            logger.warning(
                "S3 legacy create failed bucket=%s provider_code=%s",
                self.name,
                error.provider_code,
            )
            return False

    def delete(self, force: bool = False) -> bool:
        """
        Delete the bucket.

        Args:
            force: If True, delete all objects in the bucket first

        Returns:
            True if successfully deleted, False otherwise
        """
        if not self.exists:
            logger.info(f"Bucket {self.name} does not exist")
            return False

        try:
            if force:
                self.delete_all_objects()

            S3.client.delete_bucket(Bucket=self.name)
            self.exists = False
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to delete bucket {self.name}: {e}")
            return False

    def delete_all_objects(self) -> int:
        """
        Delete all objects in the bucket.

        Returns:
            Number of objects deleted
        """
        count = 0
        try:
            # List objects in the bucket
            paginator = S3.client.get_paginator('list_objects_v2')

            for page in paginator.paginate(Bucket=self.name):
                if 'Contents' not in page:
                    continue

                # Delete objects in batches of 1000 (S3 API limit)
                objects = [{'Key': obj['Key']} for obj in page['Contents']]
                if objects:
                    S3.client.delete_objects(
                        Bucket=self.name,
                        Delete={'Objects': objects}
                    )
                    count += len(objects)

            return count
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to delete objects in bucket {self.name}: {e}")
            return count

    def set_policy(self, policy: Union[Dict, str]) -> bool:
        """
        Set a bucket policy.

        Args:
            policy: Policy document as dict or JSON string

        Returns:
            True if successful, False otherwise
        """
        if not self.exists:
            logger.warning(f"Bucket {self.name} does not exist")
            return False

        try:
            # Convert dict to JSON string if needed
            policy_str = policy if isinstance(policy, str) else json.dumps(policy)

            S3.client.put_bucket_policy(
                Bucket=self.name,
                Policy=policy_str
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to set policy for bucket {self.name}: {e}")
            return False

    def _managed_public_statement(self):
        return {
            "Sid": MANAGED_PUBLIC_READ_SID,
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::%s/*" % self.name,
        }

    def _normalize_policy(self, policy):
        if not isinstance(policy, dict):
            raise S3OperationError("get_bucket_policy", "provider_error", status=409)
        normalized = copy.deepcopy(policy)
        statements = normalized.get("Statement", [])
        if isinstance(statements, dict):
            statements = [statements]
        if not isinstance(statements, list) or any(not isinstance(item, dict) for item in statements):
            raise S3OperationError("get_bucket_policy", "provider_error", status=409)
        normalized["Statement"] = statements
        return normalized

    def _read_bucket_policy(self):
        try:
            response = self._call(
                "get_bucket_policy",
                self._client.get_bucket_policy,
                **self._expected({"Bucket": self.name}),
            )
        except S3OperationError as error:
            if error.provider_code == "NoSuchBucketPolicy":
                return {"Version": "2012-10-17", "Statement": []}
            raise
        raw_policy = response.get("Policy") if isinstance(response, dict) else None
        if not isinstance(raw_policy, str):
            raise S3OperationError("get_bucket_policy", "provider_error", status=502,
                                   mutation_state=self._mutation_state())
        try:
            return self._normalize_policy(json.loads(raw_policy))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise S3OperationError("get_bucket_policy", "provider_error", status=409,
                                   mutation_state=self._mutation_state()) from exc

    def _policy_parts(self, policy):
        managed = self._managed_public_statement()
        managed_found = False
        unrelated = copy.deepcopy(policy)
        unrelated_statements = []
        for statement in policy.get("Statement", []):
            if statement.get("Sid") == MANAGED_PUBLIC_READ_SID:
                if not _semantic_equal(statement, managed):
                    raise S3OperationError("bucket_policy_conflict", "Conflict", status=409,
                                           mutation_state=self._mutation_state())
                managed_found = True
            else:
                unrelated_statements.append(copy.deepcopy(statement))
        unrelated["Statement"] = unrelated_statements
        return managed_found, unrelated

    def _merge_public_policy(self, policy):
        managed_found, unrelated = self._policy_parts(policy)
        merged = copy.deepcopy(policy)
        if not managed_found:
            merged["Statement"] = list(merged.get("Statement", [])) + [self._managed_public_statement()]
        return merged, unrelated, managed_found

    def _put_bucket_policy(self, policy):
        prior_unknown = self._unknown_mutation
        try:
            self._call(
                "put_bucket_policy",
                self._client.put_bucket_policy,
                mutation=True,
                **self._expected({
                    "Bucket": self.name,
                    "Policy": json.dumps(policy, sort_keys=True, separators=(",", ":")),
                }),
            )
        except S3OperationError as error:
            if error.mutation_state != "unknown":
                raise
            try:
                if _semantic_equal(self._read_bucket_policy(), policy):
                    self._unknown_mutation = prior_unknown
                    self._acknowledged_mutation = True
                    return
            except S3OperationError:
                pass
            raise error

    def _read_account_public_access_block(self):
        try:
            response = self._call(
                "get_account_public_access_block",
                self._s3_control.get_public_access_block,
                AccountId=self.owner_id,
            )
        except S3OperationError as error:
            if error.provider_code == "NoSuchPublicAccessBlockConfiguration":
                return {key: False for key in PRIVATE_PUBLIC_ACCESS_BLOCK}
            raise
        value = response.get("PublicAccessBlockConfiguration") if isinstance(response, dict) else None
        if not isinstance(value, dict):
            raise S3OperationError("get_account_public_access_block", "provider_error", status=502,
                                   mutation_state=self._mutation_state())
        return {key: value.get(key) is True for key in PRIVATE_PUBLIC_ACCESS_BLOCK}

    def _read_policy_status(self):
        response = self._call(
            "get_bucket_policy_status",
            self._client.get_bucket_policy_status,
            **self._expected({"Bucket": self.name}),
        )
        status = response.get("PolicyStatus") if isinstance(response, dict) else None
        if not isinstance(status, dict) or type(status.get("IsPublic")) is not bool:
            raise S3OperationError("get_bucket_policy_status", "provider_error", status=502,
                                   mutation_state=self._mutation_state())
        return status["IsPublic"]

    def _apply_safety_lock(self):
        try:
            self._put_public_access_block(PRIVATE_PUBLIC_ACCESS_BLOCK)
            self._verify_public_access_block(PRIVATE_PUBLIC_ACCESS_BLOCK)
            return "applied", False
        except S3OperationError:
            return "failed", True

    def set_public(self, requested_public):
        """Configure and verify whole-bucket policy/PAB public posture."""
        if type(requested_public) is not bool:
            raise ValueError("set_public must be a boolean")
        if not self.exists:
            raise S3OperationError("head_bucket", "NoSuchBucket", status=404)
        self._bind_owner()
        self._verify_owner_bound_head()

        if not requested_public:
            try:
                self._put_public_access_block(PRIVATE_PUBLIC_ACCESS_BLOCK)
                self._verify_public_access_block(PRIVATE_PUBLIC_ACCESS_BLOCK)
            except S3OperationError as error:
                if error.mutation_state == "none" and not self._acknowledged_mutation:
                    data = {
                        "name": self.name,
                        "action": "set_public",
                        "requested_public": False,
                        "is_public": None,
                        "configured_public": None,
                        "safety_lock": "not_needed",
                        "complete": False,
                        "mutation_state": "none",
                    }
                    raise S3OperationError(
                        error.operation, error.provider_code, status=error.status,
                        retryable=error.retryable, mutation_state="none",
                        message=error.message, data=data,
                        error_code=error.error_code,
                    ) from error
                data = {
                    "name": self.name,
                    "action": "set_public",
                    "requested_public": False,
                    "is_public": None,
                    "configured_public": None,
                    "safety_lock": "failed",
                    "complete": False,
                    "mutation_state": self._combined_mutation_state(error.mutation_state),
                }
                state = self._combined_mutation_state(error.mutation_state)
                raise S3OperationError(
                    error.operation, error.provider_code, status=409,
                    retryable=error.retryable,
                    mutation_state=state,
                    message="S3 bucket access change did not reach a verified state",
                    data=data,
                    error_code="s3_operation_incomplete",
                ) from error
            return {
                "name": self.name,
                "is_public": False,
                "configured_public": False,
                "complete": True,
                "mutation_state": "complete",
            }

        # Full read-only preflight before changing bucket policy or PAB.
        self._read_public_access_block()
        self._read_bucket_policy()
        account_pab = self._read_account_public_access_block()
        self._read_policy_status()
        if account_pab.get("BlockPublicPolicy") or account_pab.get("RestrictPublicBuckets"):
            raise S3OperationError(
                "account_public_access_block",
                "Conflict",
                status=409,
                message="Account public access settings prevent the requested bucket posture",
            )

        # Re-read immediately before merge because policy writes have no condition token.
        immediate_policy = self._read_bucket_policy()
        merged_policy, unrelated_before, managed_found_before = self._merge_public_policy(immediate_policy)
        try:
            self._put_public_access_block(PUBLIC_POLICY_ACCESS_BLOCK)
            self._verify_public_access_block(PUBLIC_POLICY_ACCESS_BLOCK)
            if not managed_found_before:
                self._put_bucket_policy(merged_policy)

            verified_pab = self._read_public_access_block()
            verified_policy = self._read_bucket_policy()
            managed_found, unrelated_after = self._policy_parts(verified_policy)
            verified_account = self._read_account_public_access_block()
            policy_is_public = self._read_policy_status()
            verified = (
                verified_pab == PUBLIC_POLICY_ACCESS_BLOCK
                and managed_found
                and _semantic_equal(unrelated_before, unrelated_after)
                and not verified_account.get("BlockPublicPolicy")
                and not verified_account.get("RestrictPublicBuckets")
                and policy_is_public is True
            )
            if not verified:
                raise S3OperationError(
                    "verify_public_access",
                    "Conflict",
                    status=409,
                    mutation_state=self._mutation_state(),
                    error_code="s3_operation_incomplete",
                )
        except S3OperationError as error:
            safety_lock, unknown = self._apply_safety_lock()
            state = "unknown" if unknown or error.mutation_state == "unknown" else "partial"
            data = {
                "name": self.name,
                "action": "set_public",
                "requested_public": True,
                "is_public": False if safety_lock == "applied" else None,
                "configured_public": False if safety_lock == "applied" else None,
                "safety_lock": safety_lock,
                "complete": False,
                "mutation_state": state,
            }
            raise S3OperationError(
                error.operation,
                error.provider_code,
                status=409,
                retryable=error.retryable,
                mutation_state=state,
                message="S3 bucket access change did not reach a verified state",
                data=data,
                error_code="s3_operation_incomplete",
            ) from error

        return {
            "name": self.name,
            "is_public": True,
            "configured_public": True,
            "complete": True,
            "mutation_state": "complete",
        }

    def make_public(self) -> bool:
        """
        Make the bucket publicly readable.

        Returns:
            True if successful, False otherwise
        """
        try:
            self.set_public(True)
            return True
        except S3OperationError as error:
            logger.warning("S3 legacy public failed bucket=%s provider_code=%s",
                           self.name, error.provider_code)
            return False

    def _read_versioning(self):
        response = self._call(
            "get_bucket_versioning",
            self._client.get_bucket_versioning,
            **self._expected({"Bucket": self.name}),
        )
        if not isinstance(response, dict):
            raise S3OperationError("get_bucket_versioning", "provider_error", status=502,
                                   mutation_state=self._mutation_state())
        status = response.get("Status")
        if status not in (None, "Enabled", "Suspended"):
            raise S3OperationError("get_bucket_versioning", "provider_error", status=502,
                                   mutation_state=self._mutation_state())
        return status

    def _list_versions_page(self, max_keys=1000, key_marker=None,
                            version_marker=None):
        self.budget.count_list_page("list_object_versions", self._mutation_state())
        params = self._expected({"Bucket": self.name, "MaxKeys": max_keys})
        if key_marker is not None:
            params["KeyMarker"] = key_marker
        if version_marker is not None:
            params["VersionIdMarker"] = version_marker
        return self._call("list_object_versions", self._client.list_object_versions, **params)

    def _list_objects_page(self, max_keys=1000, token=None):
        self.budget.count_list_page("list_objects_v2", self._mutation_state())
        params = self._expected({"Bucket": self.name, "MaxKeys": max_keys})
        if token is not None:
            params["ContinuationToken"] = token
        return self._call("list_objects_v2", self._client.list_objects_v2, **params)

    def _list_uploads_page(self, max_uploads=1000, key_marker=None,
                           upload_marker=None):
        self.budget.count_list_page("list_multipart_uploads", self._mutation_state())
        params = self._expected({"Bucket": self.name, "MaxUploads": max_uploads})
        if key_marker is not None:
            params["KeyMarker"] = key_marker
        if upload_marker is not None:
            params["UploadIdMarker"] = upload_marker
        return self._call("list_multipart_uploads", self._client.list_multipart_uploads, **params)

    def _empty_state(self):
        return {
            "counts": {
                "deleted_objects": 0,
                "deleted_versions": 0,
                "deleted_markers": 0,
                "aborted_uploads": 0,
            },
            "failed": {"objects": 0, "versions": 0, "markers": 0, "uploads": 0},
            "remaining": {"objects": None, "versions": None, "markers": None, "uploads": None},
        }

    def _raise_empty_error(self, error, state, message=None):
        mutation_state = error.mutation_state
        if self._acknowledged_mutation and mutation_state == "none":
            mutation_state = "partial"
        data = {
            "name": self.name,
            "action": "empty",
            "complete": False,
            "mutation_state": mutation_state,
            "counts": copy.deepcopy(state["counts"]),
            "failed": copy.deepcopy(state["failed"]),
            "remaining": copy.deepcopy(state["remaining"]),
            "failure": error.failure(),
        }
        raise S3OperationError(
            error.operation,
            error.provider_code,
            status=409 if error.provider_code == "work_limit" or mutation_state != "none" else error.status,
            retryable=error.retryable,
            mutation_state=mutation_state,
            message=message or "S3 bucket empty did not reach a verified empty state",
            data=data,
            error_code="s3_operation_incomplete" if error.provider_code == "work_limit" or mutation_state != "none" else error.error_code,
        ) from error

    def _abort_uploads(self, state):
        key_marker = None
        upload_marker = None
        while True:
            page = self._list_uploads_page(
                key_marker=key_marker,
                upload_marker=upload_marker,
            )
            uploads = page.get("Uploads", []) if isinstance(page, dict) else None
            if not isinstance(uploads, list):
                raise S3OperationError("list_multipart_uploads", "provider_error", status=502,
                                       mutation_state=self._mutation_state())
            if uploads:
                for upload in uploads:
                    key = upload.get("Key") if isinstance(upload, dict) else None
                    upload_id = upload.get("UploadId") if isinstance(upload, dict) else None
                    if not isinstance(key, str) or not isinstance(upload_id, str):
                        raise S3OperationError("list_multipart_uploads", "provider_error", status=502,
                                               mutation_state=self._mutation_state())
                    acknowledged = False
                    counted = False
                    terminal = False
                    for unused in range(ABORT_UPLOAD_RETRY_LIMIT):
                        self.budget.count_upload_attempt("abort_multipart_upload", self._mutation_state())
                        try:
                            self._call(
                                "abort_multipart_upload",
                                self._client.abort_multipart_upload,
                                mutation=True,
                                **self._expected({
                                    "Bucket": self.name,
                                    "Key": key,
                                    "UploadId": upload_id,
                                }),
                            )
                            acknowledged = True
                            if not counted:
                                state["counts"]["aborted_uploads"] += 1
                                counted = True
                        except S3OperationError as error:
                            if error.provider_code != "NoSuchUpload":
                                state["failed"]["uploads"] += 1
                                raise
                        try:
                            response = self._call(
                                "list_parts",
                                self._client.list_parts,
                                **self._expected({
                                    "Bucket": self.name,
                                    "Key": key,
                                    "UploadId": upload_id,
                                    "MaxParts": 1,
                                }),
                            )
                            parts = response.get("Parts", []) if isinstance(response, dict) else None
                            if isinstance(parts, list) and not parts:
                                terminal = True
                                break
                            if not isinstance(parts, list):
                                raise S3OperationError("list_parts", "provider_error", status=502,
                                                       mutation_state=self._mutation_state())
                        except S3OperationError as error:
                            if error.provider_code == "NoSuchUpload":
                                terminal = True
                                break
                            state["failed"]["uploads"] += 1
                            raise
                    if not terminal:
                        state["failed"]["uploads"] += 1
                        raise S3OperationError(
                            "abort_multipart_upload",
                            "Conflict",
                            status=409,
                            mutation_state=self._mutation_state(),
                            error_code="s3_operation_incomplete",
                        )
                # Listing continuation state is stale after aborts. Restart.
                key_marker = None
                upload_marker = None
                continue
            if not page.get("IsTruncated"):
                return
            next_key = page.get("NextKeyMarker")
            next_upload = page.get("NextUploadIdMarker")
            if not isinstance(next_key, str) or not isinstance(next_upload, str):
                raise S3OperationError("list_multipart_uploads", "provider_error", status=502,
                                       mutation_state=self._mutation_state())
            key_marker = next_key
            upload_marker = next_upload

    def _delete_batch(self, identifiers, classifications, state):
        self.budget.count_delete_batch("delete_objects", self._mutation_state())
        prior_acknowledged = self._acknowledged_mutation
        response = self._call(
            "delete_objects",
            self._client.delete_objects,
            mutation=True,
            **self._expected({
                "Bucket": self.name,
                "Delete": {"Objects": identifiers, "Quiet": False},
            }),
        )
        # DeleteObjects acknowledges each identifier separately. A successful
        # HTTP response is not itself deletion evidence.
        self._acknowledged_mutation = prior_acknowledged
        deleted = response.get("Deleted", []) if isinstance(response, dict) else None
        errors = response.get("Errors", []) if isinstance(response, dict) else None
        if not isinstance(deleted, list) or not isinstance(errors, list):
            raise S3OperationError("delete_objects", "provider_error", status=409,
                                   mutation_state="unknown", error_code="s3_operation_incomplete")

        requested = set(classifications)
        acknowledged = set()
        for item in deleted:
            if not isinstance(item, dict) or not isinstance(item.get("Key"), str):
                continue
            identity = (item.get("Key"), item.get("VersionId"))
            if identity in requested:
                acknowledged.add(identity)
        for identity in acknowledged:
            state["counts"][classifications[identity]] += 1
        if acknowledged:
            self._acknowledged_mutation = True

        failed_identities = set()
        failure_code = None
        for item in errors:
            if not isinstance(item, dict) or not isinstance(item.get("Key"), str):
                failure_code = failure_code or "provider_error"
                continue
            identity = (item.get("Key"), item.get("VersionId"))
            if identity not in requested:
                failure_code = failure_code or "provider_error"
                continue
            failed_identities.add(identity)
            failure_code = failure_code or _safe_provider_code(item.get("Code"))
            classification = classifications[identity]
            failed_key = {
                "deleted_objects": "objects",
                "deleted_versions": "versions",
                "deleted_markers": "markers",
            }[classification]
            state["failed"][failed_key] += 1

        unresolved = requested - acknowledged - failed_identities
        if unresolved or failed_identities or len(acknowledged) != len(requested):
            if unresolved:
                failed_state = "unknown"
            elif acknowledged or prior_acknowledged:
                failed_state = "partial"
            else:
                failed_state = "none"
            raise S3OperationError(
                "delete_objects",
                failure_code or "provider_error",
                status=409,
                mutation_state=failed_state,
                error_code="s3_operation_incomplete",
            )

    def _sweep_versions(self, state):
        key_marker = None
        version_marker = None
        while True:
            page = self._list_versions_page(
                key_marker=key_marker,
                version_marker=version_marker,
            )
            versions = page.get("Versions", []) if isinstance(page, dict) else None
            markers = page.get("DeleteMarkers", []) if isinstance(page, dict) else None
            if not isinstance(versions, list) or not isinstance(markers, list):
                raise S3OperationError("list_object_versions", "provider_error", status=502,
                                       mutation_state=self._mutation_state())
            identifiers = []
            classifications = {}
            for item, marker in [(item, False) for item in versions] + [(item, True) for item in markers]:
                key = item.get("Key") if isinstance(item, dict) else None
                version_id = item.get("VersionId") if isinstance(item, dict) else None
                if not isinstance(key, str) or not isinstance(version_id, str):
                    raise S3OperationError("list_object_versions", "provider_error", status=502,
                                           mutation_state=self._mutation_state())
                identity = (key, version_id)
                identifiers.append({"Key": key, "VersionId": version_id})
                if marker:
                    classifications[identity] = "deleted_markers"
                elif version_id == "null":
                    classifications[identity] = "deleted_objects"
                else:
                    classifications[identity] = "deleted_versions"
                if len(identifiers) == 1000:
                    break
            if identifiers:
                self._delete_batch(identifiers, classifications, state)
                key_marker = None
                version_marker = None
                continue
            if not page.get("IsTruncated"):
                return
            next_key = page.get("NextKeyMarker")
            next_version = page.get("NextVersionIdMarker")
            if not isinstance(next_key, str) or not isinstance(next_version, str):
                raise S3OperationError("list_object_versions", "provider_error", status=502,
                                       mutation_state=self._mutation_state())
            key_marker = next_key
            version_marker = next_version

    def _sweep_unversioned_objects(self, state):
        token = None
        while True:
            if self._read_versioning() is not None:
                raise S3OperationError(
                    "versioning_changed",
                    "Conflict",
                    status=409,
                    mutation_state=self._mutation_state(),
                    error_code="s3_operation_incomplete",
                )
            page = self._list_objects_page(token=token)
            contents = page.get("Contents", []) if isinstance(page, dict) else None
            if not isinstance(contents, list):
                raise S3OperationError("list_objects_v2", "provider_error", status=502,
                                       mutation_state=self._mutation_state())
            identifiers = []
            classifications = {}
            for item in contents[:1000]:
                key = item.get("Key") if isinstance(item, dict) else None
                if not isinstance(key, str):
                    raise S3OperationError("list_objects_v2", "provider_error", status=502,
                                           mutation_state=self._mutation_state())
                identifiers.append({"Key": key})
                classifications[(key, None)] = "deleted_objects"
            if identifiers:
                self._delete_batch(identifiers, classifications, state)
                token = None
                continue
            if not page.get("IsTruncated"):
                return
            next_token = page.get("NextContinuationToken")
            if not isinstance(next_token, str):
                raise S3OperationError("list_objects_v2", "provider_error", status=502,
                                       mutation_state=self._mutation_state())
            token = next_token

    def _final_empty_probes(self, state):
        objects = self._list_objects_page(max_keys=1)
        versions = self._list_versions_page(max_keys=1)
        uploads = self._list_uploads_page(max_uploads=1)
        object_rows = objects.get("Contents", []) if isinstance(objects, dict) else None
        version_rows = versions.get("Versions", []) if isinstance(versions, dict) else None
        marker_rows = versions.get("DeleteMarkers", []) if isinstance(versions, dict) else None
        upload_rows = uploads.get("Uploads", []) if isinstance(uploads, dict) else None
        for rows in (object_rows, version_rows, marker_rows, upload_rows):
            if not isinstance(rows, list):
                raise S3OperationError("verify_empty", "provider_error", status=502,
                                       mutation_state=self._mutation_state())
        state["remaining"] = {
            "objects": 1 if object_rows else 0,
            "versions": 1 if version_rows else 0,
            "markers": 1 if marker_rows else 0,
            "uploads": 1 if upload_rows else 0,
        }
        if any(state["remaining"].values()):
            raise S3OperationError(
                "verify_empty",
                "Conflict",
                status=409,
                mutation_state=self._mutation_state(),
                error_code="s3_operation_incomplete",
            )

    def empty(self):
        """Permanently empty a bucket with bounded, acknowledged operations."""
        state = self._empty_state()
        if not self.exists:
            raise S3OperationError("head_bucket", "NoSuchBucket", status=404)
        try:
            self._bind_owner()
            # Complete read-only preflight before the first mutation.
            self._verify_owner_bound_head()
            self._read_versioning()
            self._list_versions_page(max_keys=1)
            self._list_objects_page(max_keys=1)
            self._list_uploads_page(max_uploads=1)

            self._abort_uploads(state)
            versioning = self._read_versioning()
            self._sweep_versions(state)
            if versioning is None:
                self._sweep_unversioned_objects(state)
            self._read_versioning()
            self._final_empty_probes(state)
        except S3OperationError as error:
            self._raise_empty_error(error, state)

        return {
            "name": self.name,
            "complete": True,
            "mutation_state": "complete",
            "deleted_objects": state["counts"]["deleted_objects"],
            "deleted_versions": state["counts"]["deleted_versions"],
            "deleted_markers": state["counts"]["deleted_markers"],
            "aborted_uploads": state["counts"]["aborted_uploads"],
        }

    def enable_website(self, index_document: str = 'index.html',
                       error_document: Optional[str] = None) -> bool:
        """
        Configure the bucket for static website hosting.

        Args:
            index_document: Default index document
            error_document: Custom error document

        Returns:
            True if successful, False otherwise
        """
        if not self.exists:
            logger.warning(f"Bucket {self.name} does not exist")
            return False

        try:
            website_config = {
                'IndexDocument': {'Suffix': index_document}
            }

            if error_document:
                website_config['ErrorDocument'] = {'Key': error_document}

            S3.client.put_bucket_website(
                Bucket=self.name,
                WebsiteConfiguration=website_config
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to configure website for bucket {self.name}: {e}")
            return False

    def get_website_url(self) -> str:
        """
        Get the website URL for this bucket.

        Returns:
            The website URL
        """
        region = S3.region_name

        # Special URL format for us-east-1
        if region == 'us-east-1':
            return f"http://{self.name}.s3-website-{region}.amazonaws.com"
        else:
            return f"http://{self.name}.s3-website.{region}.amazonaws.com"

    def list_objects(self, prefix: str = '', max_keys: int = 1000) -> List[Dict]:
        """
        List objects in the bucket.

        Args:
            prefix: Filter objects by prefix
            max_keys: Maximum number of keys to return

        Returns:
            List of object metadata dictionaries
        """
        if not self.exists:
            logger.warning(f"Bucket {self.name} does not exist")
            return []

        try:
            response = S3.client.list_objects_v2(
                Bucket=self.name,
                Prefix=prefix,
                MaxKeys=max_keys
            )

            return response.get('Contents', [])
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to list objects in bucket {self.name}: {e}")
            return []

    def enable_versioning(self) -> bool:
        """
        Enable versioning on the bucket.

        Returns:
            True if successful, False otherwise
        """
        if not self.exists:
            logger.warning(f"Bucket {self.name} does not exist")
            return False

        try:
            S3.client.put_bucket_versioning(
                Bucket=self.name,
                VersioningConfiguration={'Status': 'Enabled'}
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to enable versioning for bucket {self.name}: {e}")
            return False


    def make_path_public(self, prefix: str):
        # Get the current bucket policy (if any)
        try:
            current_policy = json.loads(S3.client.get_bucket_policy(Bucket=self.name)["Policy"])
            statements = current_policy.get("Statement", [])
        except S3.client.exceptions.from_code('NoSuchBucketPolicy'):
            current_policy = {"Version": "2012-10-17", "Statement": []}
            statements = []

        # Check if our public-read rule for the prefix already exists
        public_read_sid = f"AllowPublicReadForPrefix_{prefix.strip('/')}"
        already_exists = any(
            stmt.get("Sid") == public_read_sid for stmt in statements
        )

        if already_exists:
            print(f"Policy for prefix '{prefix}' already exists.")
            return

        # Construct the public read statement for the given prefix
        new_statement = {
            "Sid": public_read_sid,
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{self.name}/{prefix}*"
        }

        # Add and apply the new policy
        current_policy["Statement"].append(new_statement)
        S3.client.put_bucket_policy(
            Bucket=self.name,
            Policy=json.dumps(current_policy)
        )

    def make_private(self):
        try:
            self.set_public(False)
            return True
        except S3OperationError as error:
            logger.warning("S3 legacy private failed bucket=%s provider_code=%s",
                           self.name, error.provider_code)
            return False


    def enable_cors(self):
        try:
            S3.client.put_bucket_cors(
                Bucket=self.name,
                CORSConfiguration={
                    'CORSRules': [
                        {
                            'AllowedHeaders': ['*'],
                            'AllowedMethods': ['GET', 'HEAD', 'PUT', 'POST', 'DELETE'],
                            'AllowedOrigins': ['*'],
                            'ExposeHeaders': ['ETag', 'x-amz-version-id'],
                            'MaxAgeSeconds': 3000
                        }
                    ]
                }
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to enable CORS for bucket {self.name}: {e}")
            return False

    def enable_lifecycle(self):
        try:
            S3.client.put_bucket_lifecycle_configuration(
                Bucket=self.name,
                LifecycleConfiguration={
                    'Rules': [
                        {
                            'Expiration': {'Days': 30},
                            'ID': 'DeleteAfter30Days',
                            'Filter': {'Prefix': 'logs/'},
                            'Status': 'Enabled'
                        }
                    ]
                }
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to enable lifecycle for bucket {self.name}: {e}")
            return False

    def get_url(self, key: str, presigned: bool = False,
                expires: int = 3600) -> str:
        """
        Get a URL for an object in the bucket.

        Args:
            key: Object key
            presigned: Whether to generate a presigned URL
            expires: Expiration time in seconds for presigned URLs

        Returns:
            URL for the object
        """
        if presigned:
            return S3.client.generate_presigned_url(
                'get_object',
                Params={'Bucket': self.name, 'Key': key},
                ExpiresIn=expires
            )
        else:
            return f"https://{self.name}.s3.amazonaws.com/{key}"
