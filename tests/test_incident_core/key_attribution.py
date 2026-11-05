"""#9556: incident events from API-key requests are filed under the attributed
person, never under the user whose id equals the key's id."""
import random
import uuid as _uuid

from testit import helpers as th

TESTIT_TIER = "core"

PWORD = "Ky##attr9556x"


def _use_apikey(opts, token):
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = token
    opts.client.is_authenticated = True


def _mk_user(tag, **kw):
    from mojo.apps.account.models import User
    email = f"attr9556_{tag}_{_uuid.uuid4().hex[:6]}@attr9556.test"
    user = User.objects.create_user(username=email, email=email, password=PWORD, **kw)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    return user


def _collide_keys(opts, label):
    """Test-owned users whose ids EQUAL reference-mode ApiKey ids (#9556).

    victim.pk == key.pk (unlinked reference key) and victim2.pk == key2.pk
    (reference key linked to `member`). Ids are forced above every existing
    User and ApiKey id, so nothing else in the database can match them.
    """
    from django.db.models import Max
    from mojo.apps.account.models import User, Group, GroupMember, ApiKey

    tag = _uuid.uuid4().hex[:8]
    opts.tag = tag
    group = Group.objects.create(name=f"attr9556_{label}_{tag}", kind="organization")
    member = _mk_user(tag)
    GroupMember.objects.get_or_create(user=member, group=group)

    top = max(User.objects.aggregate(m=Max("id"))["m"] or 0,
              ApiKey.objects.aggregate(m=Max("id"))["m"] or 0)
    hi = top + 1000 + random.randint(0, 50000)
    victim = _mk_user(tag, id=hi)
    victim2 = _mk_user(tag, id=hi + 1)

    key = ApiKey(id=hi, group=group, name=f"attr9556_k_{tag}", permissions={})
    opts.token = key.generate_token()
    key.save()
    key2 = ApiKey(id=hi + 1, group=group, name=f"attr9556_k2_{tag}", permissions={},
                  user=member, override_user=False)
    opts.token2 = key2.generate_token()
    key2.save()
    okey = ApiKey(group=group, name=f"attr9556_ko_{tag}", permissions={},
                  user=member, override_user=True)
    opts.otoken = okey.generate_token()
    okey.save()

    assert victim.pk == key.pk and victim2.pk == key2.pk, \
        f"setup: forced ids did not collide (V={victim.pk} K={key.pk}, V2={victim2.pk} K2={key2.pk})"
    opts.victim_id, opts.victim2_id = victim.pk, victim2.pk
    opts.member_id, opts.member_email = member.pk, member.email
    opts.key_id, opts.key2_id, opts.okey_id = key.pk, key2.pk, okey.pk
    opts.key_name = key.name


@th.django_unit_setup()
def setup_key_attribution(opts):
    _collide_keys(opts, "inc")


def _denied_events(opts, marker):
    """GET /api/incident/event (denied: the key holds no permissions), drain
    the queued event job, and return the rows this request filed."""
    from mojo.apps.incident.models.event import Event
    from mojo.apps.incident.reporter import QUEUED_EVENT_JOB
    try:
        resp = opts.client.get(f"/api/incident/event?attr9556={marker}")
    finally:
        opts.client.logout()
    for uid in (opts.victim_id, opts.victim2_id, opts.member_id, None):
        th.run_pending_jobs(channel="incident_handlers", func=QUEUED_EVENT_JOB,
                            payload={"uid": uid})
    rows = list(Event.objects.filter(
        metadata__http_query_string=f"attr9556={marker}").values("uid", "metadata"))
    return resp.status_code, rows


@th.django_unit_test("9556: an unlinked reference key's event has no uid and names the key")
def test_unlinked_key_event_uid(opts):
    _use_apikey(opts, opts.token)
    status, rows = _denied_events(opts, f"a{opts.tag}")
    assert status in (401, 403), f"setup: expected a denial, got {status}"
    assert rows, f"no incident event filed for the denial (status {status})"
    for row in rows:
        assert row["uid"] is None, \
            f"event filed under uid {row['uid']}; key id {opts.key_id} == user V {opts.victim_id}, expected no uid"
        assert row["metadata"].get("api_key_id") == opts.key_id, \
            f"metadata.api_key_id should name key {opts.key_id}, got {row['metadata'].get('api_key_id')}"
        assert row["metadata"].get("api_key_name") == opts.key_name, \
            f"metadata.api_key_name should be {opts.key_name}, got {row['metadata'].get('api_key_name')}"


@th.django_unit_test("9556: a linked reference key's event is filed under its member")
def test_linked_key_event_uid(opts):
    _use_apikey(opts, opts.token2)
    status, rows = _denied_events(opts, f"b{opts.tag}")
    assert status in (401, 403), f"setup: expected a denial, got {status}"
    assert rows, f"no incident event filed for the denial (status {status})"
    for row in rows:
        assert row["uid"] == opts.member_id, \
            f"event uid {row['uid']}, expected member {opts.member_id} (V2 with the key's id is {opts.victim2_id})"
        assert row["metadata"].get("api_key_id") == opts.key2_id, \
            f"metadata.api_key_id should name key {opts.key2_id}, got {row['metadata'].get('api_key_id')}"


@th.django_unit_test("9556 guard: a signed-in person's event is still filed under them")
def test_person_event_uid(opts):
    assert opts.client.login(opts.member_email, PWORD), "member login failed"
    status, rows = _denied_events(opts, f"c{opts.tag}")
    assert rows, f"no incident event filed for the person's denial (status {status})"
    for row in rows:
        assert row["uid"] == opts.member_id, \
            f"event uid {row['uid']}, expected the signed-in member {opts.member_id}"
        assert "api_key_id" not in row["metadata"], \
            f"a session request must not carry api_key_id: {row['metadata'].get('api_key_id')}"


@th.django_unit_test("9556: a caller's api_key_id / api_key_name kwargs cannot rename the key")
def test_key_metadata_is_server_set(opts):
    from objict import objict
    from mojo.apps.account.models import ApiKey
    from mojo.apps.incident.reporter import _create_event_dict

    request = objict(META={"QUERY_STRING": "", "SERVER_PROTOCOL": "HTTP/1.1"},
                     ip="127.0.0.1", path="/api/attr9556", method="GET", bearer="apikey")
    user, error = ApiKey.validate_token(opts.token, request)
    assert error is None and user is not None, f"setup: key token did not validate: {error}"
    request.user = user

    data = _create_event_dict("attr9556 collision", category=f"attr9556_{opts.tag}",
                              request=request, api_key_id=-123, api_key_name="forged")
    meta = data["metadata"]
    assert meta.get("api_key_id") == opts.key_id, \
        f"api_key_id must be the server-set key {opts.key_id}, got {meta.get('api_key_id')}"
    assert meta.get("api_key_name") == opts.key_name, \
        f"api_key_name must be the server-set {opts.key_name}, got {meta.get('api_key_name')}"
    assert data["uid"] is None, f"unlinked key must file no uid, got {data['uid']}"
