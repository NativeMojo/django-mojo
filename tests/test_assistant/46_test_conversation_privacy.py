"""Conversation history stays private across every REST read shape."""
import json

from testit import helpers as th
from testit.client import RestClient


TESTIT_TIER = "core"
PREFIX = "conversation-privacy-2588"
PASSWORD = "Conversation-Privacy-2588!"
ENDPOINT = "/api/assistant/conversation"
AUDIT_KIND = "assistant:conversation_read"


@th.django_unit_setup()
def setup_conversation_privacy(opts):
    from mojo.apps.account.models import ApiKey, Group, GroupMember, User
    from mojo.apps.assistant.models import Conversation, Message

    emails = [f"{PREFIX}-{role}@example.com" for role in ("owner", "other", "admin")]
    ApiKey.objects.filter(name=PREFIX).delete()
    User.objects.filter(username__in=emails).delete()
    Group.objects.filter(name=PREFIX).delete()
    opts.privacy_group = Group.objects.create(name=PREFIX, kind="organization")
    users = []
    for email in emails:
        user = User.objects.create_user(username=email, email=email, password=PASSWORD)
        user.is_active = user.is_email_verified = True
        user.requires_mfa = False
        user.permissions = {"assistant": True}
        user.save()
        users.append(user)
    opts.privacy_owner, opts.privacy_other, opts.privacy_admin = users
    opts.privacy_admin.add_permission("view_admin")
    # A tenant-local administrative grant must not become global oversight.
    membership = GroupMember.objects.create(user=opts.privacy_owner, group=opts.privacy_group)
    membership.add_permission("view_admin")
    opts.privacy_own = Conversation.objects.create(
        user=opts.privacy_owner, group=opts.privacy_group, title=f"{PREFIX}-own-title")
    opts.privacy_foreign = Conversation.objects.create(
        user=opts.privacy_other, group=opts.privacy_group, title=f"{PREFIX}-foreign-title",
        metadata={"private": f"{PREFIX}-metadata"})
    opts.privacy_own_text = f"{PREFIX}-own-message"
    opts.privacy_foreign_text = f"{PREFIX}-foreign-message"
    Message.objects.create(conversation=opts.privacy_own, role="user", content=opts.privacy_own_text)
    Message.objects.create(conversation=opts.privacy_foreign, role="user", content=opts.privacy_foreign_text)
    _key, opts.privacy_key = ApiKey.create_for_group(
        group=opts.privacy_group, name=PREFIX, permissions={"assistant": True},
        user=opts.privacy_owner, override_user=True)


def _login(opts, user):
    assert opts.client.login(user.username, PASSWORD), "privacy fixture user must authenticate"


def _rows(response):
    assert response.status_code == 200, f"conversation list must succeed: {response.status_code}"
    rows = response.json.get("data")
    assert isinstance(rows, list), "conversation list must return a data array"
    return rows


def _assert_no_foreign(opts, response):
    body = json.dumps(response.json) if response.get("json") else response.get("text", "")
    assert opts.privacy_foreign.title not in body, "foreign conversation title must stay private"
    assert opts.privacy_foreign_text not in body, "foreign messages must stay private"


@th.django_unit_test("assistant grant lists only the caller's conversations")
def test_owner_list_is_private(opts):
    _login(opts, opts.privacy_owner)
    for params in ({}, {"graph": "detail"}, {"group": opts.privacy_group.pk}):
        response = opts.client.get(ENDPOINT, params=params)
        ids = {row["id"] for row in _rows(response)}
        assert ids == {opts.privacy_own.pk}, "assistant/group grants must not widen the owner list"
        _assert_no_foreign(opts, response)


@th.django_unit_test("explicit foreign filters cannot replace conversation ownership")
def test_foreign_filters_cannot_widen_scope(opts):
    _login(opts, opts.privacy_owner)
    for params in (
            {"user": opts.privacy_other.pk},
            {"user__in": f"{opts.privacy_owner.pk},{opts.privacy_other.pk}"},
            {"id": opts.privacy_foreign.pk},
            {"group": opts.privacy_group.pk, "user": opts.privacy_other.pk}):
        response = opts.client.get(ENDPOINT, params=params)
        ids = {row["id"] for row in _rows(response)}
        assert ids <= {opts.privacy_own.pk}, "query filters must intersect the canonical owner scope"
        _assert_no_foreign(opts, response)


@th.django_unit_test("only the owner can read ordinary conversation details and messages")
def test_owner_detail_and_foreign_denial(opts):
    _login(opts, opts.privacy_owner)
    own = opts.client.get(f"{ENDPOINT}/{opts.privacy_own.pk}", params={"graph": "detail"})
    assert own.status_code == 200, "owner must retain conversation detail access"
    assert opts.privacy_own_text in json.dumps(own.json), "owner detail must retain message history"
    for params in ({"graph": "detail"}, {"graph": "detail", "group": opts.privacy_group.pk}):
        foreign = opts.client.get(f"{ENDPOINT}/{opts.privacy_foreign.pk}", params=params)
        assert foreign.status_code in (403, 404), "foreign detail must deny assistant and tenant admin grants"
        _assert_no_foreign(opts, foreign)
    context = opts.client.post("/api/assistant/context", {
        "model": "assistant.Conversation", "pk": opts.privacy_foreign.pk,
    })
    assert context.status_code == 403, "assistant access must not import another user's conversation through context"
    _assert_no_foreign(opts, context)


@th.django_unit_test("conversation counts and stat bundles use the same owner scope")
def test_count_and_stats_are_private(opts):
    _login(opts, opts.privacy_owner)
    response = opts.client.get(ENDPOINT, params={
        "_mode": "count",
        "_stats": json.dumps({"mine": {"user": opts.privacy_owner.pk},
                              "theirs": {"user": opts.privacy_other.pk}}),
    })
    assert response.status_code == 200, "owner aggregation must remain available"
    assert response.json.get("count") == 1, "count must exclude foreign conversations"
    assert response.json.get("stats") == {"mine": 1, "theirs": 0}, "stats cannot count another user's history"
    foreign = opts.client.get(ENDPOINT, params={"_mode": "count", "user": opts.privacy_other.pk})
    assert foreign.status_code == 200 and foreign.json.get("count") == 0, "explicit user filters cannot bypass count scoping"


@th.django_unit_test("CSV conversation export cannot bypass owner filtering")
def test_export_is_private(opts):
    _login(opts, opts.privacy_owner)
    response = opts.client.get(ENDPOINT, params={"download_format": "csv"})
    assert response.status_code == 200, "owner CSV export must remain available"
    assert opts.privacy_own.title in response.get("text", ""), "export must include the owner's title"
    _assert_no_foreign(opts, response)


@th.django_unit_test("global view_admin oversight is audited without conversation contents")
def test_global_oversight_is_audited(opts):
    from mojo.apps.logit.models import Log

    _login(opts, opts.privacy_admin)
    for path, params in (
            (ENDPOINT, {"user": opts.privacy_other.pk, "group": opts.privacy_group.pk}),
            (f"{ENDPOINT}/{opts.privacy_foreign.pk}", {"graph": "detail"})):
        previous = Log.objects.filter(kind=AUDIT_KIND, uid=opts.privacy_admin.pk).order_by("-pk").first()
        after_id = previous.pk if previous else 0
        response = opts.client.get(path, params=params)
        assert response.status_code == 200, "global view_admin must retain foreign history oversight"
        assert opts.privacy_foreign.title in json.dumps(response.json), "oversight must return the selected foreign conversation"
        audits = list(Log.objects.filter(kind=AUDIT_KIND, uid=opts.privacy_admin.pk, pk__gt=after_id))
        assert audits, "each foreign list/detail read must emit an attributable oversight audit"
        for audit in audits:
            assert audit.gid == 0, "oversight audits must not inherit the caller's tenant group"
            logged = f"{audit.log or ''} {audit.payload or ''}"
            assert opts.privacy_foreign.title not in logged, "oversight audit must not copy conversation titles"
            assert opts.privacy_foreign_text not in logged, "oversight audit must not copy messages"
            assert f"{PREFIX}-metadata" not in logged, "oversight audit must not copy metadata"


@th.django_unit_test("a key assuming the owner's identity cannot read private conversations")
def test_key_backed_owner_is_denied(opts):
    client = RestClient(opts.client.host)
    client.bearer, client.access_token, client.is_authenticated = "apikey", opts.privacy_key, True
    try:
        for path in (ENDPOINT, f"{ENDPOINT}/{opts.privacy_own.pk}", f"{ENDPOINT}/{opts.privacy_foreign.pk}"):
            response = client.get(path, params={"graph": "detail"})
            if path == ENDPOINT and response.status_code == 200:
                assert _rows(response) == [], "a confined key must never inherit an owner's list access"
            else:
                assert response.status_code in (403, 404), "a confined key must not inherit owner detail access"
            _assert_no_foreign(opts, response)
    finally:
        client.session.close()
