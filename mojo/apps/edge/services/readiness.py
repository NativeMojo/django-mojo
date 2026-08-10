"""System Setup readiness for hosting, fleet proof, and WebApp keys."""

from mojo.apps.account.services import system_readiness, system_settings
from mojo.helpers import dates
from mojo.helpers.settings import settings


def local_node_id():
    from mojo.apps.edge.settings_validators import node_id
    return node_id(settings.get_static("EDGE_NODE_ID", ""))


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
    for pool in topology["pools"]:
        installed = installer.read_installed(pool)
        evidence[pool] = {
            "generation": installed.get("generation"),
            "excluded": len(installed.get("excluded") or []),
            "www_pending": len(installed.get("www_pending") or {}),
            "cert_pending": len(installed.get("cert_pending") or []),
        }
    return {
        "node_id": local_node_id(),
        "django_mojo_version": mojo.__version__,
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
    domains = Domain.objects.all().select_related("credential")[:256]
    if not domains:
        rows.append(system_readiness.result(
            "hosting.domains", "pending", "No managed domains are configured.",
            "Register or adopt a domain in Domains."))
    for domain in domains:
        ready = domain.status == "active" and domain.has_usable_credential
        rows.append(system_readiness.result(
            f"hosting.domain.{domain.pk}", "pass" if ready else "fail",
            f"{domain.name} is ready for DNS changes." if ready else
            f"{domain.name} is not ready for DNS changes.",
            "Activate the domain and verify its linked DNS credential."))
    for certificate in Certificate.objects.all().select_related("domain")[:256]:
        ready = bool(certificate.status == "active" and certificate.not_after
                     and certificate.not_after > dates.utcnow())
        rows.append(system_readiness.result(
            f"hosting.certificate.{certificate.pk}", "pass" if ready else "fail",
            f"Certificate for {certificate.common_name} is active." if ready else
            f"Certificate for {certificate.common_name} is not healthy.",
            "Issue or renew this certificate from Certificates."))
    for row in AcmeDelegation.objects.all()[:256]:
        state = delegation.readiness_state(row)
        rows.append(system_readiness.result(
            f"hosting.delegation.{row.pk}", state["status"],
            state["explanation"], state["remediation"]))
    pending = DnsRecordReservation.objects.filter(state__in=LIVE_STATES).count()
    if pending:
        rows.append(system_readiness.result(
            "hosting.dns_reservations", "pending",
            f"{pending} certificate DNS record reservation(s) remain active.",
            "Rerun certificate issuance so durable challenge cleanup can finish."))
    return rows or [system_readiness.result(
        "hosting.dns", "pending", "DNS and certificate setup is incomplete.",
        "Add a domain and certificate.")]


def check_vhosts(context):
    from mojo.apps.edge.models import Vhost
    rows = []
    for vhost in Vhost.objects.filter(is_enabled=True).select_related(
            "domain", "certificate")[:256]:
        ready = vhost.domain.status == "active" and vhost.certificate.status == "active"
        rows.append(system_readiness.result(
            f"hosting.vhost.{vhost.pk}", "pass" if ready else "fail",
            f"{vhost.server_name} has a complete serving definition." if ready else
            f"{vhost.server_name} depends on an inactive domain or certificate.",
            "Repair the domain/certificate, then republish this Vhost."))
    return rows or [system_readiness.result(
        "hosting.vhosts", "pending", "No enabled Vhosts are configured.",
        "Create a Vhost for the installation's public domain.")]


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
    for node in nodes:
        proof = proofs.get(node)
        if proof is None:
            rows.append(system_readiness.result(
                f"fleet.node.{node}", "pending", f"Node {node} did not provide edge proof.",
                "Start its edge-channel runner and verify EDGE_NODE_ID."))
            continue
        if proof.get("django_mojo_version") != mojo.__version__:
            rows.append(system_readiness.result(
                f"fleet.node.{node}.version", "fail",
                f"Node {node} runs django-mojo {proof.get('django_mojo_version') or 'unknown'}; expected {mojo.__version__}.",
                "Deploy the installed django-mojo version to this node."))
        for pool in pools:
            evidence = (proof.get("pools") or {}).get(pool)
            healthy = bool(
                evidence and evidence.get("generation") == desired[pool]
                and not evidence.get("excluded") and not evidence.get("www_pending")
                and not evidence.get("cert_pending"))
            rows.append(system_readiness.result(
                f"fleet.node.{node}.pool.{pool}", "pass" if healthy else "pending",
                f"Node {node} installed pool {pool} generation {desired[pool]}." if healthy else
                f"Node {node} has not proven the desired generation for pool {pool}.",
                "Run pool convergence and repair excluded certificates or pending releases."))
    return rows


def check_webapp_keys(context):
    from mojo.apps.edge.models import WebApp, WebAppKeyOperation
    from mojo.apps.edge.services import webapp_keys

    rows = []
    for webapp in WebApp.objects.select_related("api_key").all()[:256]:
        metadata = webapp_keys.status(webapp)
        latest = WebAppKeyOperation.objects.filter(web_app=webapp).first()
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
        details = {"webapp": webapp.pk, "linked": metadata["linked"],
                   "active": metadata["active"],
                   "last_action": metadata.get("last_action")}
        rows.append(system_readiness.result(
            f"webapp.key.{webapp.pk}", status, explanation,
            "Create or rotate the deployment key and save its reveal-once value now.",
            details=details))
    return rows or [system_readiness.result(
        "webapp.keys", "pass", "No WebApps require deployment keys.")]


def register_sections():
    system_readiness.register_section(
        "hosting_dns", "Domains and certificates", check_dns, order=40)
    system_readiness.register_section(
        "hosting_vhosts", "Vhosts and routes", check_vhosts, order=41)
    system_readiness.register_section(
        "edge_fleet", "Fleet deployment readiness", check_fleet, order=42)
    system_readiness.register_section(
        "webapp_keys", "WebApp deployment keys", check_webapp_keys, order=43)
