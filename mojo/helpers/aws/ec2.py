"""
AWS EC2 Helper Module

TWO CLIENT PATHS, deliberately separate:

* ``EC2Instance`` / ``EC2SecurityGroup`` — the original convenience classes.
  They build their own unbounded boto client, swallow ``ClientError`` into
  ``False``/``None`` return values, and expose no ``IamInstanceProfile`` or
  ``MetadataOptions``. Left alone, and NOT extended: a capacity mutation that
  cannot tell "denied" from "did nothing", and that launches a node with IMDSv1
  enabled and no instance role, is worse than no button at all.
* the module-level functions at the bottom — the bounded seam the Admin
  capacity actions use. Every call goes through ``ProviderCaller.call`` with
  the IAM action and the mutation flag named EXPLICITLY, so a failure raises
  ``ProviderCallError`` whose ``detail()`` is the only shape safe for an API
  response or a log line.

New code wants the module-level functions.
"""

import time
import boto3
import botocore
from typing import Dict, List, Optional, Union, Any, Tuple

from .client import get_client, get_session
from .provider_call import ProviderCaller
from mojo.helpers.settings import settings
from mojo.helpers import logit

logger = logit.get_logger(__name__, "aws.log")


class EC2Instance:
    """
    Simple interface for EC2 instance management.
    """

    def __init__(self, instance_id: Optional[str] = None, access_key: Optional[str] = None,
                 secret_key: Optional[str] = None, region: Optional[str] = None):
        """
        Initialize an EC2 instance manager.

        Args:
            instance_id: Optional EC2 instance ID
            access_key: AWS access key, defaults to settings.AWS_KEY
            secret_key: AWS secret key, defaults to settings.AWS_SECRET
            region: AWS region, defaults to settings.AWS_REGION if available
        """
        self.instance_id = instance_id
        self.access_key = access_key or settings.AWS_KEY
        self.secret_key = secret_key or settings.AWS_SECRET
        self.region = region or getattr(settings, 'AWS_REGION', 'us-east-1')

        session = get_session(self.access_key, self.secret_key, self.region)
        self.client = session.client('ec2')
        self.resource = session.resource('ec2')

        self.instance = None
        if instance_id:
            self.instance = self.resource.Instance(instance_id)
            self.exists = self._check_exists()

    def _check_exists(self) -> bool:
        """Check if the instance exists."""
        try:
            self.instance.load()
            # Check if the instance state is not 'terminated'
            return self.instance.state['Name'] != 'terminated'
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'InvalidInstanceID.NotFound':
                return False
            logger.error(f"Error checking instance existence: {e}")
            raise

    def create(self,
               ami_id: str,
               instance_type: str = 't2.micro',
               key_name: Optional[str] = None,
               security_group_ids: Optional[List[str]] = None,
               subnet_id: Optional[str] = None,
               user_data: Optional[str] = None,
               tags: Optional[List[Dict[str, str]]] = None,
               count: int = 1,
               wait_until_running: bool = True) -> Dict:
        """
        Create a new EC2 instance.

        Args:
            ami_id: Amazon Machine Image ID
            instance_type: EC2 instance type (e.g. t2.micro)
            key_name: SSH key pair name
            security_group_ids: List of security group IDs
            subnet_id: VPC subnet ID
            user_data: Initialization script
            tags: List of tags for the instance
            count: Number of instances to launch
            wait_until_running: Whether to wait until the instance is running

        Returns:
            Dict containing instance information
        """
        try:
            # Prepare run parameters
            run_params = {
                'ImageId': ami_id,
                'InstanceType': instance_type,
                'MinCount': count,
                'MaxCount': count
            }

            if key_name:
                run_params['KeyName'] = key_name

            if security_group_ids:
                run_params['SecurityGroupIds'] = security_group_ids

            if subnet_id:
                run_params['SubnetId'] = subnet_id

            if user_data:
                run_params['UserData'] = user_data

            # Launch the instance
            response = self.client.run_instances(**run_params)
            instances = response['Instances']

            # Add tags if provided
            if tags and instances:
                instance_ids = [instance['InstanceId'] for instance in instances]
                self.client.create_tags(
                    Resources=instance_ids,
                    Tags=tags
                )

            # Wait until the instance is running if requested
            if wait_until_running and instances:
                instance_ids = [instance['InstanceId'] for instance in instances]
                waiter = self.client.get_waiter('instance_running')
                waiter.wait(InstanceIds=instance_ids)

                # Reload instances to get the latest state
                instances = []
                for instance_id in instance_ids:
                    instance = self.resource.Instance(instance_id)
                    instance.load()
                    instances.append({
                        'InstanceId': instance.id,
                        'PublicIpAddress': instance.public_ip_address,
                        'PrivateIpAddress': instance.private_ip_address,
                        'State': instance.state['Name']
                    })

            # If only one instance was created, set it as the current instance
            if count == 1 and instances:
                self.instance_id = instances[0]['InstanceId']
                self.instance = self.resource.Instance(self.instance_id)
                self.exists = True

            return {'Instances': instances}
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to create EC2 instance: {e}")
            return {'Error': str(e)}

    def terminate(self, wait_until_terminated: bool = True) -> bool:
        """
        Terminate the EC2 instance.

        Args:
            wait_until_terminated: Whether to wait until the instance is terminated

        Returns:
            True if successfully terminated, False otherwise
        """
        if not self.instance_id or not self.exists:
            logger.warning("No valid instance to terminate")
            return False

        try:
            self.instance.terminate()

            if wait_until_terminated:
                waiter = self.client.get_waiter('instance_terminated')
                waiter.wait(InstanceIds=[self.instance_id])

            self.exists = False
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to terminate instance {self.instance_id}: {e}")
            return False

    def start(self, wait_until_running: bool = True) -> bool:
        """
        Start the EC2 instance.

        Args:
            wait_until_running: Whether to wait until the instance is running

        Returns:
            True if successfully started, False otherwise
        """
        if not self.instance_id or not self.exists:
            logger.warning("No valid instance to start")
            return False

        try:
            # Only start if the instance is stopped
            if self.instance.state['Name'] == 'stopped':
                self.instance.start()

                if wait_until_running:
                    waiter = self.client.get_waiter('instance_running')
                    waiter.wait(InstanceIds=[self.instance_id])
                    self.instance.load()  # Reload to get the latest state

                return True
            else:
                logger.info(f"Instance {self.instance_id} is not in 'stopped' state (current: {self.instance.state['Name']})")
                return False
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to start instance {self.instance_id}: {e}")
            return False

    def stop(self, wait_until_stopped: bool = True) -> bool:
        """
        Stop the EC2 instance.

        Args:
            wait_until_stopped: Whether to wait until the instance is stopped

        Returns:
            True if successfully stopped, False otherwise
        """
        if not self.instance_id or not self.exists:
            logger.warning("No valid instance to stop")
            return False

        try:
            # Only stop if the instance is running
            if self.instance.state['Name'] == 'running':
                self.instance.stop()

                if wait_until_stopped:
                    waiter = self.client.get_waiter('instance_stopped')
                    waiter.wait(InstanceIds=[self.instance_id])
                    self.instance.load()  # Reload to get the latest state

                return True
            else:
                logger.info(f"Instance {self.instance_id} is not in 'running' state (current: {self.instance.state['Name']})")
                return False
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to stop instance {self.instance_id}: {e}")
            return False

    def reboot(self) -> bool:
        """
        Reboot the EC2 instance.

        Returns:
            True if reboot initiated successfully, False otherwise
        """
        if not self.instance_id or not self.exists:
            logger.warning("No valid instance to reboot")
            return False

        try:
            self.instance.reboot()
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to reboot instance {self.instance_id}: {e}")
            return False

    def get_status(self) -> Dict:
        """
        Get the current status of the instance.

        Returns:
            Dict containing instance status information
        """
        if not self.instance_id or not self.exists:
            logger.warning("No valid instance to get status for")
            return {}

        try:
            self.instance.load()
            return {
                'InstanceId': self.instance.id,
                'State': self.instance.state['Name'],
                'InstanceType': self.instance.instance_type,
                'PublicIpAddress': self.instance.public_ip_address,
                'PrivateIpAddress': self.instance.private_ip_address,
                'LaunchTime': self.instance.launch_time.isoformat() if hasattr(self.instance, 'launch_time') else None,
                'Tags': self.instance.tags
            }
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to get status for instance {self.instance_id}: {e}")
            return {}

    def add_tags(self, tags: List[Dict[str, str]]) -> bool:
        """
        Add tags to the instance.

        Args:
            tags: List of tags to add

        Returns:
            True if successful, False otherwise
        """
        if not self.instance_id or not self.exists:
            logger.warning("No valid instance to add tags to")
            return False

        try:
            self.instance.create_tags(Tags=tags)
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to add tags to instance {self.instance_id}: {e}")
            return False

    def get_console_output(self) -> str:
        """
        Get the console output of the instance.

        Returns:
            Console output as a string
        """
        if not self.instance_id or not self.exists:
            logger.warning("No valid instance to get console output for")
            return ""

        try:
            response = self.client.get_console_output(InstanceId=self.instance_id)
            return response.get('Output', '')
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to get console output for instance {self.instance_id}: {e}")
            return ""

    @staticmethod
    def list_instances(filters: Optional[List[Dict[str, Any]]] = None) -> List[Dict]:
        """
        List EC2 instances with optional filtering.

        Args:
            filters: Optional list of filters

        Returns:
            List of instance dictionaries
        """
        client = boto3.client('ec2',
                             aws_access_key_id=settings.AWS_KEY,
                             aws_secret_access_key=settings.AWS_SECRET,
                             region_name=getattr(settings, 'AWS_REGION', 'us-east-1'))

        try:
            if filters:
                response = client.describe_instances(Filters=filters)
            else:
                response = client.describe_instances()

            instances = []
            for reservation in response.get('Reservations', []):
                for instance in reservation.get('Instances', []):
                    instances.append(instance)

            return instances
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to list instances: {e}")
            return []

    @staticmethod
    def get_instance_by_tag(tag_key: str, tag_value: str) -> Optional[str]:
        """
        Find an instance by tag.

        Args:
            tag_key: Tag key to search for
            tag_value: Tag value to match

        Returns:
            Instance ID if found, None otherwise
        """
        filters = [
            {
                'Name': f'tag:{tag_key}',
                'Values': [tag_value]
            }
        ]

        instances = EC2Instance.list_instances(filters)
        if instances:
            return instances[0]['InstanceId']
        return None


class EC2SecurityGroup:
    """
    Simple interface for EC2 security group management.
    """

    def __init__(self, group_id: Optional[str] = None, access_key: Optional[str] = None,
                 secret_key: Optional[str] = None, region: Optional[str] = None):
        """
        Initialize a security group manager.

        Args:
            group_id: Optional security group ID
            access_key: AWS access key, defaults to settings.AWS_KEY
            secret_key: AWS secret key, defaults to settings.AWS_SECRET
            region: AWS region, defaults to settings.AWS_REGION if available
        """
        self.group_id = group_id
        self.access_key = access_key or settings.AWS_KEY
        self.secret_key = secret_key or settings.AWS_SECRET
        self.region = region or getattr(settings, 'AWS_REGION', 'us-east-1')

        session = get_session(self.access_key, self.secret_key, self.region)
        self.client = session.client('ec2')
        self.resource = session.resource('ec2')

        self.security_group = None
        if group_id:
            self.security_group = self.resource.SecurityGroup(group_id)
            self.exists = self._check_exists()

    def _check_exists(self) -> bool:
        """Check if the security group exists."""
        try:
            self.security_group.load()
            return True
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'InvalidGroup.NotFound':
                return False
            logger.error(f"Error checking security group existence: {e}")
            raise

    def create(self, name: str, description: str, vpc_id: Optional[str] = None,
               tags: Optional[List[Dict[str, str]]] = None) -> bool:
        """
        Create a new security group.

        Args:
            name: Security group name
            description: Security group description
            vpc_id: Optional VPC ID
            tags: Optional tags for the security group

        Returns:
            True if successful, False otherwise
        """
        try:
            # Prepare creation parameters
            create_params = {
                'GroupName': name,
                'Description': description
            }

            if vpc_id:
                create_params['VpcId'] = vpc_id

            # Create the security group
            response = self.client.create_security_group(**create_params)
            self.group_id = response['GroupId']
            self.security_group = self.resource.SecurityGroup(self.group_id)
            self.exists = True

            # Add tags if provided
            if tags:
                self.security_group.create_tags(Tags=tags)

            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to create security group: {e}")
            return False

    def delete(self) -> bool:
        """
        Delete the security group.

        Returns:
            True if successful, False otherwise
        """
        if not self.group_id or not self.exists:
            logger.warning("No valid security group to delete")
            return False

        try:
            self.security_group.delete()
            self.exists = False
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to delete security group {self.group_id}: {e}")
            return False

    def authorize_ingress(self, ip_protocol: str, from_port: int, to_port: int,
                          cidr_ip: Optional[str] = None,
                          source_group_id: Optional[str] = None,
                          description: Optional[str] = None) -> bool:
        """
        Add an inbound rule to the security group.

        Args:
            ip_protocol: IP protocol (tcp, udp, icmp)
            from_port: Start port
            to_port: End port
            cidr_ip: CIDR IP range
            source_group_id: Source security group ID
            description: Rule description

        Returns:
            True if successful, False otherwise
        """
        if not self.group_id or not self.exists:
            logger.warning("No valid security group to add rule to")
            return False

        try:
            rule_params = {
                'IpProtocol': ip_protocol,
                'FromPort': from_port,
                'ToPort': to_port,
            }

            if cidr_ip:
                rule_params['CidrIp'] = cidr_ip
            elif source_group_id:
                rule_params['SourceSecurityGroupId'] = source_group_id
            else:
                raise ValueError("Either cidr_ip or source_group_id must be provided")

            if description:
                rule_params['Description'] = description

            self.security_group.authorize_ingress(
                GroupId=self.group_id,
                IpPermissions=[rule_params]
            )
            return True
        except botocore.exceptions.ClientError as e:
            if 'InvalidPermission.Duplicate' in str(e):
                # Rule already exists, not a failure
                logger.info(f"Rule already exists in security group {self.group_id}")
                return True
            logger.error(f"Failed to add ingress rule to security group {self.group_id}: {e}")
            return False

    def authorize_egress(self, ip_protocol: str, from_port: int, to_port: int,
                         cidr_ip: Optional[str] = None,
                         destination_group_id: Optional[str] = None,
                         description: Optional[str] = None) -> bool:
        """
        Add an outbound rule to the security group.

        Args:
            ip_protocol: IP protocol (tcp, udp, icmp)
            from_port: Start port
            to_port: End port
            cidr_ip: CIDR IP range
            destination_group_id: Destination security group ID
            description: Rule description

        Returns:
            True if successful, False otherwise
        """
        if not self.group_id or not self.exists:
            logger.warning("No valid security group to add rule to")
            return False

        try:
            rule_params = {
                'IpProtocol': ip_protocol,
                'FromPort': from_port,
                'ToPort': to_port,
            }

            if cidr_ip:
                rule_params['CidrIp'] = cidr_ip
            elif destination_group_id:
                rule_params['DestinationSecurityGroupId'] = destination_group_id
            else:
                raise ValueError("Either cidr_ip or destination_group_id must be provided")

            if description:
                rule_params['Description'] = description

            self.security_group.authorize_egress(
                GroupId=self.group_id,
                IpPermissions=[rule_params]
            )
            return True
        except botocore.exceptions.ClientError as e:
            if 'InvalidPermission.Duplicate' in str(e):
                # Rule already exists, not a failure
                logger.info(f"Rule already exists in security group {self.group_id}")
                return True
            logger.error(f"Failed to add egress rule to security group {self.group_id}: {e}")
            return False

    def revoke_ingress(self, ip_protocol: str, from_port: int, to_port: int,
                       cidr_ip: Optional[str] = None,
                       source_group_id: Optional[str] = None) -> bool:
        """
        Remove an inbound rule from the security group.

        Args:
            ip_protocol: IP protocol (tcp, udp, icmp)
            from_port: Start port
            to_port: End port
            cidr_ip: CIDR IP range
            source_group_id: Source security group ID

        Returns:
            True if successful, False otherwise
        """
        if not self.group_id or not self.exists:
            logger.warning("No valid security group to remove rule from")
            return False

        try:
            rule_params = {
                'IpProtocol': ip_protocol,
                'FromPort': from_port,
                'ToPort': to_port,
            }

            if cidr_ip:
                rule_params['CidrIp'] = cidr_ip
            elif source_group_id:
                rule_params['SourceSecurityGroupId'] = source_group_id
            else:
                raise ValueError("Either cidr_ip or source_group_id must be provided")

            self.security_group.revoke_ingress(
                GroupId=self.group_id,
                IpPermissions=[rule_params]
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to remove ingress rule from security group {self.group_id}: {e}")
            return False

    def revoke_egress(self, ip_protocol: str, from_port: int, to_port: int,
                      cidr_ip: Optional[str] = None,
                      destination_group_id: Optional[str] = None) -> bool:
        """
        Remove an outbound rule from the security group.

        Args:
            ip_protocol: IP protocol (tcp, udp, icmp)
            from_port: Start port
            to_port: End port
            cidr_ip: CIDR IP range
            destination_group_id: Destination security group ID

        Returns:
            True if successful, False otherwise
        """
        if not self.group_id or not self.exists:
            logger.warning("No valid security group to remove rule from")
            return False

        try:
            rule_params = {
                'IpProtocol': ip_protocol,
                'FromPort': from_port,
                'ToPort': to_port,
            }

            if cidr_ip:
                rule_params['CidrIp'] = cidr_ip
            elif destination_group_id:
                rule_params['DestinationSecurityGroupId'] = destination_group_id
            else:
                raise ValueError("Either cidr_ip or destination_group_id must be provided")

            self.security_group.revoke_egress(
                GroupId=self.group_id,
                IpPermissions=[rule_params]
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to remove egress rule from security group {self.group_id}: {e}")
            return False

    def get_rules(self) -> Dict[str, List]:
        """
        Get all rules for the security group.

        Returns:
            Dict with 'Ingress' and 'Egress' rule lists
        """
        if not self.group_id or not self.exists:
            logger.warning("No valid security group to get rules for")
            return {'Ingress': [], 'Egress': []}

        try:
            self.security_group.load()
            return {
                'Ingress': self.security_group.ip_permissions,
                'Egress': self.security_group.ip_permissions_egress
            }
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to get rules for security group {self.group_id}: {e}")
            return {'Ingress': [], 'Egress': []}

    @staticmethod
    def list_security_groups(filters: Optional[List[Dict[str, Any]]] = None) -> List[Dict]:
        """
        List security groups with optional filtering.

        Args:
            filters: Optional list of filters

        Returns:
            List of security group dictionaries
        """
        client = boto3.client('ec2',
                             aws_access_key_id=settings.AWS_KEY,
                             aws_secret_access_key=settings.AWS_SECRET,
                             region_name=getattr(settings, 'AWS_REGION', 'us-east-1'))

        try:
            if filters:
                response = client.describe_security_groups(Filters=filters)
            else:
                response = client.describe_security_groups()

            return response.get('SecurityGroups', [])
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to list security groups: {e}")
            return []


# Utility functions
def create_web_server_security_group(name: str, description: str = "Web server security group",
                                    vpc_id: Optional[str] = None) -> Optional[str]:
    """
    Create a security group with common web server rules (HTTP, HTTPS, SSH).

    Args:
        name: Security group name
        description: Security group description
        vpc_id: Optional VPC ID

    Returns:
        Security group ID if successful, None otherwise
    """
    sg = EC2SecurityGroup()

    if not sg.create(name, description, vpc_id):
        return None

    # Add common inbound rules
    sg.authorize_ingress('tcp', 80, 80, '0.0.0.0/0', description="HTTP")
    sg.authorize_ingress('tcp', 443, 443, '0.0.0.0/0', description="HTTPS")
    sg.authorize_ingress('tcp', 22, 22, '0.0.0.0/0', description="SSH")

    return sg.group_id


def launch_instance(ami_id: str, instance_type: str = 't2.micro',
                   key_name: Optional[str] = None,
                   security_group_ids: Optional[List[str]] = None,
                   name_tag: Optional[str] = None,
                   user_data: Optional[str] = None) -> Dict:
    """
    Launch an EC2 instance with common defaults.

    Args:
        ami_id: Amazon Machine Image ID
        instance_type: EC2 instance type
        key_name: SSH key pair name
        security_group_ids: List of security group IDs
        name_tag: Name tag for the instance
        user_data: Initialization script

    Returns:
        Dict with instance information
    """
    instance = EC2Instance()

    # Prepare tags if a name was provided
    tags = None
    if name_tag:
        tags = [{'Key': 'Name', 'Value': name_tag}]

    # Launch the instance
    result = instance.create(
        ami_id=ami_id,
        instance_type=instance_type,
        key_name=key_name,
        security_group_ids=security_group_ids,
        user_data=user_data,
        tags=tags,
        wait_until_running=True
    )

    return result


def get_instances_by_state(state: str = 'running') -> List[Dict]:
    """
    Get instances filtered by state.

    Args:
        state: Instance state (e.g., 'running', 'stopped')

    Returns:
        List of instance dictionaries
    """
    filters = [
        {
            'Name': 'instance-state-name',
            'Values': [state]
        }
    ]

    return EC2Instance.list_instances(filters)


# ── module-level: the bounded seam for Admin capacity actions ───────────────
#
# See the module docstring for why these are not methods on EC2Instance.
# No type hints below, per repo convention for framework code.

EC2_DEFAULT_TIMEOUT = 10
MAX_IMAGES = 50
FLEET_IMAGE_TAG = "mojo:fleet-image"
CREATED_BY_TAG = "mojo:created-by"
# IMDSv2 only. A cloned node inherits the source AMI's whole filesystem, so an
# instance-metadata endpoint reachable without a token is an SSRF away from the
# instance role's credentials.
IMDS_V2_ONLY = {
    "HttpTokens": "required",
    "HttpEndpoint": "enabled",
    "HttpPutResponseHopLimit": 1,
}

_caller = ProviderCaller(logger)


def _setting(name, default=None):
    try:
        return settings.get_static(name, default)
    except Exception:
        return default


def _ec2(client=None, region=None, timeout=EC2_DEFAULT_TIMEOUT):
    """The injection seam. Tests and callers holding a session pass ``client``."""
    if client is not None:
        return client
    region = region or _setting("AWS_REGION", "us-east-1")
    session = get_session(_setting("AWS_KEY"), _setting("AWS_SECRET"), region)
    # One attempt: a retried RunInstances is a second live node.
    return get_client("ec2", session=session, region=region,
                      timeout=timeout, max_attempts=1)


def _tag_map(row):
    return {tag.get("Key"): tag.get("Value") for tag in row.get("Tags") or []
            if tag.get("Key")}


def _tag_list(tags):
    return [{"Key": key, "Value": str(value)}
            for key, value in sorted((tags or {}).items())]


def _facts(row):
    """One describe_instances row, projected to what a clone needs to copy."""
    profile = (row.get("IamInstanceProfile") or {}).get("Arn")
    placement = row.get("Placement") or {}
    tags = _tag_map(row)
    private_dns = row.get("PrivateDnsName") or ""
    return {
        "instance_id": row.get("InstanceId"),
        "state": str(((row.get("State") or {}).get("Name")) or "").lower(),
        "instance_type": row.get("InstanceType"),
        "image_id": row.get("ImageId"),
        "subnet_id": row.get("SubnetId"),
        "vpc_id": row.get("VpcId"),
        "availability_zone": placement.get("AvailabilityZone"),
        "private_ip": row.get("PrivateIpAddress"),
        "public_ip": row.get("PublicIpAddress"),
        "private_dns_name": private_dns,
        # The first label is what `hostname -s` reports on the box, which is
        # the only thing a self-removal check can compare against.
        "private_hostname": private_dns.split(".", 1)[0].lower(),
        "security_group_ids": [group.get("GroupId")
                               for group in row.get("SecurityGroups") or []
                               if group.get("GroupId")],
        "iam_instance_profile_arn": profile,
        "key_name": row.get("KeyName"),
        "tags": tags,
        "name": tags.get("Name") or row.get("InstanceId"),
    }


def _describe(ec2, filters, iam_action="ec2:DescribeInstances"):
    page = _caller.call(
        "ec2.describe_instances",
        lambda: ec2.describe_instances(Filters=filters),
        iam_action=iam_action, mutation=False)
    rows = []
    for reservation in page.get("Reservations") or []:
        for row in reservation.get("Instances") or []:
            rows.append(_facts(row))
    return rows


def instance_facts(instance_id, client=None, region=None):
    """Facts for one instance, or None.

    A FILTERED describe, never ``InstanceIds=``: AWS raises
    ``InvalidInstanceID.NotFound`` for an id it has reaped, and "this instance
    is gone" must be a None, not an exception.
    """
    ec2 = _ec2(client, region)
    rows = _describe(ec2, [{"Name": "instance-id", "Values": [str(instance_id)]}])
    return rows[0] if rows else None


def instance_map(instance_ids, client=None, region=None):
    """``{instance_id: facts}`` for many instances, in ONE describe."""
    ids = [str(value) for value in (instance_ids or []) if str(value).startswith("i-")]
    if not ids:
        return {}
    ec2 = _ec2(client, region)
    rows = _describe(ec2, [{"Name": "instance-id", "Values": ids[:100]}])
    return {row["instance_id"]: row for row in rows if row.get("instance_id")}


def fleet_instance_map(project, environment, client=None, region=None):
    """Every live EC2 node owned by one django-mojo project/environment.

    Discovery is tag-scoped at the provider boundary.  A similarly named or
    generally django-mojo-tagged instance from another environment must never
    appear in this fleet's capacity controls.
    """
    project = str(project or "").strip()
    environment = str(environment or "").strip()
    if not project or not environment:
        return {}
    ec2 = _ec2(client, region)
    rows = _describe(ec2, [
        {"Name": "tag:mojo:project", "Values": [project]},
        {"Name": "tag:mojo:env", "Values": [environment]},
        {"Name": "tag:mojo:role", "Values": ["node"]},
        {"Name": "instance-state-name", "Values": [
            "pending", "running", "stopping", "stopped", "shutting-down",
        ]},
    ])
    return {row["instance_id"]: row for row in rows if row.get("instance_id")}


def running_instance(instance_ids, client=None, region=None):
    """Facts for the first id IN THE CALLER'S ORDER that AWS reports running.

    The order is the caller's preference (the capacity service puts
    non-primary nodes first), and AWS does not preserve request order in
    ``Reservations``, so the pick happens here rather than off the response.
    """
    ids = [str(value) for value in (instance_ids or []) if str(value).startswith("i-")]
    if not ids:
        return None
    ec2 = _ec2(client, region)
    rows = _describe(ec2, [
        {"Name": "instance-id", "Values": ids[:100]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ])
    found = {row["instance_id"]: row for row in rows if row.get("instance_id")}
    for identifier in ids:
        if identifier in found:
            return found[identifier]
    return None


def capture_image(instance_id, name, tag_value, description="", client=None, region=None):
    """Snapshot one running instance WITHOUT rebooting it. Returns the image id.

    ``NoReboot=True`` is not an optimization — the source is a node that is
    serving production traffic, and rebooting it to take a picture of it would
    make an add-capacity action an outage.
    """
    ec2 = _ec2(client, region)
    tags = _tag_list({FLEET_IMAGE_TAG: tag_value, "Name": name})
    page = _caller.call(
        "ec2.create_image",
        lambda: ec2.create_image(
            InstanceId=instance_id, Name=name, NoReboot=True,
            Description=description or f"admin capacity clone source {instance_id}",
            TagSpecifications=[{"ResourceType": "image", "Tags": tags}]),
        iam_action="ec2:CreateImage", mutation=True)
    return page.get("ImageId")


def image_status(image_id, client=None, region=None):
    """``{image_id, state, created}`` for one image, or None."""
    ec2 = _ec2(client, region)
    page = _caller.call(
        "ec2.describe_images",
        lambda: ec2.describe_images(
            Owners=["self"],
            Filters=[{"Name": "image-id", "Values": [str(image_id)]}]),
        iam_action="ec2:DescribeImages", mutation=False)
    for row in (page.get("Images") or [])[:1]:
        return {"image_id": row.get("ImageId"),
                "state": str(row.get("State") or "").lower(),
                "created": row.get("CreationDate")}
    return None


def _image_age_days(value):
    """Age in days of an AWS CreationDate string, or None when unparseable."""
    from datetime import datetime, timezone
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0


def find_reusable_image(tag_value, max_age_days, client=None, region=None):
    """The newest available fleet image younger than ``max_age_days``, or None.

    Capturing an AMI takes minutes and costs storage forever. A picture of the
    same fleet taken this week is the same picture — but only for a bounded
    while, because the node it was taken from keeps moving.
    """
    ec2 = _ec2(client, region)
    page = _caller.call(
        "ec2.describe_images",
        lambda: ec2.describe_images(
            Owners=["self"],
            Filters=[{"Name": f"tag:{FLEET_IMAGE_TAG}", "Values": [str(tag_value)]},
                     {"Name": "state", "Values": ["available"]}]),
        iam_action="ec2:DescribeImages", mutation=False)
    best = None
    for row in (page.get("Images") or [])[:MAX_IMAGES]:
        age = _image_age_days(row.get("CreationDate"))
        if age is None or age > float(max_age_days):
            continue
        if best is None or str(row.get("CreationDate")) > str(best.get("CreationDate")):
            best = row
    if best is None:
        return None
    return {"image_id": best.get("ImageId"),
            "state": str(best.get("State") or "").lower(),
            "created": best.get("CreationDate"),
            "age_days": round(_image_age_days(best.get("CreationDate")) or 0, 2)}


def launch_clone(source_facts, image_id, subnet_id, name, user_data, tags=None,
                 client=None, region=None):
    """Launch ONE clone of ``source_facts``. Returns the new instance id.

    Everything that decides where the node lands and what it may do is copied
    from the source instance rather than configured: instance type, subnet,
    security groups, and the instance profile. IMDSv2 is forced regardless of
    what the source had.
    """
    ec2 = _ec2(client, region)
    facts = source_facts or {}
    all_tags = {"Name": name, CREATED_BY_TAG: "admin-capacity"}
    all_tags.update(tags or {})
    params = {
        "ImageId": image_id,
        "InstanceType": facts.get("instance_type"),
        "MinCount": 1,
        "MaxCount": 1,
        "SubnetId": subnet_id or facts.get("subnet_id"),
        "SecurityGroupIds": list(facts.get("security_group_ids") or []),
        "UserData": user_data or "",
        "MetadataOptions": dict(IMDS_V2_ONLY),
        "TagSpecifications": [{"ResourceType": "instance",
                               "Tags": _tag_list(all_tags)}],
    }
    profile = facts.get("iam_instance_profile_arn")
    if profile:
        params["IamInstanceProfile"] = {"Arn": profile}
    page = _caller.call(
        "ec2.run_instances", lambda: ec2.run_instances(**params),
        iam_action="ec2:RunInstances", mutation=True)
    rows = page.get("Instances") or []
    return rows[0].get("InstanceId") if rows else None


def terminate(instance_id, client=None, region=None):
    """Terminate ONE instance. Returns the state AWS moved it to."""
    ec2 = _ec2(client, region)
    page = _caller.call(
        "ec2.terminate_instances",
        lambda: ec2.terminate_instances(InstanceIds=[str(instance_id)]),
        iam_action="ec2:TerminateInstances", mutation=True)
    for row in page.get("TerminatingInstances") or []:
        return str((row.get("CurrentState") or {}).get("Name") or "").lower()
    return ""


# ── module-level: Elastic IP addresses, for the stable-egress control ───────


def address_map(client=None, region=None):
    """Every Elastic IP in the region, projected to what the caller decides on.

    One describe, no filter: which addresses are OURS is a tag decision the
    capacity service makes, and filtering here would hide the foreign addresses
    the report must still label rather than pretend away.
    """
    ec2 = _ec2(client, region)
    page = _caller.call(
        "ec2.describe_addresses",
        lambda: ec2.describe_addresses(),
        iam_action="ec2:DescribeAddresses", mutation=False)
    rows = []
    for row in page.get("Addresses") or []:
        tags = _tag_map(row)
        rows.append({
            "allocation_id": row.get("AllocationId"),
            "association_id": row.get("AssociationId"),
            "public_ip": row.get("PublicIp"),
            "instance_id": row.get("InstanceId"),
            "network_interface_id": row.get("NetworkInterfaceId"),
            "tags": tags,
            "name": tags.get("Name"),
        })
    return rows


def allocate_address(tags, client=None, region=None):
    """Allocate ONE Elastic IP, tagged at creation. Returns its ids.

    Tagged in the allocate call itself, never in a second one: an allocation
    that succeeds followed by a tag call that does not would leave an address
    this feature can never recognise as its own — reserved, billing, and
    invisible to every later reuse pass.
    """
    ec2 = _ec2(client, region)
    page = _caller.call(
        "ec2.allocate_address",
        lambda: ec2.allocate_address(
            Domain="vpc",
            TagSpecifications=[{"ResourceType": "elastic-ip",
                                "Tags": _tag_list(tags)}]),
        iam_action="ec2:AllocateAddress", mutation=True)
    return {"allocation_id": page.get("AllocationId"),
            "public_ip": page.get("PublicIp")}


def associate_address(allocation_id, instance_id, client=None, region=None):
    """Attach one allocation to one instance. Returns the association id.

    ``AllowReassociation=False`` is explicit and load-bearing: an address that
    is already attached somewhere else must be an ERROR here, never silently
    ripped off another instance — losing a race is recoverable, stealing an
    address in production is an outage.
    """
    ec2 = _ec2(client, region)
    page = _caller.call(
        "ec2.associate_address",
        lambda: ec2.associate_address(
            AllocationId=str(allocation_id), InstanceId=str(instance_id),
            AllowReassociation=False),
        iam_action="ec2:AssociateAddress", mutation=True)
    return page.get("AssociationId")


def disassociate_address(association_id, client=None, region=None):
    """Detach one association. The allocation itself is deliberately kept —
    releasing a reserved address is a console decision, never a side effect."""
    ec2 = _ec2(client, region)
    _caller.call(
        "ec2.disassociate_address",
        lambda: ec2.disassociate_address(AssociationId=str(association_id)),
        iam_action="ec2:DisassociateAddress", mutation=True)
    return True


def tag_resources(resource_ids, tags, client=None, region=None):
    """Add/overwrite tags on existing resources (adoption tagging)."""
    ids = [str(value) for value in (resource_ids or []) if value]
    if not ids or not tags:
        return False
    ec2 = _ec2(client, region)
    _caller.call(
        "ec2.create_tags",
        lambda: ec2.create_tags(Resources=ids, Tags=_tag_list(tags)),
        iam_action="ec2:CreateTags", mutation=True)
    return True
