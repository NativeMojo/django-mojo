"""
AWS SNS Helper Module

Provides simple interfaces for managing AWS SNS (Simple Notification Service).
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


class SNSTopic:
    """
    Simple interface for managing SNS topics.
    """

    def __init__(self, name: str, access_key: Optional[str] = None,
                 secret_key: Optional[str] = None, region: Optional[str] = None):
        """
        Initialize a topic manager for the specified SNS topic.

        Args:
            name: The topic name
            access_key: AWS access key, defaults to settings.AWS_KEY
            secret_key: AWS secret key, defaults to settings.AWS_SECRET
            region: AWS region, defaults to settings.AWS_REGION if available
        """
        self.name = name
        self.access_key = access_key or settings.AWS_KEY
        self.secret_key = secret_key or settings.AWS_SECRET
        self.region = region or getattr(settings, 'AWS_REGION', 'us-east-1')

        session = get_session(self.access_key, self.secret_key, self.region)
        self.client = session.client('sns')
        self.arn = None
        self.exists = self._check_exists()

    def _check_exists(self) -> bool:
        """
        Check if the topic exists and set ARN if it does.

        Returns:
            True if the topic exists, False otherwise
        """
        try:
            # List topics and check if this one exists
            response = self.client.list_topics()

            for topic in response.get('Topics', []):
                topic_arn = topic['TopicArn']
                if topic_arn.split(':')[-1] == self.name:
                    self.arn = topic_arn
                    return True

            return False
        except botocore.exceptions.ClientError as e:
            logger.error(f"Error checking topic existence: {e}")
            return False

    def create(self, display_name: Optional[str] = None,
               tags: Optional[List[Dict[str, str]]] = None) -> bool:
        """
        Create an SNS topic.

        Args:
            display_name: Display name for the topic
            tags: Optional tags for the topic

        Returns:
            True if topic was created, False if it already exists
        """
        if self.exists:
            logger.info(f"Topic {self.name} already exists")
            return False

        try:
            # Prepare creation parameters
            create_params = {'Name': self.name}
            attributes = {}

            if display_name:
                attributes['DisplayName'] = display_name

            if attributes:
                create_params['Attributes'] = attributes

            if tags:
                create_params['Tags'] = tags

            # Create the topic
            response = self.client.create_topic(**create_params)
            self.arn = response['TopicArn']
            self.exists = True
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to create topic {self.name}: {e}")
            return False

    def delete(self) -> bool:
        """
        Delete the SNS topic.

        Returns:
            True if successfully deleted, False otherwise
        """
        if not self.exists:
            logger.info(f"Topic {self.name} does not exist")
            return False

        try:
            self.client.delete_topic(TopicArn=self.arn)
            self.arn = None
            self.exists = False
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to delete topic {self.name}: {e}")
            return False

    def publish(self, message: str, subject: Optional[str] = None,
                attributes: Optional[Dict[str, Dict]] = None,
                message_structure: Optional[str] = None) -> Dict:
        """
        Publish a message to the topic.

        Args:
            message: Message to publish
            subject: Optional subject for the message
            attributes: Optional message attributes
            message_structure: Optional message structure (e.g., 'json')

        Returns:
            Response dict containing MessageId if successful
        """
        if not self.exists:
            logger.warning(f"Topic {self.name} does not exist")
            return {'Error': 'Topic does not exist'}

        try:
            # Prepare publish parameters
            publish_params = {
                'TopicArn': self.arn,
                'Message': message
            }

            if subject:
                publish_params['Subject'] = subject

            if attributes:
                publish_params['MessageAttributes'] = attributes

            if message_structure:
                publish_params['MessageStructure'] = message_structure

            # Publish the message
            response = self.client.publish(**publish_params)
            logger.info(f"Message published successfully with MessageId: {response['MessageId']}")
            return response
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to publish message to topic {self.name}: {e}")
            return {'Error': str(e)}

    def set_attributes(self, attributes: Dict[str, str]) -> bool:
        """
        Set topic attributes.

        Args:
            attributes: Dictionary of attribute name-value pairs

        Returns:
            True if successful, False otherwise
        """
        if not self.exists:
            logger.warning(f"Topic {self.name} does not exist")
            return False

        try:
            self.client.set_topic_attributes(
                TopicArn=self.arn,
                AttributeName=list(attributes.keys())[0],  # Can only set one at a time
                AttributeValue=list(attributes.values())[0]
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to set attributes for topic {self.name}: {e}")
            return False

    def get_attributes(self) -> Dict[str, str]:
        """
        Get topic attributes.

        Returns:
            Dictionary of topic attributes
        """
        if not self.exists:
            logger.warning(f"Topic {self.name} does not exist")
            return {}

        try:
            response = self.client.get_topic_attributes(TopicArn=self.arn)
            return response.get('Attributes', {})
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to get attributes for topic {self.name}: {e}")
            return {}

    def list_subscriptions(self) -> List[Dict]:
        """
        List all subscriptions to this topic.

        Returns:
            List of subscription dictionaries
        """
        if not self.exists:
            logger.warning(f"Topic {self.name} does not exist")
            return []

        try:
            response = self.client.list_subscriptions_by_topic(TopicArn=self.arn)
            return response.get('Subscriptions', [])
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to list subscriptions for topic {self.name}: {e}")
            return []

    @staticmethod
    def list_all_topics() -> List[Dict]:
        """
        List all SNS topics.

        Returns:
            List of topic dictionaries
        """
        client = boto3.client('sns',
                             aws_access_key_id=settings.AWS_KEY,
                             aws_secret_access_key=settings.AWS_SECRET,
                             region_name=getattr(settings, 'AWS_REGION', 'us-east-1'))

        try:
            response = client.list_topics()
            return response.get('Topics', [])
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to list topics: {e}")
            return []


class SNSSubscription:
    """
    Simple interface for managing SNS subscriptions.
    """

    def __init__(self, topic_arn: str, access_key: Optional[str] = None,
                 secret_key: Optional[str] = None, region: Optional[str] = None):
        """
        Initialize a subscription manager for the specified SNS topic.

        Args:
            topic_arn: The ARN of the topic
            access_key: AWS access key, defaults to settings.AWS_KEY
            secret_key: AWS secret key, defaults to settings.AWS_SECRET
            region: AWS region, defaults to settings.AWS_REGION if available
        """
        self.topic_arn = topic_arn
        self.access_key = access_key or settings.AWS_KEY
        self.secret_key = secret_key or settings.AWS_SECRET
        self.region = region or getattr(settings, 'AWS_REGION', 'us-east-1')

        session = get_session(self.access_key, self.secret_key, self.region)
        self.client = session.client('sns')

    def subscribe(self, protocol: str, endpoint: str,
                  attributes: Optional[Dict[str, str]] = None,
                  return_subscription_arn: bool = False) -> Dict:
        """
        Subscribe an endpoint to the topic.

        Args:
            protocol: Protocol to use (http, https, email, sms, sqs, application, lambda)
            endpoint: Endpoint to subscribe
            attributes: Optional subscription attributes
            return_subscription_arn: Whether to return the subscription ARN

        Returns:
            Response dict containing SubscriptionArn
        """
        try:
            # Prepare subscription parameters
            subscribe_params = {
                'TopicArn': self.topic_arn,
                'Protocol': protocol,
                'Endpoint': endpoint,
                'ReturnSubscriptionArn': return_subscription_arn
            }

            if attributes:
                subscribe_params['Attributes'] = attributes

            # Create the subscription
            response = self.client.subscribe(**subscribe_params)
            logger.info(f"Subscription created: {response.get('SubscriptionArn')}")
            return response
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to create subscription: {e}")
            return {'Error': str(e)}

    def unsubscribe(self, subscription_arn: str) -> bool:
        """
        Unsubscribe from the topic.

        Args:
            subscription_arn: ARN of the subscription to delete

        Returns:
            True if successful, False otherwise
        """
        try:
            self.client.unsubscribe(SubscriptionArn=subscription_arn)
            logger.info(f"Subscription {subscription_arn} deleted")
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to delete subscription {subscription_arn}: {e}")
            return False

    def set_attributes(self, subscription_arn: str,
                       attribute_name: str, attribute_value: str) -> bool:
        """
        Set subscription attributes.

        Args:
            subscription_arn: ARN of the subscription
            attribute_name: Name of the attribute to set
            attribute_value: Value of the attribute

        Returns:
            True if successful, False otherwise
        """
        try:
            self.client.set_subscription_attributes(
                SubscriptionArn=subscription_arn,
                AttributeName=attribute_name,
                AttributeValue=attribute_value
            )
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to set attributes for subscription {subscription_arn}: {e}")
            return False

    def get_attributes(self, subscription_arn: str) -> Dict[str, str]:
        """
        Get subscription attributes.

        Args:
            subscription_arn: ARN of the subscription

        Returns:
            Dictionary of subscription attributes
        """
        try:
            response = self.client.get_subscription_attributes(
                SubscriptionArn=subscription_arn
            )
            return response.get('Attributes', {})
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to get attributes for subscription {subscription_arn}: {e}")
            return {}

    @staticmethod
    def list_all_subscriptions() -> List[Dict]:
        """
        List all SNS subscriptions.

        Returns:
            List of subscription dictionaries
        """
        client = boto3.client('sns',
                             aws_access_key_id=settings.AWS_KEY,
                             aws_secret_access_key=settings.AWS_SECRET,
                             region_name=getattr(settings, 'AWS_REGION', 'us-east-1'))

        try:
            response = client.list_subscriptions()
            return response.get('Subscriptions', [])
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to list subscriptions: {e}")
            return []


# Utility functions
def create_topic_and_subscribe(topic_name: str, protocol: str, endpoint: str,
                              display_name: Optional[str] = None) -> Dict:
    """
    Create a topic and subscribe an endpoint in one operation.

    Args:
        topic_name: Name of the topic to create
        protocol: Protocol to use for subscription
        endpoint: Endpoint to subscribe
        display_name: Optional display name for the topic

    Returns:
        Dict with topic ARN and subscription ARN
    """
    result = {}

    # Create the topic
    topic = SNSTopic(topic_name)
    if not topic.exists:
        if not topic.create(display_name=display_name):
            return {'Error': f"Failed to create topic {topic_name}"}

    result['TopicArn'] = topic.arn

    # Create the subscription
    subscription = SNSSubscription(topic.arn)
    response = subscription.subscribe(protocol, endpoint, return_subscription_arn=True)

    if 'Error' in response:
        result['Error'] = response['Error']
    else:
        result['SubscriptionArn'] = response.get('SubscriptionArn')

    return result


def publish_message(topic_name: str, message: str, subject: Optional[str] = None) -> Dict:
    """
    Publish a message to a topic by name.

    Args:
        topic_name: Name of the topic
        message: Message to publish
        subject: Optional message subject

    Returns:
        Response dict containing MessageId if successful
    """
    topic = SNSTopic(topic_name)

    if not topic.exists:
        return {'Error': f"Topic {topic_name} does not exist"}

    return topic.publish(message, subject)


def get_topic_arn(topic_name: str) -> Optional[str]:
    """
    Get the ARN for a topic by name.

    Args:
        topic_name: Name of the topic

    Returns:
        Topic ARN if found, None otherwise
    """
    topic = SNSTopic(topic_name)
    return topic.arn if topic.exists else None
