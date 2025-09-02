"""
AWS Helpers Module

A simple interface for working with AWS services.
"""

# Import service modules - these will be implemented
from .s3 import S3Bucket, S3Item
from .client import get_session
from .kms import KMSHelper

# These will be implemented in future modules
from .iam import IAMRole, IAMPolicy, IAMUser
from .ses import EmailSender, EmailTemplate
from .sns import SNSTopic, SNSSubscription
from .ec2 import EC2Instance, EC2SecurityGroup

__all__ = [
    # Base
    'get_session',

    # S3
    'S3Bucket',
    'S3Item',

    # KMS
    'KMSHelper',

    # IAM
    'IAMRole',
    'IAMPolicy',
    'IAMUser',

    # SES
    'EmailSender',
    'EmailTemplate',

    # SNS
    'SNSTopic',
    'SNSSubscription',

    # EC2
    'EC2Instance',
    'EC2SecurityGroup',
]
