import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from botocore.client import Config
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime, timedelta
import os
import uuid
import hashlib
from urllib.parse import urlparse
import json

from .base import StorageBackend


class S3StorageBackend(StorageBackend):
    """
    AWS S3 storage backend implementation
    """

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
        self.endpoint_url = self.get_setting('endpoint_url')  # For S3-compatible services
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
        self._client = None
        self._resource = None

    @property
    def client(self):
        """Lazy initialization of S3 client"""
        if self._client is None:
            session = boto3.Session(
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region_name
            )

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
            session = boto3.Session(
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                region_name=self.region_name
            )

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

    def get_url(self, file_path: str, expires_in: Optional[int] = None) -> str:
        """Get a URL to access the file, either public or pre-signed based on expiration"""
        if expires_in is None:
            # Assume the bucket is public and generate a public URL
            url = f"https://{self.bucket_name}.s3.amazonaws.com/{file_path}"
        else:
            # Generate a pre-signed URL
            url = self.client.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': self.bucket_name,
                    'Key': file_path
                },
                ExpiresIn=expires_in
            )
        return url

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

        if not self.access_key_id:
            errors.append("AWS access key ID is required")

        if not self.secret_access_key:
            errors.append("AWS secret access key is required")

        # Test connection if configuration looks valid
        if not errors:
            try:
                self.client.head_bucket(Bucket=self.bucket_name)
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
