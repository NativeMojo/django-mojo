"""#4125: browser crash and debug reports, POST /api/incident/event/client.

Works without signing in, allowlists and bounds the body, owns the sender's
identity and address, and publishes each accepted report through the RuleSet
pipeline exactly once. Every test uses its own frontend:testit_* category and
its own handler-less RuleSet, so publication is provable and no job,
notification, firewall action or LLM call runs.
"""
import uuid as _uuid

from testit import helpers as th

TESTIT_TIER = "core"

PWORD = "Crash##report4125x"
ROUTE = "/api/incident/event/client"


def _mk_user(tag, label):
    from mojo.apps.account.models import User
    email = f"cr4125_{label}_{tag}@cr4125.test"
    User.objects.filter(email=email).delete()
    user = User.objects.create_user(username=email, email=email, password=PWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    return user


def _ruleset(category):
    """A test-owned, handler-less RuleSet on one frontend:testit_* category."""
    from mojo.apps.incident.models import RuleSet
    RuleSet.objects.filter(category=category).delete()
    return RuleSet.objects.create(
        name=f"cr4125 {category}", category=category, priority=1,
        handler=None, is_active=True, bundle_minutes=0)


def _no_web_rulesets():
    from mojo.apps.incident.models import RuleSet
    found = list(RuleSet.objects.filter(category="web", is_active=True).values_list("pk", flat=True))
    assert not found, f"an active 'web' RuleSet ({found}) would catch these reports first; tests cannot prove publication"


def _events(category):
    from mojo.apps.incident.models.event import Event
    return list(Event.objects.filter(category=category).order_by("id"))


def _category(opts, label):
    return f"frontend:testit_{opts.tag}_{label}"


@th.django_unit_setup()
def setup_client_report(opts):
    from mojo.apps.account.models import Group, GroupMember
    from mojo.decorators.limits import clear_rate_limits
    tag = _uuid.uuid4().hex[:10]
    opts.tag = tag
    opts.player = _mk_user(tag, "player")
    opts.other = _mk_user(tag, "other")
    opts.group = Group.objects.create(name=f"cr4125_own_{tag}", kind="organization")
    opts.foreign = Group.objects.create(name=f"cr4125_foreign_{tag}", kind="organization")
    GroupMember.objects.get_or_create(user=opts.player, group=opts.group)
    clear_rate_limits(ip="127.0.0.1", key="incident_client_report")


@th.django_unit_test("4125 T1: an anonymous crash report is stored and published once")
def test_anonymous_report_creates_and_publishes(opts):
    _no_web_rulesets()
    category = _category(opts, "t1")
    rule_set = _ruleset(category)
    opts.client.logout()
    resp = opts.client.post(ROUTE, {
        "category": category, "severity": "error", "message": "TypeError: x is undefined",
        "metadata": {"stack_trace": "TypeError: x\n  at a (app.js:1:2)", "app": "clubaxo",
                     "environment": "staging", "build": "b1", "reference": f"ref-{opts.tag}",
                     "page_path": "/play?token=secret#frag", "filename": "app.js",
                     "lineno": 1, "colno": 2, "component_stack": "in App"},
    })
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.response}"
    assert dict(resp.response) == {"status": True}, f"receipt must be exactly status true, got {resp.response}"
    rows = _events(category)
    assert len(rows) == 1, f"expected one event, got {len(rows)}"
    event = rows[0]
    assert event.scope == "web", f"scope must be forced to web, got {event.scope}"
    assert event.uid is None, f"an anonymous report has no uid, got {event.uid}"
    meta = event.metadata
    assert (meta.get("reporter") or {}).get("credential_kind") == "unknown", \
        f"metadata.reporter.credential_kind must be unknown: {meta.get('reporter')}"
    assert meta.get("stack_trace", "").startswith("TypeError: x"), f"stack_trace lost: {meta.get('stack_trace')}"
    assert meta.get("component_stack") == "in App", "component_stack lost"
    assert meta.get("reference") == f"ref-{opts.tag}", "reference lost"
    assert meta.get("page_path") == "/play", f"query and fragment must be stripped: {meta.get('page_path')}"
    assert meta.get("lineno") == 1 and meta.get("colno") == 2, "line/column lost"
    event.refresh_from_db()
    assert event.incident_id is not None and event.incident.rule_set_id == rule_set.pk, \
        "the report was not published to its RuleSet"


@th.django_unit_test("4125 T2: a signed-in player's report keeps the real identity, not the claims")
def test_player_report_publishes_with_identity(opts):
    _no_web_rulesets()
    category = _category(opts, "t2")
    rule_set = _ruleset(category)
    assert opts.client.login(opts.player.email, PWORD), "player login failed"
    try:
        resp = opts.client.post(ROUTE, {
            "category": category, "message": "boom", "uid": opts.other.pk,
            "source_ip": "203.0.113.9", "scope": "global", "level": 15,
        })
    finally:
        opts.client.logout()
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.response}"
    rows = _events(category)
    assert len(rows) == 1, f"expected one event, got {len(rows)}"
    event = rows[0]
    assert event.uid == opts.player.pk, f"uid must be the player {opts.player.pk}, got {event.uid}"
    assert event.source_ip != "203.0.113.9", "a claimed source_ip must not be stored"
    assert event.scope == "web", f"scope must stay web, got {event.scope}"
    assert 1 <= event.level <= 6, f"level must be clamped to 1..6, got {event.level}"
    assert event.metadata.get("claimed_level") == 15, \
        f"the sent level must be kept as claimed_level: {event.metadata.get('claimed_level')}"
    event.refresh_from_db()
    assert event.incident_id is not None and event.incident.rule_set_id == rule_set.pk, \
        "the report was not published to its RuleSet"


@th.django_unit_test("4125 T3: only a trusted group is stamped; others are claims")
def test_group_claims(opts):
    _no_web_rulesets()
    own, foreign, anon = (_category(opts, x) for x in ("t3own", "t3foreign", "t3anon"))
    for category in (own, foreign, anon):
        _ruleset(category)
    assert opts.client.login(opts.player.email, PWORD), "player login failed"
    try:
        r1 = opts.client.post(ROUTE, {"category": own, "message": "own"}, params={"group": opts.group.pk})
        r2 = opts.client.post(ROUTE, {"category": foreign, "message": "foreign"}, params={"group": opts.foreign.pk})
    finally:
        opts.client.logout()
    r3 = opts.client.post(ROUTE, {"category": anon, "message": "anon"}, params={"group": opts.group.pk})
    for resp in (r1, r2, r3):
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.response}"
    e_own, e_foreign, e_anon = (_events(c)[0] for c in (own, foreign, anon))
    assert e_own.group_id == opts.group.pk, f"the player's own group must be stamped, got {e_own.group_id}"
    assert e_foreign.group_id is None, f"a foreign group must not be stamped, got {e_foreign.group_id}"
    assert e_foreign.metadata.get("claimed_group_id") == opts.foreign.pk, "foreign group must be a claim"
    assert e_anon.group_id is None, f"an anonymous ?group= must not be stamped, got {e_anon.group_id}"
    assert e_anon.metadata.get("claimed_group_id") == opts.group.pk, "anonymous group must be a claim"


@th.django_unit_test("4125 T4: a non-frontend category is refused and files nothing")
def test_invalid_category_refused_without_incident(opts):
    from mojo.apps.incident.models.event import Event
    opts.client.logout()
    marker = f"cr4125t4{opts.tag}"
    for category in ("invalid_password", "security:bouncer:honeypot_post", "frontend:BAD CAPS"):
        before = Event.objects.filter(category=category, metadata__http_query_string=f"m={marker}").count()
        resp = opts.client.post(f"{ROUTE}?m={marker}", {"category": category, "message": "x"})
        assert resp.status_code == 400, f"{category!r} must be refused with 400, got {resp.status_code}"
        after = Event.objects.filter(category=category, metadata__http_query_string=f"m={marker}").count()
        assert after == before, f"a refused {category!r} report stored an event"
    errors = Event.objects.filter(metadata__http_query_string=f"m={marker}").exclude(
        category__startswith="frontend:").count()
    assert errors == 0, f"a refusal filed {errors} error event(s) of its own"


@th.django_unit_test("4125 T5: bounds and the allowlist are enforced")
def test_bounds_and_allowlist(opts):
    category = _category(opts, "t5")
    _ruleset(category)
    opts.client.logout()
    resp = opts.client.post(ROUTE, {
        "category": category, "message": "m" * 5000, "secret_field": "drop me",
        "metadata": {"stack_trace": "s" * 20000, "app": "a" * 100, "lineno": "12",
                     "colno": 99999999999, "filename": "https://x.test/app.js?v=1", "unlisted": 1},
    })
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.response}"
    event = _events(category)[0]
    meta = event.metadata
    assert len(event.details) <= 2000, f"message must be cut to 2000, got {len(event.details)}"
    assert len(event.title) <= 200, f"title must be cut to 200, got {len(event.title)}"
    assert len(meta.get("stack_trace", "")) <= 16384, f"stack_trace must be cut to 16 KB, got {len(meta.get('stack_trace', ''))}"
    assert len(meta.get("app", "")) <= 64, "app must be cut to 64"
    assert "lineno" not in meta and "colno" not in meta, "out-of-range or non-int line/col must be dropped"
    assert meta.get("filename") == "https://x.test/app.js", f"filename query must be stripped: {meta.get('filename')}"
    assert "unlisted" not in meta and "secret_field" not in meta, "unknown keys must be dropped"
    big_category = _category(opts, "t5big")
    big = opts.client.post(ROUTE, {"category": big_category, "message": "x" * (70 * 1024)})
    assert big.status_code == 413, f"a 70 KB body must be refused with 413, got {big.status_code}"
    assert not _events(big_category), "a refused oversize report stored an event"


@th.django_unit_test("4125 T6: the level clamp follows the incident threshold")
def test_level_clamp_follows_threshold(opts):
    from mojo.apps.incident.services import client_report
    norm = client_report.normalize_level
    assert norm("error", threshold=7) == (5, None), f"error -> 5, got {norm('error', threshold=7)}"
    assert norm("fatal", threshold=7) == (6, None), "fatal -> 6"
    assert norm(15, threshold=7) == (6, 15), f"15 clamps to 6 with a claim, got {norm(15, threshold=7)}"
    assert norm(9, threshold=5) == (4, 9), f"threshold 5 caps at 4, got {norm(9, threshold=5)}"
    assert norm("nonsense", threshold=7) == (5, "nonsense"), "a bad value defaults to 5 and is kept as a claim"
    assert norm(None, threshold=7) == (5, None), "a missing value defaults to 5"
    assert norm(True, threshold=7)[0] == 5, "a bool is not a level"
    assert norm(0, threshold=7) == (1, 0), "a level under 1 is raised to 1"
