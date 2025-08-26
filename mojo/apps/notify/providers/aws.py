from mojo.helpers.settings import settings
from mojo.helpers.logit import get_logger
import metrics
import boto3
from . import render as mr

# Initialize logger for email notifications
EMAIL_LOGGER = get_logger("email", filename="email.log")

# Retrieve SES settings from configuration
SES_ACCESS_KEY = settings.SES_ACCESS_KEY
SES_SECRET_KEY = settings.SES_SECRET_KEY
SES_REGION = settings.SES_REGION
EMAIL_METRICS = settings.EMAIL_METRICS
EMAIL_ASYNC_AS_TASK = settings.EMAIL_ASYNC_AS_TASK

def get_ses_client(access_key, secret_key, region):
    """Create a new SES client with the provided credentials."""
    return boto3.client('ses',
                        aws_access_key_id=access_key,
                        aws_secret_access_key=secret_key,
                        region_name=region)

def send_mail_via_ses(msg, sender, recipients, fail_silently=True):
    """Send an email using Amazon SES service."""
    try:
        ses_client = get_ses_client(SES_ACCESS_KEY, SES_SECRET_KEY, SES_REGION)
        ses_client.send_raw_email(
            Source=sender,
            Destinations=recipients,
            RawMessage={'Data': msg.as_string()}
        )
        if EMAIL_METRICS:
            metrics.record("emails_sent", category="email", min_granularity="hours")
        return True
    except Exception as err:
        if EMAIL_METRICS:
            metrics.record("email_errors", category="email", min_granularity="hours")
        EMAIL_LOGGER.exception(err)
        EMAIL_LOGGER.error(msg.as_string())
        if not fail_silently:
            raise err
    return False

def send_mail(msg, sender, recipients, fail_silently=True):
    """Send an email via SES, defaulting to fail silently."""
    return send_mail_via_ses(msg, sender, recipients, fail_silently)

def send(sender, recipients, subject, message, attachments=None, replyto=None, fail_silently=False, do_async=False):
    """
    Prepare and send an email message.

    :param sender: Email address of the sender.
    :param recipients: List of recipient email addresses.
    :param subject: Subject of the email.
    :param message: Body of the email.
    :param attachments: List of attachments.
    :param replyto: Email address to reply to.
    :param fail_silently: Flag to suppress exceptions.
    :param do_async: Flag to send email asynchronously (not implemented).
    """
    html = None
    text = None

    if mr.isHTML(message):
        html = message
    else:
        text = message

    msg = mr.createMessage(sender, recipients, subject, text, html,
                           attachments=attachments, replyto=replyto)

    return send_mail(msg.msg, msg.sender, msg.recipients, fail_silently=fail_silently)
