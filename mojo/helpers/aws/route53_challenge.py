"""Dedicated single-send ACME client and raw simple-TXT conditional batches.

General DNS CRUD keeps its existing contract. These helpers never flatten a
DELETE identity, normalize its TXT bytes, UPSERT survivors, or retry mutation.
"""
import copy
import re

import dns.rdata
import dns.rdataclass
import dns.rdatatype
from botocore.config import Config
from botocore.exceptions import ClientError

from . import route53

MAX_VALUES = 256
MAX_BYTES = 65536
_TXT = re.compile(r'(?:"(?:[^"\\\r\n]|\\[0-9]{3}|\\[\\"])*"[ \t]*)+\Z')
_OWNED = re.compile(r"[A-Za-z0-9_-]{1,512}\Z")
_ID = re.compile(r"(?:/change/)?[A-Za-z0-9_-]{1,240}\Z")


class ChallengeError(ValueError):
    def __init__(self, code, no_change=False):
        self.error_code = code
        self.no_change = no_change
        super().__init__(code)


def mutation_client(session=None):
    if session is None:
        session = route53.get_session(route53.settings.get("AWS_KEY", None),
                                      route53.settings.get("AWS_SECRET", None),
                                      route53.settings.get("AWS_REGION", route53.DOMAINS_REGION))
    return session.client("route53", config=Config(
        connect_timeout=3, read_timeout=5,
        retries={"total_max_attempts": 1, "mode": "standard"},
        ignore_configured_endpoint_urls=True))


def validate_client(client):
    try:
        config = client.meta.config
        retries = config.retries
        valid = (retries.get("total_max_attempts") == 1 and retries.get("mode") == "standard"
                 and 0 < config.connect_timeout <= 3 and 0 < config.read_timeout <= 5)
    except (AttributeError, TypeError):
        valid = False
    if not valid:
        raise ChallengeError("provider_single_attempt_unavailable")


def canonical(name):
    if not isinstance(name, str) or not name or len(name) > 254:
        raise ChallengeError("rrset_shape_unsupported")
    return name.rstrip(".").lower()


def _logical(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 4096 or not _TXT.fullmatch(value):
        raise ChallengeError("rrset_shape_unsupported")
    try:
        parsed = dns.rdata.from_text(dns.rdataclass.IN, dns.rdatatype.TXT, value)
        return b"".join(parsed.strings).decode("ascii")
    except Exception:
        raise ChallengeError("rrset_shape_unsupported") from None


def validate_rrset(raw, name):
    if (not isinstance(raw, dict) or set(raw) != {"Name", "Type", "TTL", "ResourceRecords"}
            or canonical(raw.get("Name")) != canonical(name) or raw.get("Type") != "TXT"
            or type(raw.get("TTL")) is not int or not 0 <= raw["TTL"] <= 2147483647
            or not isinstance(raw.get("ResourceRecords"), list)
            or not 1 <= len(raw["ResourceRecords"]) <= MAX_VALUES):
        raise ChallengeError("rrset_shape_unsupported")
    size = 0
    for entry in raw["ResourceRecords"]:
        if not isinstance(entry, dict) or set(entry) != {"Value"}:
            raise ChallengeError("rrset_shape_unsupported")
        _logical(entry["Value"])
        size += len(entry["Value"].encode())
    if size > MAX_BYTES:
        raise ChallengeError("rrset_shape_unsupported")
    return copy.deepcopy(raw)


def snapshot(client, zone, name):
    try:
        result = client.list_resource_record_sets(HostedZoneId=zone,
            StartRecordName=canonical(name), StartRecordType="TXT", MaxItems="2")
    except Exception:
        raise ChallengeError("provider_receipt_unavailable") from None
    allowed = {"ResourceRecordSets", "IsTruncated", "NextRecordName", "NextRecordType",
               "NextRecordIdentifier", "MaxItems", "ResponseMetadata"}
    if (not isinstance(result, dict) or set(result) - allowed
            or type(result.get("IsTruncated")) is not bool
            or not isinstance(result.get("ResourceRecordSets"), list)
            or len(result["ResourceRecordSets"]) > 2):
        raise ChallengeError("rrset_snapshot_incomplete")
    rows = result["ResourceRecordSets"]
    matching = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("Type"), str):
            raise ChallengeError("rrset_snapshot_incomplete")
        if canonical(row.get("Name")) == canonical(name) and row["Type"] == "TXT":
            matching.append(row)
    if result["IsTruncated"]:
        if (not rows or not isinstance(result.get("NextRecordType"), str)
                or not result.get("NextRecordName") or result.get("NextRecordIdentifier") is not None
                or canonical(result["NextRecordName"]) == canonical(name) and result["NextRecordType"] == "TXT"):
            raise ChallengeError("rrset_snapshot_incomplete")
    elif any(key in result for key in ("NextRecordName", "NextRecordType", "NextRecordIdentifier")):
        raise ChallengeError("rrset_snapshot_incomplete")
    if len(matching) > 1:
        raise ChallengeError("rrset_shape_unsupported")
    return validate_rrset(matching[0], name) if matching else None


def batch(zone, name, raw, remove_values, desired_values=(), ttl=60, comment=None):
    remove, desired = set(remove_values), set(desired_values)
    if (not all(isinstance(v, str) and _OWNED.fullmatch(v) for v in remove | desired)
            or len(remove | desired) > MAX_VALUES or type(ttl) is not int or not 0 <= ttl <= 2147483647):
        raise ChallengeError("owned_intent_invalid")
    changes, survivors, found = [], [], set()
    if raw is not None:
        raw = validate_rrset(raw, name)
        logical = [_logical(v["Value"]) for v in raw["ResourceRecords"]]
        for value in remove | desired:
            if logical.count(value) > 1:
                raise ChallengeError("rrset_shape_unsupported")
        for entry, value in zip(raw["ResourceRecords"], logical):
            if value not in remove or value in desired:
                survivors.append(copy.deepcopy(entry))
                found.add(value)
    for value in sorted(desired - found):
        survivors.append({"Value": route53.format_txt_value(value)})
    if len(survivors) > MAX_VALUES or sum(len(v["Value"].encode()) for v in survivors) > MAX_BYTES:
        raise ChallengeError("rrset_shape_unsupported")
    if raw is not None and survivors == raw["ResourceRecords"]:
        return None
    if raw is not None:
        changes.append({"Action": "DELETE", "ResourceRecordSet": raw})
    if survivors:
        replacement = copy.deepcopy(raw) if raw is not None else {"Name": canonical(name), "Type": "TXT", "TTL": ttl}
        replacement["ResourceRecords"] = survivors
        changes.append({"Action": "CREATE", "ResourceRecordSet": replacement})
    if not changes:
        return None
    if not isinstance(comment, str) or not 1 <= len(comment) <= 256:
        raise ChallengeError("owned_intent_invalid")
    return {"HostedZoneId": zone, "ChangeBatch": {"Comment": comment, "Changes": changes}}


def change_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ChallengeError("provider_receipt_unavailable")
    return value.split("/")[-1]


def submit(client, request):
    validate_client(client)
    try:
        result = client.change_resource_record_sets(**request)
    except ClientError as error:
        data = error.response
        retried = (data.get("ResponseMetadata") or {}).get("RetryAttempts", 0)
        conclusive = retried == 0 and (data.get("Error") or {}).get("Code") == "InvalidChangeBatch"
        raise ChallengeError("generation_changed" if conclusive else "provider_receipt_unavailable",
                             no_change=conclusive) from None
    except Exception:
        raise ChallengeError("provider_receipt_unavailable") from None
    if (result.get("ResponseMetadata") or {}).get("RetryAttempts", 0) != 0:
        raise ChallengeError("provider_single_attempt_unavailable")
    change = result.get("ChangeInfo") or {}
    if change.get("Status") not in ("PENDING", "INSYNC"):
        raise ChallengeError("provider_receipt_unavailable")
    request_id = (result.get("ResponseMetadata") or {}).get("RequestId", "")
    if not isinstance(request_id, str) or len(request_id) > 256:
        raise ChallengeError("provider_receipt_unavailable")
    return {"change_id": change_id(change.get("Id")), "status": change["Status"], "request_id": request_id}


def status(client, expected_id):
    expected_id = change_id(expected_id)
    try:
        result = client.get_change(Id=expected_id)
    except Exception:
        raise ChallengeError("provider_receipt_unavailable") from None
    change = result.get("ChangeInfo") or {}
    if change_id(change.get("Id")) != expected_id or change.get("Status") not in ("PENDING", "INSYNC"):
        raise ChallengeError("provider_receipt_unavailable")
    return change["Status"]
