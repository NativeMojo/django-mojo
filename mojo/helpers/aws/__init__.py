"""
AWS Helpers Module

A simple interface for working with AWS services.
"""

# Import service modules - these will be implemented
from .s3 import S3Bucket, S3Item
from .client import get_session, get_assumed_session
from .kms import KMSHelper

# These will be implemented in future modules
from .iam import IAMRole, IAMPolicy, IAMUser
from .ses import EmailSender, EmailTemplate
from .sns import SNSTopic, SNSSubscription
from .ec2 import EC2Instance, EC2SecurityGroup
from .cloudwatch import CloudWatchHelper
from .elbv2 import LoadBalancerHelper
# Module-level function namespaces, not classes: these carry no per-caller
# state beyond the client seam each function already accepts.
from . import elasticache, rds

__all__ = [
    # Base
    'get_session',
    'get_assumed_session',

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

    # CloudWatch
    'CloudWatchHelper',

    # ELBv2
    'LoadBalancerHelper',

    # Managed-service engine versions (read + the upgrade mutations)
    'rds',
    'elasticache',
]
