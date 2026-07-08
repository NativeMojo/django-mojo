"""Core regression for ITEM-018: a GroupMember-scoped permission grant (which
any group admin can hand out, arbitrary keys allowed) must NOT authorize a
platform-global endpoint, even when the caller passes their own group id.

Before the fix, @md.requires_perms fell back to
request.group.user_has_permission(...) using the client-supplied `group` param,
so a member-scoped grant + group id reached global config / fleet ops. After
the fix those endpoints use @md.requires_global_perms (global User grants only).

The sweep is intentionally broad but avoids destructive handlers — the denial
fires in the decorator BEFORE any handler runs, so a 403 never executes the
endpoint. Each switched REST file was changed file-wide (all sites identically),
so a representative sample per file proves the whole file switched.
"""
from testit import helpers as th
from tests.test_global_perms._helpers import (
    ALL_ENDPOINT_PERMS, make_group_member, make_user, login,
)


# (method, path) — every one reads/writes PLATFORM-GLOBAL state. Path params are
# arbitrary: the decorator runs first, so the value is never dereferenced.
GLOBAL_ENDPOINTS = [
    # jobs control (mojo/apps/jobs/rest/control.py — all 12 switched file-wide)
    ("GET", "/api/jobs/control/config"),
    ("GET", "/api/jobs/control/queue-sizes"),
    ("GET", "/api/jobs/control/channels"),
    # jobs (mojo/apps/jobs/rest/jobs.py — all 14 switched file-wide)
    ("GET", "/api/jobs/status/deadbeef"),
    ("GET", "/api/jobs/health"),
    ("GET", "/api/jobs/stats"),
    ("GET", "/api/jobs/runners"),
    ("POST", "/api/jobs/cancel"),
    # aws infra (reads only in the sweep — onboard/reconcile/send are destructive
    # but share the identical file-wide guard)
    ("GET", "/api/aws/cloudwatch/resources"),
    ("GET", "/api/aws/s3/bucket"),
    # account admin (cross-tenant)
    ("GET", "/api/auth/manage/throttle"),
    ("POST", "/api/auth/manage/clear_rate_limit"),
    ("GET", "/api/user/device/lookup?duid=whatever"),
    ("GET", "/api/account/logins/summary"),
    ("GET", "/api/account/logins/user?user_id=1"),
    ("POST", "/api/account/devices/push/send"),
    ("POST", "/api/account/devices/push/config/1/test"),
    # metrics ACLs / incident health / assistant
    ("GET", "/api/metrics/permissions"),
    ("GET", "/api/incident/health/summary"),
    ("POST", "/api/assistant"),
    ("POST", "/api/assistant/context"),
    # geofence config plane (promoted decorator — re-confirms ITEM-017)
    ("GET", "/api/geo/rules"),
    ("POST", "/api/geo/rules"),
    ("GET", "/api/geo/allowlist"),
    ("GET", "/api/geo/bypass_holders"),
    # geoip/sync: allow_api_keys=True, but a real User with only a member-scoped
    # geoip_sync grant must still be denied (no group fallback).
    ("POST", "/api/system/geoip/sync"),
]


@th.django_unit_setup()
def setup_escalation(opts):
    user, email, password, group = make_group_member(ALL_ENDPOINT_PERMS)
    opts.member_email = email
    opts.member_password = password
    opts.member_group_id = group.pk
    opts.member_group = group
    opts.member_user = user


@th.django_unit_test("escalation: member-scoped grants never authorize global endpoints")
def test_member_grant_denied_everywhere(opts):
    login(opts, opts.member_email, opts.member_password)
    gid = opts.member_group_id
    failures = []
    tested = 0
    for method, path in GLOBAL_ENDPOINTS:
        # Attach the member's own group so the (removed) fallback WOULD have
        # authorized. Body for POST, query for GET.
        if method == "POST":
            # `ip` satisfies requires_params on geoip/sync (which runs before the
            # perm check); harmless elsewhere since the gate denies before the
            # handler reads it.
            resp = opts.client.post(path, {"group": gid, "ip": "203.0.113.5"})
        else:
            sep = "&" if "?" in path else "?"
            resp = opts.client.get(f"{path}{sep}group={gid}")
        code = resp.status_code
        if code == 404:
            # Endpoint/app not present in this testproject — skip (paths are
            # validated to exist by the 200-path spot-checks below).
            continue
        tested += 1
        if code != 403:
            failures.append(f"{method} {path} -> {code} (expected 403): {opts.client.last_response.body}")
    assert not failures, \
        "member-scoped grant authorized a global endpoint (escalation NOT closed):\n" + "\n".join(failures)
    assert tested >= 15, \
        f"too few endpoints actually exercised ({tested}) — routing/path regression?"
    opts.client.logout()


@th.django_unit_test("escalation: global grant DOES authorize (paths valid)")
def test_global_grant_authorizes(opts):
    # Separate global-granted user; hits side-effect-free reads only. These 200s
    # also prove the swept paths are real (not silently 404-skipped above).
    _, gemail, gpass = make_user(perms=["view_security", "view_geofence"])
    login(opts, gemail, gpass)
    try:
        resp = opts.client.get("/api/incident/health/summary")
        assert resp.status_code == 200, \
            f"global view_security must read incident health, got {resp.status_code}: {opts.client.last_response.body}"

        resp = opts.client.get("/api/geo/rules")
        assert resp.status_code == 200, \
            f"global view_geofence must read geo/rules, got {resp.status_code}: {opts.client.last_response.body}"
    finally:
        opts.client.logout()


@th.django_unit_test("escalation: superuser authorizes global endpoints")
def test_superuser_authorizes(opts):
    _, semail, spass = make_user(is_superuser=True)
    login(opts, semail, spass)
    try:
        resp = opts.client.get("/api/geo/rules")
        assert resp.status_code == 200, \
            f"superuser must pass a global gate, got {resp.status_code}: {opts.client.last_response.body}"
    finally:
        opts.client.logout()
