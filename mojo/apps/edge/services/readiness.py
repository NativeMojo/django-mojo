"""System Setup readiness for hosting, fleet proof, and WebApp keys."""

import json
import os
import stat
from pathlib import Path

from mojo.apps.account.services import system_readiness, system_settings
from mojo.helpers import dates
from mojo.helpers.settings import settings


DETAIL_LIMIT = 16
IDENTITY_LIMIT = 4096
LEGACY_IDENTITY_LIMIT = 128


def _read_bounded(path, limit):
    """Read one small regular file through one no-follow descriptor."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = None
    try:
        descriptor = os.open(os.fspath(path), flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            return None
        chunks = []
        total = 0
        while total <= limit:
            block = os.read(descriptor, min(4096, limit + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
        final = os.fstat(descriptor)
        raw = b"".join(chunks)
        if (len(raw) > limit or final.st_size != len(raw)
                or final.st_size != metadata.st_size
                or final.st_mtime_ns != metadata.st_mtime_ns
                or final.st_ctime_ns != metadata.st_ctime_ns
                or final.st_dev != metadata.st_dev
                or final.st_ino != metadata.st_ino):
            return None
        return raw.decode("utf-8", "strict")
    except (OSError, UnicodeError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _present(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        # Unreadable state is present for fail-closed identity purposes.
        return True


def _read_deploy_identity(root=Path("var")):
    """Read atomic v2 identity, with one bounded predecessor fallback.

    A present manifest is authoritative even when malformed. The invalidation
    marker closes the otherwise unavoidable window where an interrupted v2
    update could expose the predecessor's two non-atomic legacy files.
    """
    from mojo.apps.edge.services import deploy, platform_deploy

    root = Path(root)
    manifest = root / "deploy_identity.json"
    if _present(root / "deploy_identity.invalid"):
        return "", ""
    if _present(manifest):
        raw = _read_bounded(manifest, IDENTITY_LIMIT)
        try:
            value = json.loads(raw) if raw is not None else None
        except (TypeError, ValueError):
            return "", ""
        if not isinstance(value, dict) or set(value) != {
                "schema", "sha", "deployment"} or value.get("schema") != 2:
            return "", ""
        sha = value.get("sha")
        deployment_id = platform_deploy.deployment_id(value.get("deployment"))
        if (not isinstance(sha, str) or len(sha) != 40
                or not deploy.is_valid_sha(sha) or not deployment_id
                or deployment_id != value.get("deployment")):
            return "", ""
        return sha, deployment_id

    sha_raw = _read_bounded(root / "deploy_sha", LEGACY_IDENTITY_LIMIT)
    uuid_raw = _read_bounded(root / "deployment_uuid", LEGACY_IDENTITY_LIMIT)
    sha = (sha_raw or "").strip()
    supplied_uuid = (uuid_raw or "").strip()
    deployment_id = platform_deploy.deployment_id(supplied_uuid)
    if (len(sha) != 40 or not deploy.is_valid_sha(sha)
            or not deployment_id or deployment_id != supplied_uuid):
        return "", ""
    return sha, deployment_id


def _aggregate(code, label, counts, remediation, details=None):
    status = "pass"
    for candidate in ("fail", "pending", "warn"):
        if counts.get(candidate):
            status = candidate
            break
    total = sum(counts.values())
    explanation = (
        f"{label}: {counts.get('pass', 0)} ready, {counts.get('warn', 0)} warning, "
        f"{counts.get('pending', 0)} pending, {counts.get('fail', 0)} failed "
        f"across {total} item(s).")
    safe_details = {"total": total}
    for key in ("pass", "warn", "pending", "fail"):
        safe_details[key] = counts.get(key, 0)
    if details:
        safe_details.update(details)
    return system_readiness.result(
        code, status, explanation, remediation, details=safe_details)


def local_node_id():
    """Return the stable proof identity, defaulting to the job host identity.

    Ordinary hosts already have a cross-platform identity through
    ``socket.gethostname()`` in the job engine. Requiring every project to
    repeat that value as EDGE_NODE_ID made safe deployment proof fail after a
    successful install. Keep the file-only setting as an override for
    containers and platforms whose hostnames are ephemeral or duplicated.
    """
    from mojo.apps.edge.settings_validators import node_id
    configured = settings.get_static("EDGE_NODE_ID", "")
    if configured:
        return node_id(configured)
    from mojo.apps.jobs.job_engine import host_channel
    return node_id(host_channel())


def local_node_proof(data=None):
    """Safe runner-control response: identity/version/generation metadata only."""
    import mojo
    from mojo.apps.edge.services import installer
    from mojo.apps.edge.settings_validators import expected_topology

    raw = data or {}
    topology = expected_topology("EDGE_EXPECTED_TOPOLOGY", {
        "nodes": [local_node_id()],
        "pools": raw.get("pools") or ["default"],
    })
    evidence = {}
    current = installer.current_target()
    current_generation = current.rsplit("/", 1)[-1] if current else None
    for pool in topology["pools"]:
        installed = installer.read_installed(pool)
        evidence[pool] = {
            "generation": installed.get("generation"),
            "excluded": len(installed.get("excluded") or []),
            "www_pending": len(installed.get("www_pending") or {}),
            "cert_pending": len(installed.get("cert_pending") or []),
            "serving_generation": installed.get("serving_generation"),
            "current_generation": current_generation,
        }
    deployed_sha, deployed_uuid = _read_deploy_identity()
    return {
        "node_id": local_node_id(),
        "django_mojo_version": mojo.__version__,
        "platform_sha": deployed_sha,
        "platform_deployment": deployed_uuid,
        "observed_at": dates.utcnow().isoformat(),
        "pools": evidence,
    }


def _desired_generations(pools):
    from mojo.apps.edge.services import convergence
    return {pool: convergence.desired_generation(pool) for pool in pools}


def _runner_proofs(runners, pools, timeout):
    from mojo.apps.jobs.manager import get_manager

    manager = get_manager()
    proofs = {}
    for runner in runners:
        runner_id = runner.get("runner_id")
        if not runner_id or not runner.get("alive"):
            continue
        response = manager.execute_on_runner(
            runner_id, "mojo.apps.edge.services.readiness.local_node_proof",
            {"pools": pools}, timeout=timeout)
        if not response or response.get("status") != "success":
            continue
        proof = response.get("result") or {}
        node = proof.get("node_id")
        if node and node not in proofs:
            proofs[node] = proof
    return proofs


def check_dns(context):
    from mojo.apps.dnsman.models import (
        AcmeDelegation, Certificate, Domain, DnsRecordReservation)
    from mojo.apps.dnsman.models.dns_record_reservation import LIVE_STATES
    from mojo.apps.dnsman.services import delegation

    rows = []
    counts = {"pass": 0, "warn": 0, "pending": 0, "fail": 0}
    domains_seen = 0
    for domain in Domain.objects.all().select_related("credential").iterator():
        domains_seen += 1
        ready = domain.status == "active" and domain.has_usable_credential
        counts["pass" if ready else "fail"] += 1
        if not ready and len(rows) < DETAIL_LIMIT:
            rows.append(system_readiness.result(
                f"hosting.domain.{domain.pk}", "fail",
                f"{domain.name} is not ready for DNS changes.",
                "Activate the domain and verify its linked DNS credential."))
    if not domains_seen:
        counts["pending"] += 1
    for certificate in Certificate.objects.all().select_related("domain").iterator():
        ready = bool(certificate.status == "active" and certificate.not_after
                     and certificate.not_after > dates.utcnow())
        counts["pass" if ready else "fail"] += 1
        if not ready and len(rows) < DETAIL_LIMIT:
            rows.append(system_readiness.result(
                f"hosting.certificate.{certificate.pk}", "fail",
                f"Certificate for {certificate.common_name} is not healthy.",
                "Issue or renew this certificate from Certificates."))
    for row in AcmeDelegation.objects.all().iterator():
        state = delegation.readiness_state(row)
        counts[state["status"]] += 1
        if state["status"] != "pass" and len(rows) < DETAIL_LIMIT:
            rows.append(system_readiness.result(
                f"hosting.delegation.{row.pk}", state["status"],
                state["explanation"], state["remediation"]))
    pending = DnsRecordReservation.objects.filter(state__in=LIVE_STATES).count()
    if pending:
        counts["pending"] += pending
    summary = _aggregate(
        "hosting.dns", "DNS and certificate readiness", counts,
        "Repair the listed domain, certificate, delegation, or challenge cleanup state.")
    return [summary] + rows


def check_vhosts(context):
    from mojo.apps.edge.models import Vhost
    rows = []
    counts = {"pass": 0, "warn": 0, "pending": 0, "fail": 0}
    seen = 0
    for vhost in Vhost.objects.filter(is_enabled=True).select_related(
            "domain", "certificate").iterator():
        seen += 1
        ready = vhost.domain.status == "active" and vhost.certificate.status == "active"
        counts["pass" if ready else "fail"] += 1
        if not ready and len(rows) < DETAIL_LIMIT:
            rows.append(system_readiness.result(
                f"hosting.vhost.{vhost.pk}", "fail",
                f"{vhost.server_name} depends on an inactive domain or certificate.",
                "Repair the domain/certificate, then republish this Vhost."))
    if not seen:
        counts["pending"] = 1
    return [_aggregate(
        "hosting.vhosts", "Vhost readiness", counts,
        "Create or repair enabled Vhosts and their domains/certificates.")] + rows


def check_fleet(context):
    import mojo
    from mojo.apps import jobs

    topology = system_settings.get_value(system_settings.EXPECTED_EDGE_TOPOLOGY)
    if not topology or not topology.get("nodes") or not topology.get("pools"):
        return [system_readiness.result(
            "fleet.topology", "pending", "Expected fleet node/pool topology is missing.",
            "Define every expected node and pool in System Setup.")]
    nodes = topology["nodes"]
    pools = topology["pools"]
    desired = _desired_generations(pools)
    # This filter is a security boundary: unrelated job runners are never
    # treated as edge nodes and never receive node-proof calls.
    runners = jobs.get_runners(channel="edge")
    proofs = _runner_proofs(runners, pools, float(context.get("timeout", 2.0)))
    rows = []
    counts = {"pass": 0, "warn": 0, "pending": 0, "fail": 0}
    for node in nodes:
        proof = proofs.get(node)
        if proof is None:
            counts["pending"] += len(pools)
            if len(rows) < DETAIL_LIMIT:
                rows.append(system_readiness.result(
                    f"fleet.node.{node}", "pending", f"Node {node} did not provide edge proof.",
                    "Start its edge-channel runner and verify its hostname or "
                    "EDGE_NODE_ID override matches the expected topology."))
            continue
        if proof.get("django_mojo_version") != mojo.__version__:
            counts["fail"] += 1
            if len(rows) < DETAIL_LIMIT:
                rows.append(system_readiness.result(
                    f"fleet.node.{node}.version", "fail",
                    f"Node {node} runs a different django-mojo version.",
                    "Deploy the installed django-mojo version to this node."))
        for pool in pools:
            evidence = (proof.get("pools") or {}).get(pool)
            healthy = bool(
                evidence and evidence.get("generation") == desired[pool]
                and evidence.get("serving_generation")
                and evidence.get("serving_generation") == evidence.get("current_generation")
                and not evidence.get("excluded") and not evidence.get("www_pending")
                and not evidence.get("cert_pending"))
            counts["pass" if healthy else "pending"] += 1
            if not healthy and len(rows) < DETAIL_LIMIT:
                rows.append(system_readiness.result(
                    f"fleet.node.{node}.pool.{pool}", "pending",
                    f"Node {node} has not proven serving state for pool {pool}.",
                    "Run combined pool convergence and repair excluded certificates or pending releases."))
    return [_aggregate(
        "fleet.summary", "Fleet node/pool readiness", counts,
        "Repair every expected node/pool pair; bounded detail follows.",
        {"nodes": len(nodes), "pools": len(pools)})] + rows


def check_webapp_keys(context):
    from mojo.apps.edge.models import WebApp, WebAppKeyOperation
    from mojo.apps.edge.services import webapp_keys

    rows = []
    counts = {"pass": 0, "warn": 0, "pending": 0, "fail": 0}
    actions = {}
    for webapp in WebApp.objects.select_related("api_key").all().iterator():
        metadata = webapp_keys.status(webapp)
        latest = WebAppKeyOperation.objects.filter(web_app=webapp).first()
        action = metadata.get("last_action") or "none"
        actions[action] = actions.get(action, 0) + 1
        if metadata["linked"] and metadata["active"]:
            status = "pass"
            explanation = f"{webapp.slug} has an active reveal-once deployment key."
        elif latest and latest.action == WebAppKeyOperation.ACTION_REVOKE:
            status = "warn"
            explanation = f"{webapp.slug}'s deployment key is revoked."
        elif metadata["linked"]:
            status = "fail"
            explanation = f"{webapp.slug}'s linked deployment key is inactive."
        else:
            status = "pending"
            explanation = f"{webapp.slug} has no deployment key."
        counts[status] += 1
        details = {"webapp": webapp.pk, "linked": metadata["linked"],
                   "active": metadata["active"],
                   "last_action": metadata.get("last_action")}
        if status != "pass" and len(rows) < DETAIL_LIMIT:
            rows.append(system_readiness.result(
                f"webapp.key.{webapp.pk}", status, explanation,
                "Create or rotate the deployment key and save its reveal-once value now.",
                details=details))
    if not sum(counts.values()):
        return [system_readiness.result(
            "webapp.keys", "pass", "No WebApps require deployment keys.")]
    return [_aggregate(
        "webapp.keys", "WebApp deployment-key readiness", counts,
        "Create, rotate, or repair the listed deployment keys.",
        {f"action_{action}": count for action, count in actions.items()})] + rows


def check_webapp_destination(context):
    """Where guided WebApp addresses will point, and whether it resolves.

    A managed-domain WebApp needs one thing the browser cannot supply: the
    hostname its CNAME points at. This surfaces the resolved destination and its
    provenance so an operator sees, before onboarding anyone, that the
    installation can serve app addresses — and what to fix when it cannot.
    """
    from mojo.apps.edge.services import webapp_destination

    override = str(settings.get_static(
        webapp_destination.OVERRIDE_SETTING, "") or "").strip()
    try:
        resolved = webapp_destination.resolve()
    except webapp_destination.DestinationUnavailable as err:
        # A set-but-invalid override is a misconfiguration to fix now (fail); an
        # install that has simply not finished Setup is pending, not broken.
        status = "fail" if override else "pending"
        return [system_readiness.result(
            "webapp.destination", status, str(err),
            "Set the platform's public address (BASE_URL) in System Setup, or "
            "set EDGE_WEBAPP_CNAME_TARGET for a split serving topology.",
            details={"override_configured": bool(override)})]
    source = ("a configured override" if resolved.provenance == "override"
              else "the platform's public address")
    return [system_readiness.result(
        "webapp.destination", "pass",
        f"WebApp addresses will point at {resolved.value} (from {source}). This "
        f"assumes {resolved.value} fronts the nodes that serve web apps; set "
        "EDGE_WEBAPP_CNAME_TARGET if a different tier or pool serves them.",
        details={"destination": resolved.value,
                 "provenance": resolved.provenance})]


def check_apps_domain(context):
    """Whether new web apps go live instantly, with zero per-app DNS work.

    Installation-level statement about the platform's apps domain — the
    writable domain the platform's own address sits under (or the single
    writable domain). Pass means the wildcard CNAME and a covering certificate
    already exist; pending names what is missing and that converging (the
    first onboarding under it) creates it.
    """
    from mojo.apps.edge.services import webapp_apps_domain, webapp_destination

    domain = webapp_apps_domain.installation_domain()
    if domain is None:
        return [system_readiness.result(
            "webapp.apps_domain", "pending",
            "No domain managed here is ready to host new web apps yet.",
            "Connect or purchase a domain; onboarding the first app under it "
            "creates the wildcard DNS record and certificate automatically.")]
    try:
        state = webapp_apps_domain.status(domain)
    except webapp_destination.DestinationUnavailable as err:
        return [system_readiness.result(
            "webapp.apps_domain", "pending", str(err),
            "Set the platform's public address (BASE_URL) in System Setup "
            "first.", details={"domain": domain.name})]
    except Exception as exc:
        return [system_readiness.result(
            "webapp.apps_domain", "fail",
            f"Could not read {domain.name}'s DNS records to confirm wildcard "
            f"coverage ({type(exc).__name__}).",
            "Verify the domain's DNS credential, then re-run this check.",
            details={"domain": domain.name})]
    if state.record and state.certificate:
        return [system_readiness.result(
            "webapp.apps_domain", "pass",
            f"New web apps go live instantly under {domain.name} (wildcard "
            "DNS + certificate in place).",
            details={"domain": domain.name, "destination": state.destination})]
    missing = []
    if not state.record:
        missing.append(f"the *.{domain.name} DNS record")
    if not state.certificate:
        missing.append("a covering certificate")
    return [system_readiness.result(
        "webapp.apps_domain", "pending",
        f"{domain.name} is missing {' and '.join(missing)}; converging will "
        "create it. The first app onboarded under this domain does that "
        "automatically.",
        "Onboard a web app under this domain, or converge it from Domains.",
        details={"domain": domain.name, "destination": state.destination,
                 "record": bool(state.record),
                 "certificate": bool(state.certificate)})]


def register_sections():
    system_readiness.register_section(
        "hosting_dns", "Domains and certificates", check_dns, order=40)
    system_readiness.register_section(
        "hosting_vhosts", "Vhosts and routes", check_vhosts, order=41)
    system_readiness.register_section(
        "edge_fleet", "Fleet deployment readiness", check_fleet, order=42)
    system_readiness.register_section(
        "webapp_keys", "WebApp deployment keys", check_webapp_keys, order=43)
    system_readiness.register_section(
        "webapp_destination", "WebApp serving destination",
        check_webapp_destination, order=44)
    system_readiness.register_section(
        "apps_domain", "Apps domain", check_apps_domain, order=45)
