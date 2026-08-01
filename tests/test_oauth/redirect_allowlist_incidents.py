"""The OAuth redirect-allowlist diagnostics are Redis-suppressed incidents (#1098).

Two `logit.warning` sites on the public `@md.public_endpoint()` `/begin` used to
be attacker-amplifiable: `_warn_unusable_entry` (one line per unusable entry) and
the refusal line in `_validate_redirect_uri` (one line per refused request). Both
are reachable for free by an anonymous caller — `group.metadata`
(`allowed_redirect_urls`) is `manage_group`-writable and `request.group` is
anonymously selectable via `?group=` / `?group_uuid=`. They now file through
`incident.report_event_suppressed`: Redis-suppressed, budgeted, never-raising.

Three categories, three postures (see `docs/django_developer/account/oauth.md`):

  * `auth:redirect_allowlist_tenant_entry_unusable` — a tenant list with unusable
    entries. Level 1, keyed by `group:<pk>`, budgeted (25 groups/hour),
    fail-closed. The tenant provenance is the amplifiable one.
  * `auth:redirect_allowlist_unusable_entry` — a broken DEPLOYMENT entry
    (`ALLOWED_REDIRECT_URLS`). Level 3, keyed by source name, no budget (one
    source name is self-bounding), fail-open. An operator config bug.
  * `auth:oauth_redirect_refused` — a refused `redirect_uri`. Level 3, keyed by
    host, budgeted (50 hosts/hour), fail-closed.

Every request here is ANONYMOUS — a signed-out visitor on a white-label login
page is exactly who calls `/begin`. The package pins `ALLOWED_REDIRECT_URLS` to
`["https://example.com/"]`, so tests that do not touch it need no server reload
(parallel-safe); the one test that sets it uses a strict-superset `Setting` row
removed in `finally`, matching `redirect_uri.py`.
"""
from urllib.parse import quote

from testit import helpers as th

PROVIDER = "google"
GROUP_PREFIX = "oauthinc_"

# Hosts each test refuses. Kept distinct so `details__contains` filters never
# cross between tests (or between parallel packages).
UNRELATED_HOST = "unrelated-probe.example.net"
REFUSED_HOST = "refused-probe.example.org"


def _clear_keys(keys):
    """Best-effort delete of Redis suppression keys, each in its own guard."""
    from mojo.helpers.redis import get_connection

    try:
        redis = get_connection()
    except Exception:
        return
    for k in keys:
        try:
            redis.delete(k)
        except Exception:
            pass


@th.django_unit_setup()
def setup_redirect_allowlist_incidents(opts):
    from mojo.apps.account.models.group import Group
    from mojo.apps.incident.models import Event
    from mojo.apps.incident import notice_key, budget_key
    from mojo.apps.account.services import redirect_allowlist as ra

    # Long-lived DB: delete this module's own rows before creating them.
    Group.objects.filter(name__startswith=GROUP_PREFIX).delete()
    Event.objects.filter(category__in=[
        ra.CATEGORY_TENANT_ENTRY,
        ra.CATEGORY_UNUSABLE_ENTRY,
        ra.CATEGORY_REDIRECT_REFUSED,
    ]).delete()

    # 300 tenant entries, none with a scheme (each is unusable) and none carrying
    # a `.`/`..` segment (which #1101 refuses even earlier). This is the shape a
    # manage_group holder can write into `metadata["allowed_redirect_urls"]`.
    junk = Group.objects.create(name=f"{GROUP_PREFIX}junk", kind="organization")
    junk.metadata = {"allowed_redirect_urls": [f"junk-entry-{i}" for i in range(300)]}
    junk.save()
    # create() leaves uuid lazily assigned — materialize it before use.
    opts.junk_uuid = junk.get_uuid()
    opts.junk_pk = junk.pk

    # Clear the precise suppression keys this module exercises (each guarded).
    _clear_keys([
        notice_key(ra.CATEGORY_TENANT_ENTRY, f"group:{opts.junk_pk}"),
        notice_key(ra.CATEGORY_UNUSABLE_ENTRY, "ALLOWED_REDIRECT_URLS"),
        notice_key(ra.CATEGORY_REDIRECT_REFUSED, REFUSED_HOST),
        notice_key(ra.CATEGORY_REDIRECT_REFUSED, UNRELATED_HOST),
        budget_key(ra.CATEGORY_TENANT_ENTRY, 3600),
        budget_key(ra.CATEGORY_REDIRECT_REFUSED, 3600),
    ])


def _begin(opts, redirect_uri, group_param=None):
    """Anonymous GET /begin with a redirect_uri and optional group selection."""
    url = (f"/api/auth/oauth/{PROVIDER}/begin"
           f"?redirect_uri={quote(redirect_uri, safe='')}")
    if group_param:
        url = f"{url}&{group_param}"
    return opts.client.get(url)


@th.django_unit_test("oauth: a junk tenant list files ONE bounded tenant incident")
def test_junk_group_list_through_begin_files_one_bounded_incident(opts):
    """The keystone regression. A tenant writes 300 junk entries; any anonymous
    caller amplifies. Before #1098 each junk entry emitted a `logit.warning` per
    request; now the whole list files exactly ONE suppressed tenant incident, and
    that incident never lands in the OPERATOR category.

    At HEAD this fails: the two sites were `logit.warning`, so ZERO events are
    filed in this category (which did not exist) — the count is 0, not 1.
    """
    from mojo.apps.incident.models import Event
    from mojo.apps.incident import notice_key, budget_key
    from mojo.apps.account.services import redirect_allowlist as ra

    _clear_keys([
        notice_key(ra.CATEGORY_TENANT_ENTRY, f"group:{opts.junk_pk}"),
        budget_key(ra.CATEGORY_TENANT_ENTRY, 3600),
    ])

    unrelated = f"https://{UNRELATED_HOST}/landing"
    resp = _begin(opts, unrelated, f"group_uuid={opts.junk_uuid}")
    assert resp.status_code == 400, (
        f"an unrelated redirect_uri against a 300-entry junk tenant list must be "
        f"refused with 400, got {resp.status_code}: {resp.response}")

    tenant = list(Event.objects.filter(
        category=ra.CATEGORY_TENANT_ENTRY, group_id=opts.junk_pk))
    assert len(tenant) == 1, (
        f"a tenant list with 300 unusable entries must file EXACTLY ONE tenant "
        f"incident, not one per entry, got {len(tenant)}")
    assert "300" in (tenant[0].details or ""), (
        f"the tenant incident must name the true total (300) even though only a "
        f"handful of samples are quoted, got details={tenant[0].details!r}")

    # Two more identical requests: suppressed per group per window, so the count
    # must stay at one — the tenant provenance cannot amplify.
    for _ in range(2):
        again = _begin(opts, unrelated, f"group_uuid={opts.junk_uuid}")
        assert again.status_code == 400, (
            f"repeat junk-list requests must still refuse, got {again.status_code}")
    assert Event.objects.filter(
        category=ra.CATEGORY_TENANT_ENTRY, group_id=opts.junk_pk).count() == 1, (
        "the tenant incident is suppressed per group per window — three identical "
        "requests must still yield exactly one incident")

    # Tenant junk must NEVER surface in the operator (deployment) category.
    assert Event.objects.filter(
        category=ra.CATEGORY_UNUSABLE_ENTRY,
        details__contains="junk-entry-").count() == 0, (
        "a tenant's unusable entries must file under the TENANT category only, "
        "never the operator's deployment category")

    # This module owns these rows; drop them so counts stay stable across reruns.
    Event.objects.filter(
        category=ra.CATEGORY_TENANT_ENTRY, group_id=opts.junk_pk).delete()
    Event.objects.filter(
        category=ra.CATEGORY_REDIRECT_REFUSED,
        details__contains=UNRELATED_HOST).delete()


@th.django_unit_test("oauth: a broken DEPLOYMENT entry still signals the operator")
def test_broken_deployment_entry_still_signals_the_operator(opts):
    """A broken entry in the deployment `ALLOWED_REDIRECT_URLS` is an operator
    bug, not a tenant one — it files the higher-severity operator category while
    the request itself still succeeds against the usable sibling entry."""
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.incident.models import Event
    from mojo.apps.incident import notice_key
    from mojo.apps.account.services import redirect_allowlist as ra

    _clear_keys([notice_key(ra.CATEGORY_UNUSABLE_ENTRY, "ALLOWED_REDIRECT_URLS")])
    Event.objects.filter(
        category=ra.CATEGORY_UNUSABLE_ENTRY, details__contains="'h'").delete()

    allowed = "https://example.com/"  # matches the pinned entry itself
    # A strict SUPERSET of the pinned entry (so a parallel package's begin still
    # matches during the window) PLUS a broken "h". DB-first + live to the server.
    Setting.set("ALLOWED_REDIRECT_URLS", '["https://example.com/", "h"]')
    try:
        resp = _begin(opts, allowed)
        assert resp.status_code == 200, (
            f"the pinned allowed URI must still begin normally with a broken "
            f"sibling entry present, got {resp.status_code}: {resp.response}")

        events = list(Event.objects.filter(
            category=ra.CATEGORY_UNUSABLE_ENTRY, details__contains="'h'"))
        assert len(events) == 1, (
            f"the broken deployment entry 'h' must file exactly one operator "
            f"incident, got {len(events)}: {[e.details for e in events]}")
        assert events[0].level == 3, (
            f"the operator (deployment) unusable-entry incident is level 3, got "
            f"level {events[0].level}")
    finally:
        Setting.remove("ALLOWED_REDIRECT_URLS")
        Event.objects.filter(
            category=ra.CATEGORY_UNUSABLE_ENTRY, details__contains="'h'").delete()


@th.django_unit_test("oauth: a refused redirect_uri files ONE incident per host")
def test_refused_redirect_uri_files_one_incident_per_host(opts):
    """The refusal diagnostic. One incident per host per window — a second
    refusal on the same host (different path) inside the window is suppressed, so
    an anonymous caller cannot mint one incident per crafted path."""
    from mojo.apps.incident.models import Event
    from mojo.apps.incident import notice_key, budget_key
    from mojo.apps.account.services import redirect_allowlist as ra

    _clear_keys([
        notice_key(ra.CATEGORY_REDIRECT_REFUSED, REFUSED_HOST),
        budget_key(ra.CATEGORY_REDIRECT_REFUSED, 3600),
    ])
    Event.objects.filter(
        category=ra.CATEGORY_REDIRECT_REFUSED,
        details__contains=REFUSED_HOST).delete()

    resp = _begin(opts, f"https://{REFUSED_HOST}/landing")
    assert resp.status_code == 400, (
        f"a redirect_uri on no allowlist must be refused with 400, got "
        f"{resp.status_code}: {resp.response}")

    refused = list(Event.objects.filter(
        category=ra.CATEGORY_REDIRECT_REFUSED, details__contains=REFUSED_HOST))
    assert len(refused) == 1, (
        f"a refused redirect_uri must file exactly one incident naming its host, "
        f"got {len(refused)}")
    assert REFUSED_HOST in (refused[0].details or ""), (
        f"the refusal incident must name the refused host so an operator can "
        f"allowlist or block it, got {refused[0].details!r}")

    # Same host, different path: the host is the suppression unit.
    again = _begin(opts, f"https://{REFUSED_HOST}/other-path")
    assert again.status_code == 400, (
        f"the second refusal must still be a 400, got {again.status_code}")
    assert Event.objects.filter(
        category=ra.CATEGORY_REDIRECT_REFUSED,
        details__contains=REFUSED_HOST).count() == 1, (
        "a second refusal on the same host inside the window must not file a "
        "second incident — the host is the unit, not the path")

    Event.objects.filter(
        category=ra.CATEGORY_REDIRECT_REFUSED,
        details__contains=REFUSED_HOST).delete()
