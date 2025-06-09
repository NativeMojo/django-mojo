"""
AWS EC2 Helper Module

Provides simple interfaces for managing AWS EC2 (Elastic Compute Cloud) resources.
"""

import logging
import time
import boto3
import botocore
from typing import Dict, List, Optional, Union, Any, Tuple

from .client import get_session
from mojo.helpers.settings import settings
from mojo.helpers import logit

logger = logit.get_logger(__name__)


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
