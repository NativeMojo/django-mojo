"""The optional hosted zone and the records that point at the environment.

Optional because a domain is often already managed somewhere else, and taking
over a zone that a registrar or another team owns is not a thing a bootstrap
should do quietly. With no `spec.domain`, this step reports that and does
nothing.

WHERE THE RECORD POINTS depends on whether a balancer exists. With one, the
record is an alias to the NLB — free, health-aware, and it follows the
balancer's addresses. Without one (the micro preset), it is a plain A record to
the node's own elastic IP, which is why `nodes.py` reserves one exactly when
there is no balancer to hold it.

The delegation set — the four name servers a newly created zone is served from —
is RETURNED, not printed. An operator has to paste those into their registrar
and nothing else about the environment works until they do, so it is the single
most important thing this whole run produces. Printing it here would put it in
the wrong place for the portal, which renders the same value into a browser.
"""

import time

from mojo.deploy.provision import report
from mojo.deploy.provision import spec as spec_module


STEP = "dns"

RECORD_TTL = 60


def record_names(spec):
    """The names this environment answers on.

    The apex and `api.` both point at the same place. `api.` is what an app
    configures; the apex is what a person types.
    """
    if not spec.domain:
        return []
    domain = spec.domain.rstrip(".")
    return [domain, f"api.{domain}"]


def ensure_dns(clients, spec, observed, apply=False):
    findings, actions = [], []
    result = report.Result()

    if not spec.domain:
        findings.append(report.existing(
            STEP, "dns.not_wanted",
            "no domain was declared, so no DNS records are managed here"))
        return findings, actions, result

    route53 = clients.get("route53")
    zone = observed.get("hosted_zone")
    domain = spec.domain.rstrip(".")

    if not zone:
        if not spec.create_zone:
            findings.append(report.manual(
                STEP, "zone.missing",
                f"there is no Route 53 hosted zone for {domain} in this "
                f"account, and spec.create_zone is off",
                f"create the zone yourself and re-run, or set create_zone if "
                f"this account should be authoritative for {domain}"))
            return findings, actions, result

        findings.append(report.missing(
            STEP, "zone.missing",
            f"no hosted zone for {domain}",
            "apply creates one and returns the name servers to delegate to"))
        actions.append(report.Action(STEP, "create", domain))
        if not apply:
            return findings, actions, result
        created = report.safe(
            findings, STEP, "route53.create_hosted_zone",
            lambda: route53.create_hosted_zone(
                Name=f"{domain}.",
                # Route 53 uses this to make the call idempotent. A stable
                # value would make a retry a no-op but also make a deliberate
                # second zone impossible; a timestamp is what the API expects.
                CallerReference=f"django-mojo-{spec.project}-{spec.env}-"
                                f"{int(time.time())}",
                HostedZoneConfig={
                    "Comment": f"django-mojo {spec.project}-{spec.env}"}))
        if not created:
            return findings, actions, result
        zone = created.get("HostedZone") or {}
        result.set("name_servers",
                   list((created.get("DelegationSet") or {}).get(
                       "NameServers") or []))
        # Route 53 has no Tags parameter on CreateHostedZone. Same shape as the
        # S3 exception in storage.py, and safe for the same reason: a zone is
        # found by its domain name, so an untagged one left by an interrupted
        # run is still adopted rather than duplicated.
        report.safe(
            findings, STEP, "route53.change_tags_for_resource",
            lambda: route53.change_tags_for_resource(
                ResourceType="hostedzone",
                ResourceId=(zone.get("Id") or "").split("/")[-1],
                AddTags=spec_module.tag_list(spec, "dns", name=domain)))
    else:
        findings.append(report.existing(
            STEP, "zone.ok", f"hosted zone for {domain} is in place"))

    zone_id = (zone or {}).get("Id")
    result.set("zone_id", zone_id)

    _ensure_records(route53, spec, observed, zone_id, findings, actions, apply,
                    result)
    return findings, actions, result


def _target(spec, observed):
    """Where the records should point, and how.

    Returns (kind, value, hosted_zone_id) — kind is "alias" for a balancer and
    "address" for a bare node.
    """
    if spec_module.wants_balancer(spec):
        dns_name = observed.get("balancer_dns")
        zone_id = observed.get("balancer_zone_id")
        if dns_name and zone_id:
            return "alias", dns_name, zone_id
        return None, None, None
    addresses = [value for value in (observed.get("node_addresses") or [])
                 if value]
    if addresses:
        return "address", addresses[0], None
    return None, None, None


def _ensure_records(route53, spec, observed, zone_id, findings, actions, apply,
                    result):
    kind, value, target_zone = _target(spec, observed)
    if not kind:
        findings.append(report.pending(
            STEP, "records.no_target",
            "the balancer or node address these records point at is not "
            "available yet"))
        return

    existing = {}
    for record in observed.get("record_sets") or []:
        if record.get("Type") == "A":
            existing[(record.get("Name") or "").rstrip(".")] = record

    wanted_names = record_names(spec)
    changes = []
    for name in wanted_names:
        current = existing.get(name)
        if current and _record_matches(current, kind, value, target_zone):
            findings.append(report.existing(
                STEP, "record.ok", f"{name} already points at {value}"))
            continue
        state = "does not point at" if current else "does not exist; it should point at"
        findings.append(report.drift(
            STEP, "record.drift" if current else "record.missing",
            f"{name} {state} {value}",
            "apply upserts the record"))
        actions.append(report.Action(STEP, "modify" if current else "create",
                                     name, value))
        changes.append(_change(name, kind, value, target_zone))

    result.set("records", wanted_names)
    if not changes or not apply or not zone_id:
        return
    report.safe(
        findings, STEP, "route53.change_resource_record_sets",
        lambda: route53.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={
                "Comment": f"django-mojo {spec.project}-{spec.env}",
                # UPSERT, never DELETE. Re-running is a no-op and a record that
                # is no longer wanted is left for a human.
                "Changes": changes}))


def _change(name, kind, value, target_zone):
    record = {"Name": f"{name}.", "Type": "A"}
    if kind == "alias":
        record["AliasTarget"] = {
            "HostedZoneId": target_zone,
            "DNSName": f"{value}.",
            # An NLB has no health check of its own to evaluate here; the target
            # groups do that a layer down.
            "EvaluateTargetHealth": False,
        }
    else:
        record["TTL"] = RECORD_TTL
        record["ResourceRecords"] = [{"Value": value}]
    return {"Action": "UPSERT", "ResourceRecordSet": record}


def _record_matches(record, kind, value, target_zone):
    if kind == "alias":
        alias = record.get("AliasTarget") or {}
        return (alias.get("DNSName") or "").rstrip(".") == value.rstrip(".")
    values = {row.get("Value") for row in record.get("ResourceRecords") or []}
    return value in values
