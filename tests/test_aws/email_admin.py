"""Admin email management: summary/test/default endpoints and the posture service.

Two regressions ride here:

* ``Mailbox.on_rest_saved`` must file an admin audit event naming the REAL
  acting user when a default flag changes through a REST save — before this
  item it filed nothing at all.
* One locked writer owns "exactly one system default": both the new
  ``mailbox-default`` endpoint and a REST Mailbox save must leave exactly one
  ``is_system_default=True`` even from a corrupt two-defaults starting state.

Assertions about the system default are scoped to this module's own domain so
a parallel module exercising ``configure_email`` cannot flake them — the
corrupt pair the regression manufactures lives entirely inside this domain.
"""

import json
from unittest import mock

from testit import helpers as th


DOMAIN = "mailadmin-test.example.com"
PENDING_DOMAIN = "mailadmin-pending.example.com"
ADMIN_EMAIL = "email-admin@test.com"
PLAIN_EMAIL = "email-plain@test.com"
PASSWORD = "example"


def _addr(local, domain=DOMAIN):
    return f"{local}@{domain}"


@th.django_unit_setup()
def setup_email_admin(opts):
    from mojo.apps.account.models import User
    from mojo.apps.aws.models import EmailDomain, Mailbox, SentMessage

    SentMessage.objects.filter(
        mailbox__domain__name__in=(DOMAIN, PENDING_DOMAIN)).delete()
    Mailbox.objects.filter(
        domain__name__in=(DOMAIN, PENDING_DOMAIN)).delete()
    EmailDomain.objects.filter(name__in=(DOMAIN, PENDING_DOMAIN)).delete()
    User.objects.filter(username__in=(ADMIN_EMAIL, PLAIN_EMAIL)).delete()

    admin = User.objects.create_user(
        email=ADMIN_EMAIL, username=ADMIN_EMAIL, password=PASSWORD)
    admin.is_active = True
    admin.save()
    admin.add_permission("manage_aws")
    admin.save()
    plain = User.objects.create_user(
        email=PLAIN_EMAIL, username=PLAIN_EMAIL, password=PASSWORD)
    plain.is_active = True
    plain.save()
    opts.admin_pk = admin.pk

    verified = EmailDomain.objects.create(
        name=DOMAIN, status="verified", can_send=True)
    pending = EmailDomain.objects.create(name=PENDING_DOMAIN, status="pending")
    opts.verified_pk = verified.pk
    opts.pending_pk = pending.pk

    sender = Mailbox.objects.create(
        domain=verified, email=_addr("sender"), allow_outbound=True)
    noreply = Mailbox.objects.create(
        domain=verified, email=_addr("noreply"), allow_outbound=False)
    alt = Mailbox.objects.create(
        domain=verified, email=_addr("alt"), allow_outbound=True)
    unverified = Mailbox.objects.create(
        domain=pending, email=_addr("box", PENDING_DOMAIN), allow_outbound=True)
    opts.sender_pk = sender.pk
    opts.noreply_pk = noreply.pk
    opts.alt_pk = alt.pk
    opts.unverified_pk = unverified.pk


def _my_defaults():
    from mojo.apps.aws.models import Mailbox
    return list(Mailbox.objects.filter(
        is_system_default=True, domain__name=DOMAIN
    ).order_by("email").values_list("email", flat=True))


# ── regressions ─────────────────────────────────────────────────────────────

@th.django_unit_test("REGRESSION: a REST default change audits the real actor")
def test_rest_save_audits_real_actor(opts):
    from mojo.apps.account.models import User
    from mojo.apps.aws.models import Mailbox
    from mojo.apps.incident.models import Event
    from mojo.apps.incident import reporter

    user = User.objects.get(username=ADMIN_EMAIL)
    Mailbox.objects.filter(domain__name=DOMAIN).update(is_system_default=False)
    Event.objects.filter(category="admin_platform",
                         details__contains=_addr("alt")).delete()
    # Clear the once-per-hour suppression so THIS run files its own event.
    try:
        from mojo.helpers.redis import get_connection
        get_connection().delete(reporter.notice_key(
            "admin_platform",
            f"admin-platform:mailbox_default:{user.pk}:{_addr('alt')}"))
    except Exception:
        pass

    assert opts.client.login(ADMIN_EMAIL, PASSWORD), \
        "the manage_aws fixture could not establish a session"
    try:
        resp = opts.client.post(f"/api/aws/email/mailbox/{opts.alt_pk}",
                                {"is_system_default": True})
        assert resp.status_code == 200, \
            f"REST mailbox save failed: {resp.status_code}"

        row = Event.objects.filter(
            category="admin_platform", details__contains="mailbox_default"
        ).filter(details__contains=_addr("alt")).order_by("-id").first()
        assert row is not None, \
            "a REST default-flag change filed no admin audit event"
        assert f"actor={user.pk}" in (row.details or ""), \
            f"the audit event does not name the real acting user: {row.details!r}"
    finally:
        opts.client.logout()
        # Never leave a live system default behind (maestro #2789): a stray
        # is_system_default row makes every later send_template_email in the
        # whole run resolve a real SES mailbox.
        Mailbox.objects.filter(domain__name=DOMAIN).update(is_system_default=False)


@th.django_unit_test("REGRESSION: one locked writer leaves exactly one system default")
def test_single_locked_default_writer(opts):
    from mojo.apps.aws.models import Mailbox

    # Manufacture the corrupt state the locked writer must collapse: two
    # mailboxes in this domain both claim the system default.
    Mailbox.objects.filter(domain__name=DOMAIN).update(is_system_default=False)
    Mailbox.objects.filter(pk__in=(opts.sender_pk, opts.noreply_pk)).update(
        is_system_default=True)

    assert opts.client.login(ADMIN_EMAIL, PASSWORD), \
        "the manage_aws fixture could not establish a session"
    try:
        # Path 1: the dedicated endpoint.
        resp = opts.client.post("/api/aws/email/mailbox-default",
                                {"mailbox": opts.alt_pk, "scope": "system"})
        assert resp.status_code == 200, \
            f"mailbox-default endpoint missing or refused: {resp.status_code}"
        assert _my_defaults() == [_addr("alt")], \
            f"the endpoint left more or less than one default: {_my_defaults()}"

        # Path 2: a plain REST Mailbox save with the flag.
        resp = opts.client.post(f"/api/aws/email/mailbox/{opts.sender_pk}",
                                {"is_system_default": True})
        assert resp.status_code == 200, \
            f"REST mailbox save failed: {resp.status_code}"
        assert _my_defaults() == [_addr("sender")], \
            f"a REST save left more or less than one default: {_my_defaults()}"
    finally:
        opts.client.logout()
        # See test_rest_save_audits_real_actor — no live default may survive.
        Mailbox.objects.filter(domain__name=DOMAIN).update(is_system_default=False)


# ── test-send: every foreseeable failure is a structured 200 ────────────────

@th.django_unit_test("test_send answers every failure with a structured error")
def test_send_failure_branches(opts):
    from mojo.apps.aws.services import email_admin

    cases = [
        (dict(from_email="", to="x@example.org", subject="s"),
         "invalid_request"),
        (dict(from_email=_addr("ghost"), to="x@example.org", subject="s"),
         "mailbox_not_found"),
        (dict(from_email=_addr("noreply"), to="x@example.org", subject="s"),
         "outbound_not_allowed"),
        (dict(from_email=_addr("box", PENDING_DOMAIN), to="x@example.org",
              subject="s"), "domain_not_verified"),
        (dict(from_email=_addr("sender"), to="", subject="s"),
         "invalid_request"),
        (dict(from_email=_addr("sender"), to="x@example.org"),
         "invalid_request"),
    ]
    for kwargs, code in cases:
        result = email_admin.test_send(**kwargs)
        assert result.get("sent") is False, \
            f"{code}: test_send claimed success for {kwargs}: {result}"
        assert result.get("error_code") == code, \
            f"expected error_code {code} for {kwargs}, got {result}"
        assert result.get("error"), f"{code}: no plain-words error: {result}"


@th.django_unit_test("test_send reports an SES-side failure as sent+failed, never raises")
def test_send_ses_failure_and_success(opts):
    from mojo.apps.aws.services import email as email_service
    from mojo.apps.aws.services import email_admin

    failing = mock.Mock()
    failing.send_email.return_value = {"Error": "MessageRejected"}
    with mock.patch.object(email_service, "_get_sender", return_value=failing):
        result = email_admin.test_send(
            from_email=_addr("sender"), to="x@example.org", subject="hello",
            body_text="hello")
    assert result.get("sent") is True and result.get("status") == "failed", \
        f"an SES-side failure must come back as a failed SentMessage: {result}"
    assert result.get("status_reason"), f"no failure reason surfaced: {result}"

    ok = mock.Mock()
    ok.send_email.return_value = {"MessageId": "test-message-id"}
    with mock.patch.object(email_service, "_get_sender", return_value=ok):
        result = email_admin.test_send(
            from_email=_addr("sender"), to="x@example.org", subject="hello",
            body_text="hello")
    assert result.get("sent") is True and result.get("message_id") == "test-message-id", \
        f"a successful send did not surface its message id: {result}"


@th.django_unit_test("POST /api/aws/email/test never answers 500")
def test_http_test_send_never_500s(opts):
    assert opts.client.login(ADMIN_EMAIL, PASSWORD), \
        "the manage_aws fixture could not establish a session"
    try:
        for payload, code in (
            ({"from_email": _addr("ghost"), "to": "x@example.org",
              "subject": "s"}, "mailbox_not_found"),
            ({"from_email": _addr("noreply"), "to": "x@example.org",
              "subject": "s"}, "outbound_not_allowed"),
            ({"from_email": _addr("box", PENDING_DOMAIN),
              "to": "x@example.org", "subject": "s"}, "domain_not_verified"),
            ({"from_email": _addr("sender"), "to": "", "subject": "s"},
             "invalid_request"),
        ):
            resp = opts.client.post("/api/aws/email/test", payload)
            assert resp.status_code == 200, \
                f"{code}: test send answered {resp.status_code}, not 200"
            data = (resp.json or {}).get("data") or {}
            assert data.get("sent") is False and data.get("error_code") == code, \
                f"{code}: no structured error in {data}"
    finally:
        opts.client.logout()


# ── summary ─────────────────────────────────────────────────────────────────

@th.django_unit_test("the email summary never emits credential material")
def test_summary_never_leaks_credentials(opts):
    from mojo.apps.aws.models import EmailDomain
    from mojo.apps.aws.services import email_admin

    domain = EmailDomain.objects.get(pk=opts.verified_pk)
    domain.set_aws_key("AKIAFAKEFAKEFAKEFAKE")
    domain.set_aws_secret("secretsecretFAKEFAKEFAKEFAKE")
    domain.save()

    rendered = json.dumps(email_admin.summarize())
    for banned in ("aws_key", "aws_secret", "AKIAFAKEFAKEFAKEFAKE",
                   "secretsecretFAKEFAKEFAKEFAKE", "masked"):
        assert banned not in rendered, \
            f"the summary leaked credential material: {banned}"

    assert opts.client.login(ADMIN_EMAIL, PASSWORD), \
        "the manage_aws fixture could not establish a session"
    try:
        resp = opts.client.get("/api/aws/email/summary")
        assert resp.status_code == 200, \
            f"manage_aws could not read the summary: {resp.status_code}"
        data = (resp.json or {}).get("data") or {}
        names = [row["name"] for row in data.get("domains") or []]
        assert DOMAIN in names, f"the summary omitted this module's domain: {names}"
        assert "AKIAFAKEFAKEFAKEFAKE" not in json.dumps(data), \
            "the HTTP summary leaked the AWS key"
    finally:
        opts.client.logout()


@th.django_unit_test("email admin endpoints refuse callers outside the tier")
def test_email_admin_permissions(opts):
    resp = opts.client.get("/api/aws/email/summary")
    assert resp.status_code in (401, 403), \
        f"anonymous summary answered {resp.status_code}"
    assert opts.client.login(PLAIN_EMAIL, PASSWORD), \
        "the unprivileged fixture could not establish a session"
    try:
        for method, path, payload in (
                ("GET", "/api/aws/email/summary", None),
                ("POST", "/api/aws/email/test",
                 {"from_email": _addr("sender"), "to": "x@example.org",
                  "subject": "s"}),
                ("POST", "/api/aws/email/mailbox-default",
                 {"mailbox": opts.alt_pk, "scope": "system"})):
            if method == "GET":
                resp = opts.client.get(path)
            else:
                resp = opts.client.post(path, payload)
            assert resp.status_code in (401, 403), \
                f"{path} answered {resp.status_code} without the email tier"
    finally:
        opts.client.logout()


# ── dashboard source ────────────────────────────────────────────────────────

@th.django_unit_test("dashboard_source states: unconfigured/degraded/healthy, never unhealthy")
def test_dashboard_source_states(opts):
    from mojo.apps.aws.models import EmailDomain, Mailbox
    from mojo.apps.aws.services import email_admin

    try:
        _run_dashboard_source_states(opts, EmailDomain, Mailbox, email_admin)
    finally:
        # See test_rest_save_audits_real_actor — no live default may survive.
        Mailbox.objects.filter(domain__name=DOMAIN).update(is_system_default=False)


def _run_dashboard_source_states(opts, EmailDomain, Mailbox, email_admin):
    seen = []

    with mock.patch.object(email_admin, "_domains", return_value=[]):
        value = email_admin.dashboard_source()
    seen.append(value)
    assert value.get("_collector_status") == "unconfigured" \
        and value.get("configured") is False, \
        f"no domains must read unconfigured: {value}"

    pending = EmailDomain.objects.get(pk=opts.pending_pk)
    with mock.patch.object(email_admin, "_domains", return_value=[pending]):
        value = email_admin.dashboard_source()
    seen.append(value)
    assert value.get("_collector_status") == "degraded" \
        and value.get("_collector_reason") == "no_sendable_domain", \
        f"an unaudited pending domain must read degraded: {value}"

    verified = EmailDomain.objects.get(pk=opts.verified_pk)
    Mailbox.objects.filter(domain__name=DOMAIN).update(is_system_default=False)
    Mailbox.objects.filter(pk__in=(opts.sender_pk, opts.alt_pk)).update(
        is_system_default=True)
    with mock.patch.object(email_admin, "_domains", return_value=[verified]):
        value = email_admin.dashboard_source()
    seen.append(value)
    assert value.get("_collector_status") == "degraded" \
        and value.get("_collector_reason") == "default_sender_conflict", \
        f"two system defaults must read as a named conflict: {value}"

    # Healthy needs the global posture green: one default, templates present.
    Mailbox.objects.filter(is_system_default=True).update(is_system_default=False)
    Mailbox.objects.filter(pk=opts.sender_pk).update(is_system_default=True)
    with mock.patch.object(email_admin, "_domains", return_value=[verified]), \
            mock.patch("mojo.apps.aws.services.email_templates.shipped_status",
                       return_value={"total": 0, "missing": []}):
        value = email_admin.dashboard_source()
    seen.append(value)
    assert value.get("_collector_status") == "healthy", \
        f"a verified sendable domain with one default must be healthy: {value}"
    assert value.get("default_sender") == _addr("sender"), \
        f"the healthy row does not name its default sender: {value}"

    for value in seen:
        assert value.get("_collector_status") != "unhealthy", \
            f"the email source must never claim an outage: {value}"


@th.django_unit_test("email_posture is byte-identical to the settings resolver")
def test_email_posture_matches_settings_resolver(opts):
    from mojo.apps.account.services import admin_settings
    from mojo.apps.aws.services import email_admin

    state = admin_settings._email_posture_state(None, None)
    assert state[0] == email_admin.email_posture(), \
        f"the settings resolver diverged from email_posture(): {state[0]}"
    assert state[1] == "computed" and state[2] is False, \
        f"the resolver tuple contract changed: {state!r}"
