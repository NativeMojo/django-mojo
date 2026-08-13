from urllib.parse import parse_qs


NAME = "webapps"


def _owner(handler):
    return handler if isinstance(handler, type) else type(handler)


def describe(capabilities):
    values = {"view": capabilities["webapps"],
              "manage": capabilities["manage_webapps"],
              "onboard": capabilities["manage_webapps"]}
    return {"id": NAME, "enabled": values["view"], "capabilities": values,
            "contracts": {"onboarding": 1, "summary": 1}}


def reset(handler, fixtures, *, key_state="active", onboarding_state="idle", **options):
    handler.key_state = key_state
    handler.onboarding_state = onboarding_state
    handler.onboarding_receipts = {}
    handler.webapps = [dict(row) for row in fixtures["webapps"]]
    handler.webapp_onboarding_factory = fixtures["webapp_onboarding"]
    handler.onboarding_operation = (
        fixtures["webapp_onboarding"](onboarding_state)
        if onboarding_state not in ("idle", "new_group") else None)


def get(handler, parsed):
    if parsed.path != "/api/edge/webapp/onboarding/detail":
        return None
    operation_id = parse_qs(parsed.query).get("operation", [""])[0]
    receipt = handler.onboarding_receipts.get(operation_id)
    if receipt is None:
        return 404, {"error": "WebApp onboarding operation not found"}
    return 200, receipt


def post(handler, path, payload):
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
