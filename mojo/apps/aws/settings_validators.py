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


def stable_outbound_ips(key, value):
    """The stable-egress policy: exactly {"enabled": bool}, nothing else.

    Strict on purpose — this row is an admission gate for add_node, so a shape
    nobody validated must be refused at write time, not tolerated at read time.
    """
    if not isinstance(value, dict):
        raise ValueError(f'{key} must be a JSON object like {{"enabled": true}}')
    if set(value.keys()) != {"enabled"}:
        raise ValueError(f"{key} accepts exactly one key: enabled")
    if not isinstance(value.get("enabled"), bool):
        raise ValueError(f"{key} enabled must be true or false")
    return {"enabled": value["enabled"]}
