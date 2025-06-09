from mojo.helpers.settings import settings
from mojo.helpers import logit
from objict import objict
import boto3
import botocore
from urllib.parse import urlparse
import io
import tempfile
import json
from typing import Optional, Union, BinaryIO, Dict, List, Any, Tuple

logger = logit.get_logger(__name__)

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
            self._client = boto3.client('s3',
                                       aws_access_key_id=self.key,
                                       aws_secret_access_key=self.secret,
                                       region_name=self.region_name)
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
        try:
            response = S3.client.list_buckets()
            return [
                {"id": b["Name"], "name": b["Name"], "created": b["CreationDate"].timestamp()}
                for b in response.get("Buckets", [])
            ]
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to list buckets: {e}")
            return []

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


class S3Bucket:
    """
    Simple interface for S3 bucket management.

    This class provides methods to create, configure, and manage S3 buckets
    with sensible defaults.
    """

    def __init__(self, name: str):
        """
        Initialize a bucket manager for the specified bucket.

        Args:
            name: The name of the S3 bucket
        """
        self.name = name
        self.exists = self._check_exists()

    def _check_exists(self) -> bool:
        """Check if the bucket exists."""
        try:
            S3.client.head_bucket(Bucket=self.name)
            return True
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == '404':
                return False
            # If it's a different error (e.g., 403 forbidden), still return False
            # but log the error
            logger.warning(f"Error checking bucket existence: {e}")
            return False

    def create(self, region: Optional[str] = None, public_access: bool = False) -> bool:
        """
        Create the S3 bucket with optional configuration.

        Args:
            region: AWS region for the bucket. If None, uses configured region.
            public_access: Whether to allow public access to the bucket.

        Returns:
            True if bucket was created, False if it already exists
        """
        if self.exists:
            logger.info(f"Bucket {self.name} already exists")
            return False

        # Use configured region if none specified
        if region is None:
            region = S3.region_name

        create_params = {'Bucket': self.name}

        # Add region configuration if specified
        if region and region != 'us-east-1':
            create_params['CreateBucketConfiguration'] = {
                'LocationConstraint': region
            }

        try:
            S3.client.create_bucket(**create_params)
            self.exists = True

            # Configure public access blocking based on the public_access parameter
            if not public_access:
                S3.client.put_public_access_block(
                    Bucket=self.name,
                    PublicAccessBlockConfiguration={
                        'BlockPublicAcls': True,
                        'IgnorePublicAcls': True,
                        'BlockPublicPolicy': True,
                        'RestrictPublicBuckets': True
                    }
                )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to create bucket {self.name}: {e}")
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

    def make_public(self) -> bool:
        """
        Make the bucket publicly readable.

        Returns:
            True if successful, False otherwise
        """
        if not self.exists:
            logger.warning(f"Bucket {self.name} does not exist")
            return False

        try:
            # Remove public access block
            S3.client.put_public_access_block(
                Bucket=self.name,
                PublicAccessBlockConfiguration={
                    'BlockPublicAcls': False,
                    'IgnorePublicAcls': False,
                    'BlockPublicPolicy': False,
                    'RestrictPublicBuckets': False
                }
            )

            # Set public read policy
            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "PublicReadGetObject",
                        "Effect": "Allow",
                        "Principal": "*",
                        "Action": "s3:GetObject",
                        "Resource": f"arn:aws:s3:::{self.name}/*"
                    }
                ]
            }

            return self.set_policy(policy)
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to make bucket {self.name} public: {e}")
            return False

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
            S3.client.put_public_access_block(
                Bucket=self.name,
                PublicAccessBlockConfiguration={
                    'BlockPublicAcls': True,
                    'IgnorePublicAcls': True,
                    'BlockPublicPolicy': True,
                    'RestrictPublicBuckets': True
                }
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to make bucket {self.name} private: {e}")
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
