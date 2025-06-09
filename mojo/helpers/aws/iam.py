"""
AWS IAM Helper Module

Provides simple interfaces for managing AWS IAM resources.
"""

import json
import logging
import boto3
import botocore
from typing import Dict, List, Optional, Union, Any

from .client import get_session
from mojo.helpers.settings import settings
from mojo.helpers import logit

logger = logit.get_logger(__name__)


class IAMBase:
    """Base class for IAM resource management."""

    def __init__(self, access_key: Optional[str] = None, secret_key: Optional[str] = None,
                 region: Optional[str] = None):
        """
        Initialize IAM client with credentials.

        Args:
            access_key: AWS access key, defaults to settings.AWS_KEY
            secret_key: AWS secret key, defaults to settings.AWS_SECRET
            region: AWS region, defaults to settings.AWS_REGION if available
        """
        self.access_key = access_key or settings.AWS_KEY
        self.secret_key = secret_key or settings.AWS_SECRET
        self.region = region or getattr(settings, 'AWS_REGION', 'us-east-1')

        session = get_session(self.access_key, self.secret_key, self.region)
        self.client = session.client('iam')
        self.resource = session.resource('iam')


class IAMUser(IAMBase):
    """
    Simple interface for IAM user management.
    """

    def __init__(self, username: str, **kwargs):
        """
        Initialize a user manager for the specified IAM user.

        Args:
            username: The IAM username
            **kwargs: Additional arguments for IAMBase
        """
        super().__init__(**kwargs)
        self.username = username
        self.user = self.resource.User(self.username)
        self.exists = self._check_exists()

    def _check_exists(self) -> bool:
        """Check if the user exists."""
        try:
            self.user.load()
            return True
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchEntity':
                return False
            # If it's a different error, log and re-raise
            logger.error(f"Error checking user existence: {e}")
            raise

    def create(self, path: str = '/', tags: Optional[List[Dict[str, str]]] = None) -> bool:
        """
        Create the IAM user.

        Args:
            path: Path for the user
            tags: Optional tags for the user

        Returns:
            True if user was created, False if it already exists
        """
        if self.exists:
            logger.info(f"User {self.username} already exists")
            return False

        create_params = {
            'UserName': self.username,
            'Path': path
        }

        if tags:
            create_params['Tags'] = tags

        try:
            self.client.create_user(**create_params)
            self.exists = True
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to create user {self.username}: {e}")
            return False

    def delete(self, delete_access_keys: bool = True,
               delete_signing_certs: bool = True,
               delete_ssh_keys: bool = True) -> bool:
        """
        Delete the IAM user and optionally its access keys.

        Args:
            delete_access_keys: Delete access keys
            delete_signing_certs: Delete signing certificates
            delete_ssh_keys: Delete SSH public keys

        Returns:
            True if successfully deleted, False otherwise
        """
        if not self.exists:
            logger.info(f"User {self.username} does not exist")
            return False

        try:
            # Delete access keys if requested
            if delete_access_keys:
                for key in list(self.user.access_keys.all()):
                    key.delete()

            # Delete signing certificates if requested
            if delete_signing_certs:
                for cert in list(self.user.signing_certificates.all()):
                    cert.delete()

            # Delete SSH public keys if requested
            if delete_ssh_keys:
                response = self.client.list_ssh_public_keys(UserName=self.username)
                for key in response.get('SSHPublicKeys', []):
                    self.client.delete_ssh_public_key(
                        UserName=self.username,
                        SSHPublicKeyId=key['SSHPublicKeyId']
                    )

            # Delete user's policies
            for policy in list(self.user.attached_policies.all()):
                self.user.detach_policy(PolicyArn=policy.arn)

            # Delete the user
            self.user.delete()
            self.exists = False
            return True

        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to delete user {self.username}: {e}")
            return False

    def create_access_key(self) -> Dict[str, str]:
        """
        Create an access key for the user.

        Returns:
            Dict containing 'AccessKeyId' and 'SecretAccessKey'
        """
        if not self.exists:
            logger.warning(f"User {self.username} does not exist")
            return {}

        try:
            response = self.client.create_access_key(UserName=self.username)
            return {
                'AccessKeyId': response['AccessKey']['AccessKeyId'],
                'SecretAccessKey': response['AccessKey']['SecretAccessKey']
            }
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to create access key for user {self.username}: {e}")
            return {}

    def attach_policy(self, policy_arn: str) -> bool:
        """
        Attach a managed policy to the user.

        Args:
            policy_arn: ARN of the policy to attach

        Returns:
            True if successful, False otherwise
        """
        if not self.exists:
            logger.warning(f"User {self.username} does not exist")
            return False

        try:
            self.client.attach_user_policy(
                UserName=self.username,
                PolicyArn=policy_arn
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to attach policy to user {self.username}: {e}")
            return False

    def detach_policy(self, policy_arn: str) -> bool:
        """
        Detach a managed policy from the user.

        Args:
            policy_arn: ARN of the policy to detach

        Returns:
            True if successful, False otherwise
        """
        if not self.exists:
            logger.warning(f"User {self.username} does not exist")
            return False

        try:
            self.client.detach_user_policy(
                UserName=self.username,
                PolicyArn=policy_arn
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to detach policy from user {self.username}: {e}")
            return False

    def add_to_group(self, group_name: str) -> bool:
        """
        Add the user to an IAM group.

        Args:
            group_name: Name of the group

        Returns:
            True if successful, False otherwise
        """
        if not self.exists:
            logger.warning(f"User {self.username} does not exist")
            return False

        try:
            self.client.add_user_to_group(
                UserName=self.username,
                GroupName=group_name
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to add user {self.username} to group {group_name}: {e}")
            return False

    def list_groups(self) -> List[str]:
        """
        List the groups the user belongs to.

        Returns:
            List of group names
        """
        if not self.exists:
            logger.warning(f"User {self.username} does not exist")
            return []

        try:
            response = self.client.list_groups_for_user(UserName=self.username)
            return [group['GroupName'] for group in response.get('Groups', [])]
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to list groups for user {self.username}: {e}")
            return []


class IAMPolicy(IAMBase):
    """
    Simple interface for IAM policy management.
    """

    def __init__(self, name: str, **kwargs):
        """
        Initialize a policy manager for the specified IAM policy.

        Args:
            name: The policy name
            **kwargs: Additional arguments for IAMBase
        """
        super().__init__(**kwargs)
        self.name = name
        self.arn = None
        self.exists = self._check_exists()

    def _check_exists(self) -> bool:
        """Check if the policy exists and set ARN if it does."""
        try:
            # List policies with this name
            response = self.client.list_policies(Scope='Local', PathPrefix='/')

            for policy in response.get('Policies', []):
                if policy['PolicyName'] == self.name:
                    self.arn = policy['Arn']
                    return True

            return False
        except botocore.exceptions.ClientError as e:
            logger.error(f"Error checking policy existence: {e}")
            return False

    def create(self, policy_document: Union[Dict, str],
               description: str = '',
               path: str = '/') -> bool:
        """
        Create an IAM policy.

        Args:
            policy_document: Policy document as dict or JSON string
            description: Policy description
            path: Policy path

        Returns:
            True if policy was created, False if it already exists
        """
        if self.exists:
            logger.info(f"Policy {self.name} already exists")
            return False

        try:
            # Convert dict to JSON string if needed
            policy_doc = policy_document if isinstance(policy_document, str) else json.dumps(policy_document)

            response = self.client.create_policy(
                PolicyName=self.name,
                PolicyDocument=policy_doc,
                Description=description,
                Path=path
            )

            self.arn = response['Policy']['Arn']
            self.exists = True
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to create policy {self.name}: {e}")
            return False

    def delete(self) -> bool:
        """
        Delete the IAM policy.

        Returns:
            True if successfully deleted, False otherwise
        """
        if not self.exists:
            logger.info(f"Policy {self.name} does not exist")
            return False

        try:
            # Delete all versions except the default version
            versions = self.client.list_policy_versions(PolicyArn=self.arn)

            for version in versions.get('Versions', []):
                if not version['IsDefaultVersion']:
                    self.client.delete_policy_version(
                        PolicyArn=self.arn,
                        VersionId=version['VersionId']
                    )

            # Delete the policy
            self.client.delete_policy(PolicyArn=self.arn)
            self.exists = False
            self.arn = None
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to delete policy {self.name}: {e}")
            return False

    def update(self, policy_document: Union[Dict, str]) -> bool:
        """
        Update the IAM policy.

        Args:
            policy_document: New policy document as dict or JSON string

        Returns:
            True if successful, False otherwise
        """
        if not self.exists:
            logger.warning(f"Policy {self.name} does not exist")
            return False

        try:
            # Convert dict to JSON string if needed
            policy_doc = policy_document if isinstance(policy_document, str) else json.dumps(policy_document)

            # Create a new policy version and set it as default
            self.client.create_policy_version(
                PolicyArn=self.arn,
                PolicyDocument=policy_doc,
                SetAsDefault=True
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to update policy {self.name}: {e}")
            return False

    def get_document(self) -> Dict:
        """
        Get the policy document.

        Returns:
            Policy document as a dict
        """
        if not self.exists:
            logger.warning(f"Policy {self.name} does not exist")
            return {}

        try:
            # Get default version ID
            policy = self.client.get_policy(PolicyArn=self.arn)
            default_version = policy['Policy']['DefaultVersionId']

            # Get the policy version document
            version = self.client.get_policy_version(
                PolicyArn=self.arn,
                VersionId=default_version
            )

            # The document is URL-encoded JSON, so we need to decode it
            import urllib.parse
            doc_json = urllib.parse.unquote(version['PolicyVersion']['Document'])

            # Convert to dict if it's a string
            if isinstance(doc_json, str):
                return json.loads(doc_json)
            return doc_json
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to get policy document for {self.name}: {e}")
            return {}

    @staticmethod
    def list_all_policies(scope: str = 'Local') -> List[Dict]:
        """
        List all IAM policies.

        Args:
            scope: 'All' for AWS managed + customer managed, 'Local' for customer managed only,
                  'AWS' for AWS managed only

        Returns:
            List of policy information dictionaries
        """
        client = boto3.client('iam',
                             aws_access_key_id=settings.AWS_KEY,
                             aws_secret_access_key=settings.AWS_SECRET)

        try:
            paginator = client.get_paginator('list_policies')
            policies = []

            for page in paginator.paginate(Scope=scope):
                policies.extend(page['Policies'])

            return policies
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to list policies: {e}")
            return []


class IAMRole(IAMBase):
    """
    Simple interface for IAM role management.
    """

    def __init__(self, name: str, **kwargs):
        """
        Initialize a role manager for the specified IAM role.

        Args:
            name: The role name
            **kwargs: Additional arguments for IAMBase
        """
        super().__init__(**kwargs)
        self.name = name
        self.role = self.resource.Role(self.name)
        self.exists = self._check_exists()

    def _check_exists(self) -> bool:
        """Check if the role exists."""
        try:
            self.role.load()
            return True
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchEntity':
                return False
            # If it's a different error, log and re-raise
            logger.error(f"Error checking role existence: {e}")
            raise

    def create(self, assume_role_policy_document: Union[Dict, str],
               description: str = '',
               path: str = '/',
               max_session_duration: int = 3600,
               tags: Optional[List[Dict[str, str]]] = None) -> bool:
        """
        Create an IAM role.

        Args:
            assume_role_policy_document: Trust policy document as dict or JSON string
            description: Role description
            path: Role path
            max_session_duration: Maximum session duration in seconds (3600-43200)
            tags: Optional tags for the role

        Returns:
            True if role was created, False if it already exists
        """
        if self.exists:
            logger.info(f"Role {self.name} already exists")
            return False

        try:
            # Convert dict to JSON string if needed
            policy_doc = assume_role_policy_document if isinstance(assume_role_policy_document, str) else json.dumps(assume_role_policy_document)

            create_params = {
                'RoleName': self.name,
                'AssumeRolePolicyDocument': policy_doc,
                'Description': description,
                'Path': path,
                'MaxSessionDuration': max_session_duration
            }

            if tags:
                create_params['Tags'] = tags

            self.client.create_role(**create_params)
            self.exists = True
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to create role {self.name}: {e}")
            return False

    def delete(self) -> bool:
        """
        Delete the IAM role.

        Returns:
            True if successfully deleted, False otherwise
        """
        if not self.exists:
            logger.info(f"Role {self.name} does not exist")
            return False

        try:
            # Detach all policies
            for policy in list(self.role.attached_policies.all()):
                self.role.detach_policy(PolicyArn=policy.arn)

            # Delete all inline policies
            for policy_name in list(self.role.policies.all()):
                policy_name.delete()

            # Delete the role
            self.role.delete()
            self.exists = False
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to delete role {self.name}: {e}")
            return False

    def attach_policy(self, policy_arn: str) -> bool:
        """
        Attach a managed policy to the role.

        Args:
            policy_arn: ARN of the policy to attach

        Returns:
            True if successful, False otherwise
        """
        if not self.exists:
            logger.warning(f"Role {self.name} does not exist")
            return False

        try:
            self.client.attach_role_policy(
                RoleName=self.name,
                PolicyArn=policy_arn
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to attach policy to role {self.name}: {e}")
            return False

    def detach_policy(self, policy_arn: str) -> bool:
        """
        Detach a managed policy from the role.

        Args:
            policy_arn: ARN of the policy to detach

        Returns:
            True if successful, False otherwise
        """
        if not self.exists:
            logger.warning(f"Role {self.name} does not exist")
            return False

        try:
            self.client.detach_role_policy(
                RoleName=self.name,
                PolicyArn=policy_arn
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to detach policy from role {self.name}: {e}")
            return False

    def update_assume_role_policy(self, policy_document: Union[Dict, str]) -> bool:
        """
        Update the role's trust policy.

        Args:
            policy_document: New trust policy document as dict or JSON string

        Returns:
            True if successful, False otherwise
        """
        if not self.exists:
            logger.warning(f"Role {self.name} does not exist")
            return False

        try:
            # Convert dict to JSON string if needed
            policy_doc = policy_document if isinstance(policy_document, str) else json.dumps(policy_document)

            self.client.update_assume_role_policy(
                RoleName=self.name,
                PolicyDocument=policy_doc
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to update trust policy for role {self.name}: {e}")
            return False

    def put_inline_policy(self, policy_name: str, policy_document: Union[Dict, str]) -> bool:
        """
        Create or update an inline policy for the role.

        Args:
            policy_name: Name of the inline policy
            policy_document: Policy document as dict or JSON string

        Returns:
            True if successful, False otherwise
        """
        if not self.exists:
            logger.warning(f"Role {self.name} does not exist")
            return False

        try:
            # Convert dict to JSON string if needed
            policy_doc = policy_document if isinstance(policy_document, str) else json.dumps(policy_document)

            self.client.put_role_policy(
                RoleName=self.name,
                PolicyName=policy_name,
                PolicyDocument=policy_doc
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to put inline policy for role {self.name}: {e}")
            return False

    @staticmethod
    def list_all_roles() -> List[Dict]:
        """
        List all IAM roles.

        Returns:
            List of role information dictionaries
        """
        client = boto3.client('iam',
                             aws_access_key_id=settings.AWS_KEY,
                             aws_secret_access_key=settings.AWS_SECRET)

        try:
            paginator = client.get_paginator('list_roles')
            roles = []

            for page in paginator.paginate():
                roles.extend(page['Roles'])

            return roles
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to list roles: {e}")
            return []


# Utility functions
def create_service_role(name: str, service: str, managed_policy_arns: Optional[List[str]] = None) -> IAMRole:
    """
    Create a role that can be assumed by an AWS service.

    Args:
        name: Role name
        service: AWS service identifier (e.g., 'ec2.amazonaws.com')
        managed_policy_arns: List of managed policy ARNs to attach

    Returns:
        IAMRole instance
    """
    role = IAMRole(name)

    if not role.exists:
        # Create trust relationship policy document
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Service": service
                    },
                    "Action": "sts:AssumeRole"
                }
            ]
        }

        role.create(
            assume_role_policy_document=trust_policy,
            description=f"Service role for {service}"
        )

        # Attach managed policies if provided
        if managed_policy_arns:
            for policy_arn in managed_policy_arns:
                role.attach_policy(policy_arn)

    return role


def get_aws_account_id() -> str:
    """
    Get the AWS account ID.

    Returns:
        AWS account ID
    """
    client = boto3.client('sts',
                         aws_access_key_id=settings.AWS_KEY,
                         aws_secret_access_key=settings.AWS_SECRET)

    try:
        return client.get_caller_identity()['Account']
    except botocore.exceptions.ClientError as e:
        logger.error(f"Failed to get AWS account ID: {e}")
        return ""
