"""AWS-specific protected-setting validators."""

import re


_TOPIC_ARN = re.compile(r"^arn:(aws|aws-us-gov|aws-cn):sns:[a-z0-9-]+:\d{12}:[A-Za-z0-9_.-]{1,256}$")


def monitoring_topic_arns(key, value):
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a JSON list of SNS topic ARNs")
    clean = []
    for item in value:
        item = str(item or "").strip()
        if not _TOPIC_ARN.fullmatch(item):
            raise ValueError(f"{key} contains an invalid SNS topic ARN")
        if item not in clean:
            clean.append(item)
    return clean
