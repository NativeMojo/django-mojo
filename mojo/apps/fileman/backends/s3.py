from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError
from botocore.client import Config
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime, timedelta
import os
import uuid
import hashlib
from urllib.parse import quote, urlparse
import json
import fnmatch
import requests

from mojo.helpers.aws.client import get_session, get_assumed_session
from mojo.helpers.aws.provider_call import safe_error_detail

from .base import StorageBackend


class S3StorageBackend(StorageBackend):
    """
    AWS S3 storage backend implementation
    """

    # Declared on the class as well as in __init__ because some callers build a
    # backend with object.__new__ and never run __init__.
    _session = None
    assume_role_arn = None

    def __init__(self, file_manager, **kwargs):
        super().__init__(file_manager, **kwargs)

        # Parse bucket name and folder path
        purl = urlparse(file_manager.backend_url)
        if purl.scheme != "s3":
            raise ValueError("Invalid scheme for S3 backend")
        self.bucket_name = purl.netloc
        self.folder_path = purl.path.lstrip('/')

        # S3 configuration
        self.region_name = self.get_setting('aws_region', 'us-east-1')
        self.access_key_id = self.get_setting('aws_key')
        self.secret_access_key = self.get_setting('aws_secret')

        # Cross-account access. When assume_role_arn is set the credentials
        # above (or the ambient default chain when they are unset) are only the
        # source identity used to call sts:AssumeRole.
        self.assume_role_arn = self.get_setting('assume_role_arn')
        self.external_id = self.get_setting('external_id')
        self.role_session_name = self.get_setting('role_session_name')
        self.assume_role_duration = self.get_setting('assume_role_duration')

        self.endpoint_url = self.get_setting('endpoint_url') or f"https://s3.{self.region_name}.amazonaws.com"
        self.signature_version = self.get_setting('signature_version', 's3v4')
        self.addressing_style = self.get_setting('addressing_style', 'auto')

        # Upload configuration
        self.upload_expires_in = self.get_setting('upload_expires_in', 3600)  # 1 hour
        self.download_expires_in = self.get_setting('download_expires_in', 3600)  # 1 hour
        self.multipart_threshold = self.get_setting('multipart_threshold', 8 * 1024 * 1024)  # 8MB
        self.max_concurrency = self.get_setting('max_concurrency', 10)

        # Security settings
        self.server_side_encryption = self.get_setting('server_side_encryption')
        self.kms_key_id = self.get_setting('kms_key_id')

        # Initialize S3 client
        self._session = None
        self._client = None
        self._resource = None

    def _build_session(self):
        """One session per backend, shared by every client it hands out."""
        if self._session is None:
            if self.assume_role_arn:
                self._session = get_assumed_session(
                    self.assume_role_arn,
                    region=self.region_name,
                    external_id=self.external_id,
                    session_name=self.role_session_name or f"django-mojo-fileman-{self.file_manager.pk}",
                    duration=self.assume_role_duration,
                    access_key=self.access_key_id,
                    secret_key=self.secret_access_key,
                )
            else:
                self._session = get_session(
                    access_key=self.access_key_id,
                    secret_key=self.secret_access_key,
                    region=self.region_name,
                )
        return self._session

    def _credential_seconds_remaining(self):
        """Seconds of life left in the signing credential, or None if unbounded."""
        try:
            credentials = self._build_session()._session.get_credentials()
            if credentials is None:
                return None
            # Resolve the deferred AssumeRole call so an expiry actually exists.
            credentials.get_frozen_credentials()
            return credentials._seconds_remaining()
        except Exception:
            return None

    def _clamp_expires_in(self, expires_in):
        """Never sign a URL that outlives the credentials that signed it.

        A presigned URL made with STS temporary credentials stops working the
        moment those credentials expire, whatever its own ExpiresIn says. Under
        an assumed role the live credential is refreshed 10-15 minutes ahead of
        expiry, so the usable window is whatever is left right now.
        """
        if not self.assume_role_arn or not expires_in:
            return expires_in
        remaining = self._credential_seconds_remaining()
        if remaining is None:
            return expires_in
        return max(1, min(int(expires_in), int(remaining)))

    @property
    def client(self):
        """Lazy initialization of S3 client"""
        if self._client is None:
            session = self._build_session()

            config = Config(
                signature_version=self.signature_version,
                connect_timeout=3,
                read_timeout=3,
                s3={
                    'addressing_style': self.addressing_style
                },
                retries={
                    'max_attempts': 3,
                    'mode': 'adaptive'
                }
            )

            self._client = session.client(
                's3',
                endpoint_url=self.endpoint_url,
                config=config
            )

        return self._client

    @property
    def resource(self):
        """Lazy initialization of S3 resource"""
        if self._resource is None:
            session = self._build_session()

            self._resource = session.resource(
                's3',
                endpoint_url=self.endpoint_url
            )

        return self._resource

    def save(self, file_obj, file_path: str, content_type: Optional[str] = None, metadata: Optional[dict] = None) -> str:
        """Save a file to S3"""
        # Prepare upload parameters
        upload_params = {
            'Bucket': self.bucket_name,
            'Key': file_path,
            'ContentType': content_type,
            'Body': file_obj
        }

        # Add server-side encryption if configured
        if self.server_side_encryption:
            upload_params['ServerSideEncryption'] = self.server_side_encryption
            if self.kms_key_id:
                upload_params['SSEKMSKeyId'] = self.kms_key_id

        if metadata:
            upload_params['Metadata'] = metadata

        # Upload the file
        self.client.put_object(**upload_params)

        return file_path

    def delete(self, file_path: str) -> bool:
        """Delete a file from S3"""
        try:
            self.client.delete_object(
                Bucket=self.bucket_name,
                Key=file_path
            )
            return True
        except ClientError:
            return False

    def delete_folder(self, folder_path: str) -> bool:
        """Delete a folder from S3"""
        # List all objects under the prefix
        response = self.client.list_objects_v2(Bucket=self.bucket_name, Prefix=folder_path)
        if 'Contents' not in response:
            return True # Folder is already empty or doesn't exist
        # Prepare delete batch
        objects_to_delete = [{'Key': obj['Key']} for obj in response['Contents']]

        # Delete in batch
        self.client.delete_objects(
            Bucket=self.bucket_name,
            Delete={'Objects': objects_to_delete}
        )
        return True

    def exists(self, file_path: str) -> bool:
        """Check if a file exists in S3"""
        try:
            self.client.head_object(
                Bucket=self.bucket_name,
                Key=file_path
            )
            return True
        except ClientError:
            return False

    def get_file_size(self, file_path: str) -> Optional[int]:
        """Get the size of a file in S3"""
        try:
            response = self.client.head_object(
                Bucket=self.bucket_name,
                Key=file_path
            )
            return response['ContentLength']
        except ClientError:
            return None

    def get_content_type(self, file_path):
        try:
            response = self.client.head_object(Bucket=self.bucket_name, Key=file_path)
            return response.get("ContentType")
        except ClientError:
            return None

    def get_url(self, file_path: str, expires_in: Optional[int] = None) -> str:
        """Get a URL to access the file, either public or pre-signed based on expiration"""
        if expires_in is None:
            url = self.get_public_url(file_path)
        else:
            # Generate a pre-signed URL
            url = self.client.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': self.bucket_name,
                    'Key': file_path
                },
                ExpiresIn=self._clamp_expires_in(expires_in)
            )
        return url

    def get_public_url(self, file_path: str) -> str:
        """Build the unsigned URL for the same endpoint used by this backend."""
        endpoint = urlparse(self.endpoint_url)
        encoded_path = quote(file_path.lstrip("/"), safe="/~")
        hostname = endpoint.hostname or ""
        is_aws_endpoint = hostname == "s3.amazonaws.com" or hostname.startswith("s3.") and hostname.endswith(".amazonaws.com")
        use_virtual = self.addressing_style == "virtual" or (
            self.addressing_style == "auto" and is_aws_endpoint
        )

        if use_virtual:
            netloc = f"{self.bucket_name}.{hostname}"
            if endpoint.port:
                netloc = f"{netloc}:{endpoint.port}"
            base_path = endpoint.path.rstrip("/")
            return f"{endpoint.scheme}://{netloc}{base_path}/{encoded_path}"

        return (
            f"{self.endpoint_url.rstrip('/')}/{quote(self.bucket_name, safe='')}/"
            f"{encoded_path}"
        )

    def supports_direct_upload(self) -> bool:
        """
        Check if this backend supports direct uploads

        Returns:
            bool: True if direct uploads are supported
        """
        return True

    def generate_upload_url(self, file_path: str, content_type: str,
                           file_size: Optional[int] = None,
                           expires_in: int = 3600) -> Dict[str, Any]:
        """Generate a pre-signed URL for direct upload to S3"""
        try:
            expires_in = self._clamp_expires_in(expires_in)

            # Conditions for the upload
            conditions = []

            # Content type condition
            if content_type:
                conditions.append({"Content-Type": content_type})

            # File size conditions
            if file_size:
                # Allow some variance in file size (±1KB)
                conditions.append(["content-length-range", max(0, file_size - 1024), file_size + 1024])

            # Server-side encryption conditions
            if self.server_side_encryption:
                conditions.append({"x-amz-server-side-encryption": self.server_side_encryption})
                if self.kms_key_id:
                    conditions.append({"x-amz-server-side-encryption-aws-kms-key-id": self.kms_key_id})
            else:
                params = dict(Bucket=self.bucket_name, Key=file_path, ContentType=content_type)
                return self.client.generate_presigned_url(
                    'put_object',
                    ExpiresIn=expires_in,
                    Params=params)
            # Generate the presigned POST
            response = self.client.generate_presigned_post(
                Bucket=self.bucket_name,
                Key=file_path,
                Fields={
                    'Content-Type': content_type,
                },
                Conditions=conditions,
                ExpiresIn=expires_in
            )

            # Add server-side encryption fields if configured
            if self.server_side_encryption:
                response['fields']['x-amz-server-side-encryption'] = self.server_side_encryption
                if self.kms_key_id:
                    response['fields']['x-amz-server-side-encryption-aws-kms-key-id'] = self.kms_key_id

            return {
                'upload_url': response['url'],
                'method': 'POST',
                'fields': response['fields'],
                'headers': {
                    'Content-Type': content_type
                }
            }

        except ClientError as e:
            raise Exception(f"Failed to generate upload URL: {e}")

    def get_file_checksum(self, file_path: str, algorithm: str = 'md5') -> Optional[str]:
        """Get file checksum from S3 metadata or calculate it"""
        try:
            # First try to get ETag (which is MD5 for non-multipart uploads)
            response = self.client.head_object(
                Bucket=self.bucket_name,
                Key=file_path
            )

            if algorithm.lower() == 'md5':
                etag = response.get('ETag', '').strip('"')
                # ETag is MD5 only for non-multipart uploads (no hyphens)
                if etag and '-' not in etag:
                    return etag

            # If ETag is not usable, download and calculate checksum
            return super().get_file_checksum(file_path, algorithm)

        except ClientError:
            return None

    def open(self, file_path: str, mode: str = 'rb'):
        """Open a file from S3"""
        if 'w' in mode or 'a' in mode:
            raise ValueError("S3 backend only supports read-only file access")

        try:
            obj = self.resource.Object(self.bucket_name, file_path)
            return obj.get()['Body']
        except ClientError as e:
            raise FileNotFoundError(f"File not found in S3: {e}")

    def list_files(self, path_prefix: str = "", limit: int = 1000) -> List[str]:
        """List files in S3 with optional path prefix"""
        try:
            paginator = self.client.get_paginator('list_objects_v2')

            page_iterator = paginator.paginate(
                Bucket=self.bucket_name,
                Prefix=path_prefix,
                PaginationConfig={'MaxItems': limit}
            )

            files = []
            for page in page_iterator:
                if 'Contents' in page:
                    for obj in page['Contents']:
                        files.append(obj['Key'])

            return files

        except ClientError:
            return []

    def copy_file(self, source_path: str, dest_path: str) -> bool:
        """Copy a file within S3"""
        try:
            copy_source = {
                'Bucket': self.bucket_name,
                'Key': source_path
            }

            self.client.copy_object(
                CopySource=copy_source,
                Bucket=self.bucket_name,
                Key=dest_path
            )

            return True

        except ClientError:
            return False

    def move_file(self, source_path: str, dest_path: str) -> bool:
        """Move a file within S3"""
        if self.copy_file(source_path, dest_path):
            return self.delete(source_path)
        return False

    def get_file_metadata(self, file_path: str) -> Dict[str, Any]:
        """Get comprehensive metadata for a file in S3"""
        try:
            response = self.client.head_object(
                Bucket=self.bucket_name,
                Key=file_path
            )

            metadata = {
                'exists': True,
                'size': response.get('ContentLength'),
                'path': file_path,
                'last_modified': response.get('LastModified'),
                'content_type': response.get('ContentType'),
                'etag': response.get('ETag', '').strip('"'),
                'storage_class': response.get('StorageClass', 'STANDARD'),
                'metadata': response.get('Metadata', {}),
                'server_side_encryption': response.get('ServerSideEncryption'),
                'version_id': response.get('VersionId')
            }

            return metadata

        except ClientError:
            return {'exists': False, 'path': file_path}

    def cleanup_expired_uploads(self, before_date: Optional[datetime] = None):
        """Clean up incomplete multipart uploads"""
        if before_date is None:
            before_date = datetime.now() - timedelta(days=1)

        try:
            paginator = self.client.get_paginator('list_multipart_uploads')

            page_iterator = paginator.paginate(Bucket=self.bucket_name)

            for page in page_iterator:
                if 'Uploads' in page:
                    for upload in page['Uploads']:
                        if upload['Initiated'].replace(tzinfo=None) < before_date:
                            self.client.abort_multipart_upload(
                                Bucket=self.bucket_name,
                                Key=upload['Key'],
                                UploadId=upload['UploadId']
                            )

        except ClientError:
            pass  # Silently ignore cleanup errors

    def get_available_space(self) -> Optional[int]:
        """S3 has virtually unlimited space"""
        return None

    def generate_file_path(self, filename: str, group_id: Optional[int] = None) -> str:
        """Generate an S3 key for the file"""
        # Use the base implementation but ensure S3-compatible paths
        path = super().generate_file_path(filename, group_id)

        # Ensure no leading slash for S3 keys
        return path.lstrip('/')



    def validate_configuration(self) -> Tuple[bool, List[str]]:
        """Validate S3 configuration"""
        errors = []

        if not self.bucket_name:
            errors.append("S3 bucket name is required")

        if self.assume_role_arn and not str(self.assume_role_arn).startswith("arn:"):
            errors.append("assume_role_arn must be an IAM role ARN (arn:...)")

        # Test connection if configuration looks valid
        if not errors:
            try:
                self.client.head_bucket(Bucket=self.bucket_name)
            except PartialCredentialsError:
                errors.append("AWS key configured without a secret (or vice versa)")
            except NoCredentialsError:
                errors.append("Invalid AWS credentials")
            except ClientError as e:
                error_code = e.response['Error']['Code']
                if error_code == '404':
                    errors.append(f"S3 bucket '{self.bucket_name}' does not exist")
                elif error_code == '403':
                    errors.append(f"Access denied to S3 bucket '{self.bucket_name}'")
                else:
                    errors.append(f"S3 connection error: {e}")

        return len(errors) == 0, errors

    def test_connection(self):
        try:
            self.client.head_bucket(Bucket=self.bucket_name)
            return True
        except PartialCredentialsError:
            raise ValueError("AWS key configured without a secret (or vice versa)")
        except NoCredentialsError:
            raise ValueError("Invalid AWS credentials")
        except ClientError as e:
            error_code = e.response['Error']['Code']
            if error_code == '404':
                raise ValueError(f"S3 bucket '{self.bucket_name}' does not exist")
            elif error_code == '403':
                raise ValueError(f"Access denied to S3 bucket '{self.bucket_name}'")
            else:
                raise ValueError(f"S3 connection error: {e}")

    # -------------------------------
    # PUBLIC ACCESS AUDIT
    # -------------------------------
    @staticmethod
    def _error_code(exc):
        return str((getattr(exc, "response", {}).get("Error") or {}).get("Code") or "")

    @staticmethod
    def _as_list(value):
        if isinstance(value, list):
            return value
        return [] if value is None else [value]

    @classmethod
    def _principal_is_public(cls, principal):
        if principal == "*":
            return True
        if isinstance(principal, dict):
            return "*" in cls._as_list(principal.get("AWS"))
        return False

    @classmethod
    def _action_can_get_object(cls, action):
        return any(
            isinstance(pattern, str)
            and fnmatch.fnmatchcase("s3:getobject", pattern.lower())
            for pattern in cls._as_list(action)
        )

    @classmethod
    def _deny_can_get_object(cls, statement):
        if "Action" in statement:
            return cls._action_can_get_object(statement.get("Action"))
        if "NotAction" in statement:
            excluded = cls._as_list(statement.get("NotAction"))
            return not any(
                isinstance(pattern, str)
                and fnmatch.fnmatchcase("s3:getobject", pattern.lower())
                for pattern in excluded
            )
        return False

    def _resource_covers_entire_prefix(self, resource):
        """Accept only a trailing-star resource with an unambiguous literal base."""
        prefix_arn = f"arn:aws:s3:::{self.bucket_name}/{self.folder_path}"
        for pattern in self._as_list(resource):
            if not isinstance(pattern, str) or not pattern.endswith("*"):
                continue
            literal = pattern[:-1]
            if any(char in literal for char in ("*", "?", "[")):
                continue
            if prefix_arn.startswith(literal):
                return True
        return False

    def _resource_may_overlap_prefix(self, resource):
        prefix_arn = f"arn:aws:s3:::{self.bucket_name}/{self.folder_path}"
        child_arn = f"{prefix_arn.rstrip('/')}/__fileman_audit_probe__"
        for pattern in self._as_list(resource):
            if not isinstance(pattern, str):
                continue
            if fnmatch.fnmatchcase(prefix_arn, pattern) or fnmatch.fnmatchcase(child_arn, pattern):
                return True
            literal = pattern.split("*", 1)[0].split("?", 1)[0].split("[", 1)[0]
            if literal.startswith(prefix_arn) or prefix_arn.startswith(literal):
                return True
        return False

    def _get_account_public_access_block(self):
        # Must use the same session as self.client: under an assumed role a
        # separate session would audit a different account than the one the
        # bucket calls actually run against.
        session = self._build_session()
        account_id = session.client("sts").get_caller_identity()["Account"]
        response = session.client("s3control").get_public_access_block(AccountId=account_id)
        return response.get("PublicAccessBlockConfiguration") or {}

    def _audit_existing_object(self, file_path):
        details = {
            "bucket": self.bucket_name,
            "prefix": self.folder_path,
            "file_path": file_path,
            "method": "object",
            "authenticated_head": None,
            "anonymous_head_status": None,
        }
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=file_path)
            details["authenticated_head"] = "exists"
        except ClientError as exc:
            code = self._error_code(exc)
            details["authenticated_head"] = code or "error"
            details["status"] = "unknown"
            details["failure"] = safe_error_detail(
                exc, "s3.head_object", "s3:GetObject")
            return False, ["Authenticated HeadObject could not confirm the object."], details
        except Exception as exc:
            details["authenticated_head"] = "error"
            details["status"] = "unknown"
            details["failure"] = safe_error_detail(exc, "s3.head_object", "s3:GetObject")
            return False, ["Authenticated HeadObject failed safely."], details

        try:
            response = requests.head(self.get_url(file_path), allow_redirects=True, timeout=3)
            details["anonymous_head_status"] = response.status_code
        except Exception as exc:
            details["status"] = "unknown"
            details["failure"] = safe_error_detail(exc, "s3.anonymous_head_object")
            return False, ["Anonymous HeadObject failed safely."], details

        if 200 <= response.status_code < 400:
            details["status"] = "public"
            return True, [], details
        if response.status_code in (401, 403):
            details["status"] = "private"
            return False, [f"Anonymous HeadObject returned {response.status_code}."], details
        details["status"] = "unknown"
        return False, [f"Anonymous HeadObject returned indeterminate status {response.status_code}."], details

    def check_public_access_for_prefix(self, file_path=None):
        """Return tri-state evidence for anonymous access to this manager prefix."""
        if file_path:
            ok, issues, details = self._audit_existing_object(file_path)
            if details.get("status") != "public":
                return ok, issues, details

            # A successful anonymous request proves this key is public, not the
            # entire FileManager prefix. Require whole-prefix policy evidence
            # before caching a manager-wide public result.
            policy_ok, policy_issues, policy_details = self.check_public_access_for_prefix()
            details["policy_evidence"] = policy_details
            if policy_ok:
                return True, [], details
            details["status"] = "unknown"
            return False, [
                "The object is public but whole-prefix public access could not be established."
            ] + list(policy_issues or []), details

        issues = []
        details = {
            "bucket": self.bucket_name,
            "prefix": self.folder_path,
            "method": "policy",
            "policy": None,
            "bucket_public_access_block": None,
            "account_public_access_block": None,
        }

        try:
            policy_text = self.client.get_bucket_policy(Bucket=self.bucket_name).get("Policy")
            policy = json.loads(policy_text) if policy_text else None
            if not isinstance(policy, dict):
                raise ValueError("bucket policy is not a JSON object")
            details["policy"] = policy
        except ClientError as exc:
            code = self._error_code(exc)
            if code in ("NoSuchBucketPolicy", "NoSuchPolicy", "404"):
                details["status"] = "private"
                return False, ["No bucket policy grants anonymous access to this prefix."], details
            details["status"] = "unknown"
            details["failure"] = safe_error_detail(
                exc, "s3.get_bucket_policy", "s3:GetBucketPolicy")
            return False, ["Unable to read bucket policy safely."], details
        except Exception as exc:
            details["status"] = "unknown"
            details["failure"] = safe_error_detail(exc, "s3.parse_bucket_policy")
            return False, ["Unable to parse bucket policy safely."], details

        raw_statements = policy.get("Statement", [])
        statements = raw_statements if isinstance(raw_statements, list) else [raw_statements]
        covering_allow = False
        ambiguous_allow = False
        matching_deny = False

        for statement in statements:
            if not isinstance(statement, dict):
                continue
            if statement.get("Effect") == "Deny" and self._deny_can_get_object(statement):
                if "NotResource" in statement or self._resource_may_overlap_prefix(statement.get("Resource")):
                    matching_deny = True
                continue
            if statement.get("Effect") != "Allow":
                continue
            if not self._principal_is_public(statement.get("Principal")):
                continue
            if not self._action_can_get_object(statement.get("Action")):
                continue
            if statement.get("Condition"):
                if self._resource_may_overlap_prefix(statement.get("Resource")):
                    ambiguous_allow = True
                continue
            if self._resource_covers_entire_prefix(statement.get("Resource")):
                covering_allow = True
            elif self._resource_may_overlap_prefix(statement.get("Resource")):
                ambiguous_allow = True

        if matching_deny:
            details["status"] = "unknown"
            return False, ["A matching deny may override anonymous GetObject access."], details
        if not covering_allow:
            if ambiguous_allow:
                details["status"] = "unknown"
                return False, ["The bucket policy has only conditional or partial public access."], details
            details["status"] = "private"
            return False, ["Bucket policy does not grant anonymous GetObject for the entire prefix."], details

        try:
            response = self.client.get_public_access_block(Bucket=self.bucket_name)
            bucket_pab = response.get("PublicAccessBlockConfiguration") or {}
        except ClientError as exc:
            code = self._error_code(exc)
            if code in ("NoSuchPublicAccessBlockConfiguration", "NoSuchPublicAccessBlock", "404"):
                bucket_pab = {}
            else:
                details["status"] = "unknown"
                details["failure"] = safe_error_detail(
                    exc, "s3.get_public_access_block",
                    "s3:GetBucketPublicAccessBlock")
                return False, ["Unable to read bucket Public Access Block safely."], details
        details["bucket_public_access_block"] = bucket_pab

        try:
            account_pab = self._get_account_public_access_block()
        except ClientError as exc:
            code = self._error_code(exc)
            if code in ("NoSuchPublicAccessBlockConfiguration", "NoSuchPublicAccessBlock", "404"):
                account_pab = {}
            else:
                details["status"] = "unknown"
                details["failure"] = safe_error_detail(
                    exc, "s3control.get_public_access_block",
                    "s3:GetAccountPublicAccessBlock")
                return False, ["Unable to read account Public Access Block safely."], details
        except Exception as exc:
            details["status"] = "unknown"
            details["failure"] = safe_error_detail(
                exc, "s3control.get_public_access_block",
                "s3:GetAccountPublicAccessBlock")
            return False, ["Unable to read account Public Access Block safely."], details
        details["account_public_access_block"] = account_pab

        for scope, config in (("Bucket", bucket_pab), ("Account", account_pab)):
            # BlockPublicPolicy rejects future PutBucketPolicy calls but does
            # not disable an existing policy. RestrictPublicBuckets changes the
            # effective read posture and is therefore conclusive here.
            if config.get("RestrictPublicBuckets"):
                issues.append(f"{scope} Public Access Block prevents effective public bucket policies.")
        if issues:
            details["status"] = "private"
            return False, issues, details

        details["status"] = "public"
        return True, [], details

    def make_path_public(self):
        # Get the current bucket policy (if any)
        try:
            current_policy = json.loads(self.client.get_bucket_policy(Bucket=self.bucket_name)["Policy"])
            statements = current_policy.get("Statement", [])
        except self.client.exceptions.from_code('NoSuchBucketPolicy'):
            current_policy = {"Version": "2012-10-17", "Statement": []}
            statements = []

        # Check if our public-read rule for the prefix already exists
        public_read_sid = f"AllowPublicReadForPrefix_{self.folder_path.replace('/', '_')}"
        already_exists = any(stmt.get("Sid") == public_read_sid for stmt in statements)

        if already_exists:
            return

        # Construct the public read statement for the given prefix
        new_statement = {
            "Sid": public_read_sid,
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{self.bucket_name}/{self.folder_path}*"
        }

        # Add and apply the new policy
        current_policy["Statement"].append(new_statement)
        self.client.put_bucket_policy(
            Bucket=self.bucket_name,
            Policy=json.dumps(current_policy))

    def make_path_private(self):
        # Get the current bucket policy (if any)
        try:
            current_policy = json.loads(self.client.get_bucket_policy(Bucket=self.bucket_name)["Policy"])
            statements = current_policy.get("Statement", [])
        except self.client.exceptions.from_code('NoSuchBucketPolicy'):
            current_policy = {"Version": "2012-10-17", "Statement": []}
            statements = []

        # Check if our public-read rule for the prefix exists
        public_read_sid = f"AllowPublicReadForPrefix_{self.folder_path.replace('/', '_')}"
        exists = any(stmt.get("Sid") == public_read_sid for stmt in statements)

        if not exists:
            return

        # Remove the public read statement for the given prefix
        statements = [stmt for stmt in statements if stmt.get("Sid") != public_read_sid]

        # Apply the updated policy
        current_policy["Statement"] = statements
        self.client.put_bucket_policy(
            Bucket=self.bucket_name,
            Policy=json.dumps(current_policy))

    def download(self, file_path: str, local_path: str) -> None:
        """Download a file from S3 to a local path"""
        try:
            with open(local_path, 'wb') as local_file:
                self.client.download_fileobj(self.bucket_name, file_path, local_file)
        except ClientError as e:
            raise Exception(f"Failed to download file from S3: {e}")

    # -------------------------------
    # CORS MANAGEMENT FOR DIRECT UPLOADS
    # -------------------------------
    def get_cors_configuration(self) -> Optional[Dict[str, Any]]:
        """
        Return the current CORS configuration for the bucket, or None if not set.
        """
        try:
            resp = self.client.get_bucket_cors(Bucket=self.bucket_name)
            return resp
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "NoSuchCORSConfiguration":
                return None
            raise

    def _default_direct_upload_cors_rule(
        self,
        allowed_origins: List[str],
        allowed_methods: Optional[List[str]] = None,
        allowed_headers: Optional[List[str]] = None,
        expose_headers: Optional[List[str]] = None,
        max_age_seconds: int = 3000,
    ) -> Dict[str, Any]:
        """
        Build a single CORS rule suitable for direct uploads via pre-signed PUT/POST.
        Note: S3 CORS applies at the bucket level, not per-prefix. Access is still
        enforced by IAM policies and the fact that we use pre-signed URLs.
        """
        if not allowed_origins or not any(str(o).strip() for o in allowed_origins):
            raise ValueError("allowed_origins must contain at least one origin")
        methods = allowed_methods or ["PUT", "HEAD"]
        headers = allowed_headers or ["*"]  # simplest and safest for signed uploads
        expose = expose_headers or ["ETag", "x-amz-request-id", "x-amz-id-2", "x-amz-version-id"]

        return {
            "CORSRules": [
                {
                    "AllowedOrigins": allowed_origins,
                    "AllowedMethods": methods,
                    "AllowedHeaders": headers,
                    "ExposeHeaders": expose,
                    "MaxAgeSeconds": max_age_seconds,
                }
            ]
        }

    def check_cors_configuration_for_direct_upload(
        self,
        allowed_origins: List[str],
        required_methods: Optional[List[str]] = None,
        required_headers: Optional[List[str]] = None,
    ) -> Tuple[bool, List[str], Optional[Dict[str, Any]]]:
        """
        Validate current CORS config can support direct uploads from the given origins.

        Returns:
            (ok, issues, current_config)
        """
        if not allowed_origins or not any(str(o).strip() for o in allowed_origins):
            raise ValueError("allowed_origins must contain at least one origin")
        issues: List[str] = []
        config = self.get_cors_configuration()
        if config is None:
            return False, ["No CORS configuration set on this bucket."], None

        if required_methods is None:
            required_methods = ["POST", "HEAD"] if self.server_side_encryption else ["PUT", "HEAD"]
        required_methods = [m.upper() for m in required_methods]
        # For PUT we often need Content-Type. For POST, headers are not required (fields are in the form).
        # Using "*" for AllowedHeaders is the simplest and reduces edge cases.
        if required_headers is None:
            required_headers = [] if self.server_side_encryption else ["content-type"]
        required_headers = [h.lower() for h in required_headers]

        # Flatten rules
        cors_rules: List[Dict[str, Any]] = config.get("CORSRules", [])

        def origin_is_covered(origin: str) -> bool:
            for rule in cors_rules:
                origins = rule.get("AllowedOrigins", [])
                if "*" in origins or origin in origins:
                    # methods
                    methods = [m.upper() for m in rule.get("AllowedMethods", [])]
                    if not all(m in methods for m in required_methods):
                        continue
                    # headers
                    hdrs = [h.lower() for h in rule.get("AllowedHeaders", [])]
                    if "*" in hdrs:
                        return True
                    if not all(h in hdrs for h in required_headers):
                        continue
                    return True
            return False

        for origin in allowed_origins:
            if not origin_is_covered(origin):
                issues.append(f"Origin not covered for direct upload: {origin}")

        return (len(issues) == 0), issues, config

    def update_cors_configuration_for_direct_upload(
        self,
        allowed_origins: List[str],
        allowed_methods: Optional[List[str]] = None,
        allowed_headers: Optional[List[str]] = None,
        expose_headers: Optional[List[str]] = None,
        max_age_seconds: int = 3000,
        merge: bool = True,
    ) -> Dict[str, Any]:
        """
        Ensure CORS allows direct uploads from allowed_origins.
        If merge=True, append our rule to any existing rules instead of replacing.
        """
        # Validate input
        if not allowed_origins or not any(str(o).strip() for o in allowed_origins):
            raise ValueError("allowed_origins must contain at least one origin")
        # If current config already satisfies requirements, do nothing
        ok, issues, current = self.check_cors_configuration_for_direct_upload(
            allowed_origins=allowed_origins,
            required_methods=allowed_methods,
            required_headers=allowed_headers,
        )
        if ok:
            return {
                "changed": False,
                "message": "Existing CORS configuration already supports direct uploads.",
                "issues": [],
                "applied_configuration": current,
            }

        new_rule_config = self._default_direct_upload_cors_rule(
            allowed_origins=allowed_origins,
            allowed_methods=allowed_methods or (["POST", "HEAD"] if self.server_side_encryption else ["PUT", "HEAD"]),
            allowed_headers=allowed_headers or ["*"],
            expose_headers=expose_headers,
            max_age_seconds=max_age_seconds,
        )

        if merge and current:
            merged = dict(CORSRules=[*current.get("CORSRules", []), *new_rule_config["CORSRules"]])
            self.client.put_bucket_cors(Bucket=self.bucket_name, CORSConfiguration=merged)
            applied = merged
        else:
            # Replace entirely with our single rule
            self.client.put_bucket_cors(Bucket=self.bucket_name, CORSConfiguration=new_rule_config)
            applied = new_rule_config

        return {
            "changed": True,
            "message": "CORS configuration updated to support direct uploads.",
            "issues": issues,
            "applied_configuration": applied,
        }

    def ensure_cors_for_direct_upload(
        self,
        allowed_origins: List[str],
        allowed_methods: Optional[List[str]] = None,
        allowed_headers: Optional[List[str]] = None,
        expose_headers: Optional[List[str]] = None,
        max_age_seconds: int = 3000,
        merge: bool = True,
    ) -> Dict[str, Any]:
        """
        Convenience wrapper that checks and updates CORS as needed.

        Example:
            backend.ensure_cors_for_direct_upload(
                ["http://localhost:3000", "https://your-prod-domain.com"]
            )
        """
        if not allowed_origins or not any(str(o).strip() for o in allowed_origins):
            raise ValueError("allowed_origins must contain at least one origin")
        result = self.update_cors_configuration_for_direct_upload(
            allowed_origins=allowed_origins,
            allowed_methods=allowed_methods,
            allowed_headers=allowed_headers,
            expose_headers=expose_headers,
            max_age_seconds=max_age_seconds,
            merge=merge,
        )
        # Re-check to confirm
        ok, issues, current = self.check_cors_configuration_for_direct_upload(
            allowed_origins=allowed_origins,
            required_methods=allowed_methods,
            required_headers=allowed_headers,
        )
        result.update({
            "verified": ok,
            "post_update_issues": issues,
            "current_configuration": current,
        })
        return result
