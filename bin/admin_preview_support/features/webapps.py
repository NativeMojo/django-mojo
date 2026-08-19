from urllib.parse import parse_qs, urlsplit


NAME = "webapps"

# One managed domain the preview knows about, so URL-first prechecks can show a
# ready fast-path, an apex steer, and an unknown-domain (keep-my-DNS) fork.
KNOWN_DOMAIN = "nativemojo.com"
CNAME_TARGET = "edge.nativemojo.com"


def _owner(handler):
    return handler if isinstance(handler, type) else type(handler)


def describe(capabilities):
    values = {"view": capabilities["webapps"],
              "manage": capabilities["manage_webapps"],
              "onboard": capabilities["manage_webapps"]}
    enabled = values["view"]
    values["infrastructure_managed"] = capabilities["infrastructure_managed"]
    return {"id": NAME, "enabled": enabled, "capabilities": values,
            "contracts": {"onboarding": 1, "summary": 1}}


def reset(handler, fixtures, *, key_state="active", onboarding_state="idle",
          deployments_state="mixed", **options):
    handler.key_state = key_state
    handler.onboarding_state = onboarding_state
    handler.deployments_state = deployments_state
    handler.onboarding_receipts = {}
    handler.webapps = [dict(row) for row in fixtures["webapps"]]
    handler.webapp_onboarding_factory = fixtures["webapp_onboarding"]
    handler.onboarding_operation = (
        fixtures["webapp_onboarding"](onboarding_state)
        if onboarding_state not in ("idle", "new_group") else None)


def _precheck(url):
    """URL-first verdict, mirroring the real precheck shape for the preview."""
    raw = str(url or "").strip()
    base = {"schema_version": 1, "cname_target": CNAME_TARGET}
    if not raw:
        return {**base, "verdict": "invalid", "reason": "Enter the web address you want"}
    candidate = raw if "://" in raw else f"https://{raw}"
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").strip().lower()
    trailing = (parsed.path or "").strip("/")
    if not host:
        return {**base, "verdict": "invalid", "reason": "That address has no hostname"}
    if trailing:
        segment = trailing.split("/")[0]
        return {**base, "verdict": "path", "host": host,
                "suggestion": f"{segment}.{host}",
                "reason": "Apps are served on a whole address, not a path under one"}
    normalized = {"url": f"https://{host}", "hostname": host,
                  "scheme_note": "http upgraded to https" if parsed.scheme == "http" else None}
    domain = {"id": 11, "name": KNOWN_DOMAIN, "provider": "route53"}
    if host == KNOWN_DOMAIN:
        normalized["domain_name"] = KNOWN_DOMAIN
        return {**base, "verdict": "apex", "normalized": normalized, "domain": domain,
                "suggestion": f"www.{KNOWN_DOMAIN}",
                "reason": "Apps are served on a subdomain, not the bare domain"}
    if host.endswith(f".{KNOWN_DOMAIN}"):
        normalized.update({"domain_name": KNOWN_DOMAIN,
                           "label": host[: -(len(KNOWN_DOMAIN) + 1)]})
        return {**base, "verdict": "ready", "normalized": normalized, "domain": domain,
                "records": [{"type": "CNAME", "name": host, "value": CNAME_TARGET, "ttl": 300}]}
    return {**base, "verdict": "domain_unknown", "normalized": normalized,
            "options": {"external_available": True, "purchase_available": True,
                        "godaddy_available": True}}


def _summary():
    return {
        "schema_version": 1,
        "webapp": {"id": 42, "slug": "customer-portal", "display_name": "Customer Portal",
                   "environment": "production", "repository": "NativeMojo/customer-portal",
                   "deployment_ref": "main", "build_output": "dist"},
        "address": {"hostname": "portal.nativemojo.com",
                    "https_origin": "https://portal.nativemojo.com",
                    "domain": {"id": 11, "name": KNOWN_DOMAIN, "provider": "route53"},
                    "certificate": {"status": "active", "not_after": "2030-01-16T08:00:00Z"}},
        "current_release": {"id": 8, "version": "2026.08.10", "status": "live",
                            "created": "2026-08-10T18:00:00Z"},
        "latest_deployment": {"id": 501, "status": "live",
                              "created": "2026-08-10T18:00:00Z", "finished": "2026-08-10T18:02:00Z"},
        "onboarding": {"status": "succeeded", "cursor": "complete", "evidence": {}},
        "deployment_key": {"linked": True, "active": True},
    }


# A real release id is the 40-character workflow sha — the fixture proves the
# row demotes it to a 10-character muted mono detail.
FORTY_CHAR_VERSION = "3f9c2d1e8a" * 4
ACTIVE_CERT = {"status": "active", "not_after": "2030-01-16T08:00:00Z"}
EXPIRED_CERT = {"status": "active", "not_after": "2026-08-01T08:00:00Z"}


def _summary_item(app_id, slug, display_name, *, hostname=None, certificate=None,
                  release=None, deployment=None, ref="main"):
    return {
        "webapp": {"id": app_id, "slug": slug, "display_name": display_name,
                   "environment": "production", "deployment_ref": ref},
        "address": ({"hostname": hostname, "certificate": certificate}
                    if hostname else None),
        "current_release": release,
        "latest_deployment": deployment,
    }


def _summaries(state):
    """Deterministic merged-lane app rows, keyed by --deployments-state."""
    live_release = {"id": 8, "version": FORTY_CHAR_VERSION, "status": "live",
                    "created": "2026-08-10T18:00:00Z"}
    live_deploy = {"id": 501, "status": "live", "created": "2026-08-10T18:00:00Z",
                   "finished": "2026-08-10T18:02:00Z"}
    failed_deploy = {"id": 502, "status": "failed", "created": "2026-08-12T09:00:00Z",
                     "finished": "2026-08-12T09:01:00Z"}
    docs_release = {"id": 9, "version": "2026.08.11", "status": "live",
                    "created": "2026-08-11T18:00:00Z"}
    docs_deploy = {"id": 503, "status": "live", "created": "2026-08-11T18:00:00Z",
                   "finished": "2026-08-11T18:01:30Z"}
    portal_green = _summary_item(
        42, "customer-portal", "Customer Portal", hostname="portal.nativemojo.com",
        certificate=dict(ACTIVE_CERT), release=live_release, deployment=live_deploy)
    docs_bare = _summary_item(54, "docs", "Documentation")
    docs_green = _summary_item(
        54, "docs", "Documentation", hostname="docs.nativemojo.com",
        certificate=dict(ACTIVE_CERT), release=docs_release, deployment=docs_deploy)
    portal_failed = _summary_item(
        42, "customer-portal", "Customer Portal", hostname="portal.nativemojo.com",
        certificate=dict(EXPIRED_CERT), release=live_release, deployment=failed_deploy)
    items = {
        "mixed": [portal_green, docs_bare],
        "converged": [portal_green, docs_green],
        "failed": [portal_failed, docs_green],
        "empty": [],
    }.get(state, [portal_green, docs_bare])
    return {"schema_version": 1, "items": items, "count": len(items),
            "limit": 50, "truncated": False}


def _deployments():
    return [
        {"id": 501, "created": "2026-08-10T18:00:00Z", "started": "2026-08-10T18:00:10Z",
         "finished": "2026-08-10T18:02:00Z", "status": "live",
         "release": {"id": 8, "version": "2026.08.10", "status": "live", "created": "2026-08-10T18:00:00Z"},
         "previous_release": {"id": 7, "version": "2026.08.09", "status": "superseded", "created": "2026-08-09T18:00:00Z"}},
        {"id": 498, "created": "2026-08-09T18:00:00Z", "started": "2026-08-09T18:00:10Z",
         "finished": "2026-08-09T18:02:00Z", "status": "superseded",
         "release": {"id": 7, "version": "2026.08.09", "status": "superseded", "created": "2026-08-09T18:00:00Z"},
         "previous_release": None},
    ]


def _releases():
    return [
        {"id": 8, "version": "2026.08.10", "status": "live", "created": "2026-08-10T18:00:00Z"},
        {"id": 7, "version": "2026.08.09", "status": "superseded", "created": "2026-08-09T18:00:00Z"},
        {"id": 6, "version": "2026.08.08", "status": "uploaded", "created": "2026-08-08T18:00:00Z"},
    ]


def get(handler, parsed):
    path = parsed.path
    query = parse_qs(parsed.query)
    if path == "/api/edge/webapp/onboarding/precheck":
        return 200, _precheck(query.get("url", [""])[0])
    if path == "/api/edge/webapp/onboarding/detail":
        operation_id = query.get("operation", [""])[0]
        receipt = handler.onboarding_receipts.get(operation_id)
        if receipt is None:
            return 404, {"error": "WebApp onboarding operation not found"}
        return 200, receipt
    if path == "/api/edge/webapp/summaries":
        return 200, _summaries(getattr(handler, "deployments_state", "mixed"))
    if path == "/api/edge/webapp/summary":
        return 200, _summary()
    if path == "/api/edge/webapp/deployment":
        return 200, _deployments()
    if path == "/api/edge/webapp/release":
        return 200, _releases()
    if path == "/api/edge/webapp/health":
        return 200, {"webapp": 42, "status": "healthy",
                     "checked": "2026-08-10T18:05:00Z", "detail": "HTTP 200"}
    return None


def post(handler, path, payload):
    if path == "/api/dnsman/delegation/initiate":
        return 200, {"id": 77, "domain": 11, "domain_name": payload.get("name"),
                     "source": f"_acme-challenge.{payload.get('name')}",
                     "target": "_acme.edge.nativemojo.com", "state": "pending",
                     "verified_at": None, "last_error_code": ""}
    if path == "/api/dnsman/delegation/verify":
        return 200, {"id": payload.get("delegation") or 77, "domain": 11,
                     "domain_name": "example.com", "state": "verified",
                     "verified_at": "2026-08-10T18:00:00Z", "last_error_code": ""}
    if path == "/api/edge/webapp/rollback":
        return 200, {"webapp": 42, "deployment": 512, "status": "rolling_back",
                     "release": {"id": payload.get("release"), "version": "2026.08.09"}}
    if path == "/api/edge/webapp/detach_address":
        return 200, {"webapp": payload.get("webapp"), "address": None}
    if path != "/api/edge/webapp/onboarding/create":
        return None
    owner = _owner(handler)
    operation_id = str(payload.get("operation_id") or "preview")
    receipt = handler.onboarding_receipts.get(operation_id)
    if receipt is not None:
        return 200, {"created": False, "operation": receipt}

    operation = owner.webapp_onboarding_factory("address")
    operation["operation_id"] = operation_id
    operation["profile"].update({
        key: value for key, value in payload.items()
        if key in operation["profile"]
    })
    if payload.get("group_intent") == "new":
        group = {"id": 109,
                 "name": payload.get("display_name") or "New WebApp Group"}
        operation["group"] = group
        operation["resources"]["webapp"] = 142
        if not any(row.get("id") == group["id"] for row in handler.groups):
            owner.groups.append({
                **group, "uuid": "preview-group-109", "kind": "organization",
                "is_active": True, "member_count": 0, "last_activity": None,
                "metadata": {}, "parent": None})
        if not any(row.get("id") == 142 for row in handler.webapps):
            owner.webapps.append({
                "id": 142, "slug": payload.get("slug"),
                "display_name": group["name"],
                "environment": payload.get("environment", "production"),
                "github_repository": "", "deployment_ref": "main",
                "build_output": "dist", "created": "2026-08-13T12:00:00Z",
                "current_release": None})
    owner.onboarding_operation = operation
    owner.onboarding_receipts[operation_id] = operation
    if handler.onboarding_state == "new_group":
        return 503, {"error": "Deterministic committed WebApp response loss"}
    return 200, {"created": True, "operation": operation}
