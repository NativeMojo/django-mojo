"""
AWS SES Helper Module

Provides simple interfaces for managing AWS SES (Simple Email Service).
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


class EmailSender:
    """
    Simple interface for sending emails using AWS SES.
    """

    def __init__(self, access_key: Optional[str] = None, secret_key: Optional[str] = None,
                region: Optional[str] = None):
        """
        Initialize SES client with credentials.

        Args:
            access_key: AWS access key, defaults to settings.AWS_KEY
            secret_key: AWS secret key, defaults to settings.AWS_SECRET
            region: AWS region, defaults to settings.AWS_REGION if available
        """
        self.access_key = access_key or settings.AWS_KEY
        self.secret_key = secret_key or settings.AWS_SECRET
        self.region = region or getattr(settings, 'AWS_REGION', 'us-east-1')

        session = get_session(self.access_key, self.secret_key, self.region)
        self.client = session.client('ses')

    def send_email(self,
                   source: str,
                   to_addresses: List[str],
                   subject: str,
                   body_text: Optional[str] = None,
                   body_html: Optional[str] = None,
                   cc_addresses: Optional[List[str]] = None,
                   bcc_addresses: Optional[List[str]] = None,
                   reply_to_addresses: Optional[List[str]] = None) -> Dict:
        """
        Send an email using Amazon SES.

        Args:
            source: Email sender address
            to_addresses: List of recipient email addresses
            subject: Email subject
            body_text: Plain text email body
            body_html: HTML email body
            cc_addresses: List of CC email addresses
            bcc_addresses: List of BCC email addresses
            reply_to_addresses: List of reply-to email addresses

        Returns:
            Dict containing 'MessageId' if successful
        """
        if not body_text and not body_html:
            raise ValueError("At least one of body_text or body_html must be provided")

        message = {
            'Subject': {'Data': subject}
        }

        # Add body content
        body = {}
        if body_text:
            body['Text'] = {'Data': body_text}
        if body_html:
            body['Html'] = {'Data': body_html}
        message['Body'] = body

        # Configure recipients
        destination = {'ToAddresses': to_addresses}
        if cc_addresses:
            destination['CcAddresses'] = cc_addresses
        if bcc_addresses:
            destination['BccAddresses'] = bcc_addresses

        # Prepare send parameters
        send_params = {
            'Source': source,
            'Destination': destination,
            'Message': message
        }

        if reply_to_addresses:
            send_params['ReplyToAddresses'] = reply_to_addresses

        try:
            response = self.client.send_email(**send_params)
            logger.info(f"Email sent successfully with MessageId: {response['MessageId']}")
            return response
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to send email: {e}")
            return {'Error': str(e)}

    def send_template_email(self,
                           source: str,
                           to_addresses: List[str],
                           template_name: str,
                           template_data: Dict,
                           cc_addresses: Optional[List[str]] = None,
                           bcc_addresses: Optional[List[str]] = None,
                           reply_to_addresses: Optional[List[str]] = None) -> Dict:
        """
        Send an email using an SES template.

        Args:
            source: Email sender address
            to_addresses: List of recipient email addresses
            template_name: Name of the SES template to use
            template_data: Dictionary of template data
            cc_addresses: List of CC email addresses
            bcc_addresses: List of BCC email addresses
            reply_to_addresses: List of reply-to email addresses

        Returns:
            Dict containing 'MessageId' if successful
        """
        # Configure recipients
        destination = {'ToAddresses': to_addresses}
        if cc_addresses:
            destination['CcAddresses'] = cc_addresses
        if bcc_addresses:
            destination['BccAddresses'] = bcc_addresses

        # Prepare send parameters
        send_params = {
            'Source': source,
            'Destination': destination,
            'Template': template_name,
            'TemplateData': json.dumps(template_data)
        }

        if reply_to_addresses:
            send_params['ReplyToAddresses'] = reply_to_addresses

        try:
            response = self.client.send_templated_email(**send_params)
            logger.info(f"Template email sent successfully with MessageId: {response['MessageId']}")
            return response
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to send template email: {e}")
            return {'Error': str(e)}

    def send_raw_email(self, raw_message: str, source: Optional[str] = None) -> Dict:
        """
        Send a raw email (MIME format).

        Args:
            raw_message: The raw text of the message
            source: Email sender address (optional, can be specified in raw_message)

        Returns:
            Dict containing 'MessageId' if successful
        """
        send_params = {
            'RawMessage': {'Data': raw_message}
        }

        if source:
            send_params['Source'] = source

        try:
            response = self.client.send_raw_email(**send_params)
            logger.info(f"Raw email sent successfully with MessageId: {response['MessageId']}")
            return response
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to send raw email: {e}")
            return {'Error': str(e)}

    def get_send_quota(self) -> Dict:
        """
        Get SES sending limits and usage.

        Returns:
            Dict with quota information
        """
        try:
            return self.client.get_send_quota()
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to get send quota: {e}")
            return {'Error': str(e)}

    def verify_email_identity(self, email: str) -> bool:
        """
        Verify an email address identity.

        Args:
            email: Email address to verify

        Returns:
            True if verification process started successfully
        """
        try:
            self.client.verify_email_identity(EmailAddress=email)
            logger.info(f"Verification email sent to {email}")
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to initiate verification for {email}: {e}")
            return False

    def verify_domain_identity(self, domain: str) -> Dict:
        """
        Verify a domain identity.

        Args:
            domain: Domain name to verify

        Returns:
            Dict with verification token if successful
        """
        try:
            response = self.client.verify_domain_identity(Domain=domain)
            logger.info(f"Domain verification initiated for {domain}")
            return response
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to initiate domain verification for {domain}: {e}")
            return {'Error': str(e)}

    def list_identities(self, identity_type: Optional[str] = None) -> List[str]:
        """
        List verified email addresses and domains.

        Args:
            identity_type: Filter by type ('EmailAddress' or 'Domain')

        Returns:
            List of identity strings
        """
        try:
            params = {}
            if identity_type:
                params['IdentityType'] = identity_type

            response = self.client.list_identities(**params)
            return response.get('Identities', [])
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to list identities: {e}")
            return []


class EmailTemplate:
    """
    Simple interface for managing SES email templates.
    """

    def __init__(self, name: str, access_key: Optional[str] = None,
                 secret_key: Optional[str] = None, region: Optional[str] = None):
        """
        Initialize a template manager for the specified SES template.

        Args:
            name: The template name
            access_key: AWS access key, defaults to settings.AWS_KEY
            secret_key: AWS secret key, defaults to settings.AWS_SECRET
            region: AWS region, defaults to settings.AWS_REGION if available
        """
        self.name = name
        self.access_key = access_key or settings.AWS_KEY
        self.secret_key = secret_key or settings.AWS_SECRET
        self.region = region or getattr(settings, 'AWS_REGION', 'us-east-1')

        session = get_session(self.access_key, self.secret_key, self.region)
        self.client = session.client('ses')
        self.exists = self._check_exists()

    def _check_exists(self) -> bool:
        """Check if the template exists."""
        try:
            self.client.get_template(TemplateName=self.name)
            return True
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'TemplateDoesNotExist':
                return False
            logger.error(f"Error checking template existence: {e}")
            raise

    def create(self, subject: str, html_content: str,
               text_content: Optional[str] = None) -> bool:
        """
        Create an email template.

        Args:
            subject: Template subject
            html_content: HTML template content
            text_content: Plain text template content

        Returns:
            True if template was created, False if it already exists
        """
        if self.exists:
            logger.info(f"Template {self.name} already exists")
            return False

        try:
            template = {
                'TemplateName': self.name,
                'SubjectPart': subject,
                'HtmlPart': html_content,
            }

            if text_content:
                template['TextPart'] = text_content

            self.client.create_template(Template=template)
            self.exists = True
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to create template {self.name}: {e}")
            return False

    def update(self, subject: Optional[str] = None,
               html_content: Optional[str] = None,
               text_content: Optional[str] = None) -> bool:
        """
        Update an existing email template.

        Args:
            subject: New template subject
            html_content: New HTML template content
            text_content: New plain text template content

        Returns:
            True if successful, False otherwise
        """
        if not self.exists:
            logger.warning(f"Template {self.name} does not exist")
            return False

        try:
            # Get current template
            current = self.client.get_template(TemplateName=self.name)['Template']

            # Update only the specified parts
            template = {
                'TemplateName': self.name,
                'SubjectPart': subject or current.get('SubjectPart', ''),
                'HtmlPart': html_content or current.get('HtmlPart', ''),
            }

            if text_content or 'TextPart' in current:
                template['TextPart'] = text_content or current.get('TextPart', '')

            self.client.update_template(Template=template)
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to update template {self.name}: {e}")
            return False

    def delete(self) -> bool:
        """
        Delete the email template.

        Returns:
            True if successfully deleted, False otherwise
        """
        if not self.exists:
            logger.info(f"Template {self.name} does not exist")
            return False

        try:
            self.client.delete_template(TemplateName=self.name)
            self.exists = False
            return True
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to delete template {self.name}: {e}")
            return False

    def get(self) -> Dict:
        """
        Get the email template details.

        Returns:
            Template details dictionary
        """
        if not self.exists:
            logger.warning(f"Template {self.name} does not exist")
            return {}

        try:
            response = self.client.get_template(TemplateName=self.name)
            return response.get('Template', {})
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to get template {self.name}: {e}")
            return {}

    @staticmethod
    def list_all_templates() -> List[Dict]:
        """
        List all email templates.

        Returns:
            List of template information dictionaries
        """
        client = boto3.client('ses',
                             aws_access_key_id=settings.AWS_KEY,
                             aws_secret_access_key=settings.AWS_SECRET,
                             region_name=getattr(settings, 'AWS_REGION', 'us-east-1'))

        try:
            response = client.list_templates()
            return response.get('TemplatesMetadata', [])
        except botocore.exceptions.ClientError as e:
            logger.error(f"Failed to list templates: {e}")
            return []


# Utility functions
def send_simple_email(from_email: str, to_email: str, subject: str,
                      message: str, html_message: Optional[str] = None) -> Dict:
    """
    Convenience function to send a simple email.

    Args:
        from_email: Sender email address
        to_email: Recipient email address
        subject: Email subject
        message: Plain text email body
        html_message: Optional HTML email body

    Returns:
        Response dictionary
    """
    sender = EmailSender()
    return sender.send_email(
        source=from_email,
        to_addresses=[to_email],
        subject=subject,
        body_text=message,
        body_html=html_message
    )


def verify_sender(email: str) -> bool:
    """
    Verify an email address for sending.

    Args:
        email: Email address to verify

    Returns:
        True if verification process started successfully
    """
    sender = EmailSender()
    return sender.verify_email_identity(email)


def is_identity_verified(identity: str) -> bool:
    """
    Check if an identity is verified.

    Args:
        identity: Email address or domain to check

    Returns:
        True if verified, False otherwise
    """
    client = boto3.client('ses',
                         aws_access_key_id=settings.AWS_KEY,
                         aws_secret_access_key=settings.AWS_SECRET,
                         region_name=getattr(settings, 'AWS_REGION', 'us-east-1'))

    try:
        response = client.get_identity_verification_attributes(
            Identities=[identity]
        )
        attributes = response.get('VerificationAttributes', {}).get(identity, {})
        status = attributes.get('VerificationStatus', '')
        return status.lower() == 'success'
    except botocore.exceptions.ClientError as e:
        logger.error(f"Failed to check verification status for {identity}: {e}")
        return False
