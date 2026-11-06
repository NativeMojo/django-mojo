"""#4125: a submitted security event is stamped by the server.

POST /api/incident/event stays open to any signed-in caller, but the server
owns who sent it and from where. What the sender claims (another user, another
address, another company) is kept as flat ``claimed_*`` metadata for the rules
to judge, never as the trusted columns, and never in anyone's security history.
"""
import uuid as _uuid

from testit import helpers as th

TESTIT_TIER = "core"

PWORD = "Sub##event4125x"


def _mk_user(tag, label, **kw):
    from mojo.apps.account.models import User
    email = f"ev4125_{label}_{tag}@ev4125.test"
    User.objects.filter(email=email).delete()
    user = User.objects.create_user(username=email, email=email, password=PWORD, **kw)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    return user


def _ruleset(category, rules=()):
    """A test-owned RuleSet on one category, with no handler."""
    from mojo.apps.incident.models import RuleSet, Rule
    RuleSet.objects.filter(category=category).delete()
    rule_set = RuleSet.objects.create(
        name=f"ev4125 {category}", category=category, priority=1,
        handler=None, is_active=True, bundle_minutes=0)
    for index, (field, comparator, value, value_type) in enumerate(rules):
        Rule.objects.create(parent=rule_set, name=f"r{index}", index=index,
                            field_name=field, comparator=comparator,
                            value=str(value), value_type=value_type)
    return rule_set


def _incident_rule_set(event):
    event.refresh_from_db()
    if event.incident_id is None:
        return None
    return event.incident.rule_set_id


@th.django_unit_setup()
def setup_submitted_event(opts):
    from mojo.apps.account.models import Group, GroupMember, ApiKey
    tag = _uuid.uuid4().hex[:10]
    opts.tag = tag
    opts.member = _mk_user(tag, "member")
    opts.victim = _mk_user(tag, "victim")
    opts.operator = _mk_user(tag, "operator")
    opts.operator.add_permission("security")
    opts.group = Group.objects.create(name=f"ev4125_own_{tag}", kind="organization")
    opts.foreign = Group.objects.create(name=f"ev4125_foreign_{tag}", kind="organization")
    GroupMember.objects.get_or_create(user=opts.member, group=opts.group)
    key = ApiKey(group=opts.group, name=f"ev4125_k_{tag}", permissions={},
                 user=opts.member, override_user=False)
    opts.key_token = key.generate_token()
    key.save()
    opts.key_id = key.pk


def _post(opts, body, params=None):
    return opts.client.post("/api/incident/event", body, params=params or {})


def _event_for(category):
    from mojo.apps.incident.models.event import Event
    return Event.objects.filter(category=category).order_by("-id").first()


@th.django_unit_test("4125 T7: a member's submitted event is server-stamped and keeps claims")
def test_member_submit_stamps_reporter_keeps_claims(opts):
    category = f"testit:{opts.tag}:t7"
    rule_set = _ruleset(category, [("claimed_uid", "==", opts.victim.pk, "int")])
    assert opts.client.login(opts.member.email, PWORD), "member login failed"
    try:
        resp = _post(opts, {
            "category": category, "scope": f"testit_{opts.tag}", "level": 12,
            "title": "t7", "uid": opts.victim.pk, "source_ip": "203.0.113.9",
            "metadata": {"request_ip": "1.2.3.4", "x": 1},
        }, params={"group": opts.foreign.pk})
    finally:
        opts.client.logout()
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.response}"
    body = resp.response
    assert body.get("status") is True, f"receipt should be status true: {body}"
    data = body.get("data") or {}
    assert set(data.keys()) == {"id"}, f"receipt data must hold only the id, got {dict(data)}"
    event = _event_for(category)
    assert event is not None and event.pk == data["id"], f"receipt id {data.get('id')} names no event"
    meta = event.metadata
    assert event.uid == opts.member.pk, f"uid must be the sender {opts.member.pk}, got {event.uid}"
    assert event.source_ip == meta.get("request_ip"), \
        f"source_ip must be the request ip {meta.get('request_ip')}, got {event.source_ip}"
    assert event.source_ip != "203.0.113.9", "the claimed source_ip must not be stored as fact"
    assert event.group_id is None, f"an untrusted ?group= must not be stamped, got {event.group_id}"
    assert meta.get("claimed_uid") == opts.victim.pk, f"claimed_uid lost: {meta.get('claimed_uid')}"
    assert meta.get("claimed_source_ip") == "203.0.113.9", f"claimed_source_ip lost: {meta.get('claimed_source_ip')}"
    assert meta.get("claimed_group_id") == opts.foreign.pk, f"claimed_group_id lost: {meta.get('claimed_group_id')}"
    assert meta.get("claimed_request_ip") == "1.2.3.4", f"claimed_request_ip lost: {meta.get('claimed_request_ip')}"
    assert meta.get("request_ip") != "1.2.3.4", "a client request_ip must not replace the server's"
    assert (meta.get("reporter") or {}).get("credential_kind") == "user", \
        f"metadata.reporter must name a user credential: {meta.get('reporter')}"
    assert event.level == 12, f"level is kept as the claimed severity, got {event.level}"
    assert meta.get("x") == 1, f"ordinary client metadata must be kept, got {meta.get('x')}"
    assert _incident_rule_set(event) == rule_set.pk, \
        "the event was not published: the claimed_uid rule did not open an incident"


@th.django_unit_test("4125 T8: an API key's event is confined and attributed to its member")
def test_api_key_submit_confined_and_attributed(opts):
    category = f"testit:{opts.tag}:t8"
    _ruleset(category)
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = opts.key_token
    opts.client.is_authenticated = True
    try:
        resp = _post(opts, {"category": category, "scope": f"testit_{opts.tag}", "uid": opts.victim.pk})
        foreign = _post(opts, {"category": category, "scope": f"testit_{opts.tag}"},
                        params={"group": opts.foreign.pk})
    finally:
        opts.client.logout()
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.response}"
    event = _event_for(category)
    assert event is not None, "no event was created for the key"
    assert event.group_id == opts.group.pk, f"group must be the key's own {opts.group.pk}, got {event.group_id}"
    assert event.uid == opts.member.pk, f"uid must be the key's member {opts.member.pk}, got {event.uid}"
    assert event.uid != opts.key_id or opts.key_id == opts.member.pk, "uid must never be the key's pk"
    assert event.metadata.get("api_key_id") == opts.key_id, \
        f"metadata.api_key_id must name the key {opts.key_id}: {event.metadata.get('api_key_id')}"
    assert event.metadata.get("claimed_uid") == opts.victim.pk, "the key's uid claim must be kept"
    assert foreign.status_code == 403, f"a foreign ?group= must stay 403 for a key, got {foreign.status_code}"


@th.django_unit_test("4125 T9: security history ignores submitted events")
def test_security_history_ignores_submitted(opts):
    from mojo.apps.incident import reporter
    submitted = f"login:testit_{opts.tag}_sub"
    filed = f"login:testit_{opts.tag}_rep"
    assert opts.client.login(opts.member.email, PWORD), "member login failed"
    try:
        resp = _post(opts, {"category": submitted, "scope": f"testit_{opts.tag}", "uid": opts.victim.pk})
        assert resp.status_code == 200, f"setup: submit failed {resp.status_code}: {resp.response}"
        mine = opts.client.get("/api/account/security-events", params={"size": 100})
    finally:
        opts.client.logout()
    reporter.record_event("ev4125 reporter-filed", category=filed, uid=opts.victim.pk)
    assert opts.client.login(opts.victim.email, PWORD), "victim login failed"
    try:
        theirs = opts.client.get("/api/account/security-events", params={"size": 100})
    finally:
        opts.client.logout()
    kinds_mine = [row.get("kind") for row in (mine.response.get("data") or [])]
    kinds_theirs = [row.get("kind") for row in (theirs.response.get("data") or [])]
    assert submitted not in kinds_theirs, "a submitted event appeared in the named victim's security history"
    assert submitted not in kinds_mine, "a submitted event appeared in the submitter's security history"
    assert filed in kinds_theirs, f"a reporter-filed event must still appear in history: {kinds_theirs[:10]}"


@th.django_unit_test("4125 T10: the governed rule writer accepts the claimed_* fields")
def test_governed_schema_accepts_claims(opts):
    from mojo.apps.incident.services import rule_validation
    payload = {
        "name": "ev4125 claims", "category": f"testit_{opts.tag}",
        "rules": [
            {"field": "claimed_uid", "operator": "==", "value": 5},
            {"field": "claimed_source_ip", "operator": "==", "value": "203.0.113.9"},
            {"field": "claimed_group_id", "operator": "==", "value": 7},
        ],
    }
    try:
        normalized = rule_validation.normalize_ruleset(payload)
    except Exception as exc:
        raise AssertionError(f"governed writer refused the claimed_* fields: {exc}")
    fields = [row.get("field_name", row.get("field")) for row in normalized["rules"]]
    assert fields == ["claimed_uid", "claimed_source_ip", "claimed_group_id"], \
        f"claim fields not normalized as given: {fields}"


@th.django_unit_test("4125 T11 guard: anonymous create stays refused; bad bodies are 400; operator reads unchanged")
def test_anonymous_and_operator_unchanged(opts):
    category = f"testit:{opts.tag}:t11"
    opts.client.logout()
    anon = _post(opts, {"category": category})
    anon_group = _post(opts, {"category": category}, params={"group": opts.group.pk})
    anon_get = opts.client.get("/api/incident/event")
    assert anon.status_code == 401, f"anonymous create must be 401, got {anon.status_code}"
    assert anon_group.status_code == 403, f"anonymous create with ?group= must be 403, got {anon_group.status_code}"
    assert anon_get.status_code == 401, f"anonymous list must be 401, got {anon_get.status_code}"
    from mojo.apps.incident import reporter
    from mojo.apps.incident.models.event import Event
    assert not Event.objects.filter(category=category).exists(), "an anonymous create stored a row"
    assert opts.client.login(opts.member.email, PWORD), "member login failed"
    try:
        huge = _post(opts, {"category": category, "level": 2**40})
        no_category = _post(opts, {"level": 1})
    finally:
        opts.client.logout()
    assert huge.status_code == 400, f"an out-of-range level must be 400, not {huge.status_code}"
    assert no_category.status_code == 400, f"a missing category must be 400, not {no_category.status_code}"
    row = reporter.record_event("ev4125 operator row", category=category)
    assert opts.client.login(opts.operator.email, PWORD), "operator login failed"
    try:
        listed = opts.client.get("/api/incident/event", params={"category": category})
        saved = opts.client.post(f"/api/incident/event/{row.pk}", {"title": f"ev4125 {opts.tag}"})
    finally:
        opts.client.logout()
    assert listed.status_code == 200, f"operator list must stay 200, got {listed.status_code}"
    assert saved.status_code == 200, f"operator pk save must stay 200, got {saved.status_code}: {saved.response}"
    row.refresh_from_db()
    assert row.title == f"ev4125 {opts.tag}", f"operator pk save did not apply: {row.title}"
