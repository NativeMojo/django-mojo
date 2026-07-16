"""DM-042: telemetry ingestion is bounded per SESSION (muid), not just per IP.

The doom-loop postmortem's amplifier was a telemetry endpoint doing one DB
write per client failure, limited only by IP — useless against CGNAT and
generous enough for a machine-rate client. bouncer_event now carries
muid_limit=30 (the muid is a server-set HttpOnly cookie, so a client can't
mint a new identity without dropping its session).
"""
import time

from testit import helpers as th


def _wait_for_window_headroom(window, needed):
    now = time.time()
    remaining = window - (now % window)
    if remaining < needed:
        time.sleep(remaining + 0.2)


@th.django_unit_test()
def test_bouncer_event_muid_limit(opts):
    from mojo.decorators.limits import clear_rate_limits
    from mojo.apps.account.models.bouncer_signal import BouncerSignal

    opts.client.logout()
    opts.client.clear_cookies()
    clear_rate_limits(ip="127.0.0.1", key="bouncer_event")
    _wait_for_window_headroom(60, 20)

    payload = {"event_type": "client_error", "data": {"src": "dm042-test"}}

    # First request mints the muid cookie; learn it, then clear its counter so
    # the test owns the full budget.
    resp = opts.client.post("/api/account/bouncer/event", payload)
    assert resp.status_code == 200, f"bouncer event should accept, got {resp.status_code}"
    muid = opts.client.session.cookies.get("_muid")
    assert muid, "server must set the _muid session cookie"
    clear_rate_limits(key="bouncer_event", muid=muid)

    blocked_at = None
    for i in range(31):
        resp = opts.client.post("/api/account/bouncer/event", payload)
        if resp.status_code == 429:
            blocked_at = i + 1
            break
        assert resp.status_code == 200, (
            f"request {i + 1} under the muid limit should pass, got {resp.status_code}"
        )
    assert blocked_at == 31, (
        f"muid limit of 30/min should block exactly the 31st request, blocked at {blocked_at}"
    )

    # Same IP, fresh session cookie: passes — proving the 429 keyed on muid.
    opts.client.clear_cookies()
    resp = opts.client.post("/api/account/bouncer/event", payload)
    assert resp.status_code == 200, (
        f"a fresh session from the same IP must not inherit the muid block, got {resp.status_code}"
    )

    new_muid = opts.client.session.cookies.get("_muid")
    clear_rate_limits(ip="127.0.0.1", key="bouncer_event")
    clear_rate_limits(key="bouncer_event", muid=muid)
    if new_muid:
        clear_rate_limits(key="bouncer_event", muid=new_muid)
    BouncerSignal.objects.filter(muid__in=[muid, new_muid or ""]).delete()
