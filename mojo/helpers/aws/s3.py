from rest import settings
from objict import objict
import boto3
import botocore
from urllib.parse import urlparse
import io
import sys
from medialib import utils
import threading
import tempfile
import logging
from typing import Optional, Union, BinaryIO, Dict, List, Any

logger = logging.getLogger(__name__)

class S3Config:
    """S3 configuration holder with lazy initialization of clients and resources."""
    def __init__(self, key: str, secret: str):
        self.key = key
        self.secret = secret
        self._resource = None
        self._client = None

    @property
    def resource(self):
        if self._resource is None:
            self._resource = boto3.resource('s3',
                                           aws_access_key_id=self.key,
                                           aws_secret_access_key=self.secret)
        return self._resource

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client('s3',
                                       aws_access_key_id=self.key,
                                       aws_secret_access_key=self.secret)
        return self._client

# Initialize the global S3 configuration
S3 = S3Config(settings.AWS_KEY, settings.AWS_SECRET)


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
