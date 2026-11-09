"""#9556: a reference-mode API key cannot read or write metrics account
user-<id> of the user whose id equals the key's id."""
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
def setup_user_account_keys(opts):
    _collide_keys(opts, "met")


def _statuses(opts, user_id):
    slug = f"attr9556_{opts.tag}"
    try:
        fetch = opts.client.get(f"/api/metrics/fetch?account=user-{user_id}&slug={slug}").status_code
        record = opts.client.post("/api/metrics/record",
                                  {"slug": slug, "account": f"user-{user_id}"}).status_code
    finally:
        opts.client.logout()
    return fetch, record


@th.django_unit_test("9556: an unlinked reference key cannot read or write user-<key id>")
def test_unlinked_key_denied(opts):
    _use_apikey(opts, opts.token)
    fetch, record = _statuses(opts, opts.victim_id)
    assert fetch in (401, 403) and record in (401, 403), \
        f"key {opts.key_id} reached user-{opts.victim_id}: fetch={fetch} record={record}"


@th.django_unit_test("9556: a linked reference key cannot read or write user-<key id>")
def test_linked_key_denied(opts):
    _use_apikey(opts, opts.token2)
    fetch, record = _statuses(opts, opts.victim2_id)
    assert fetch in (401, 403) and record in (401, 403), \
        f"key {opts.key2_id} reached user-{opts.victim2_id}: fetch={fetch} record={record}"


@th.django_unit_test("9556 guard: a signed-in person keeps their own metrics account")
def test_person_own_account(opts):
    assert opts.client.login(opts.member_email, PWORD), "member login failed"
    fetch, record = _statuses(opts, opts.member_id)
    assert fetch == 200 and record == 200, \
        f"member {opts.member_id} lost their own account: fetch={fetch} record={record}"


@th.django_unit_test("9556 guard: an override key keeps its member's own metrics account")
def test_override_key_own_account(opts):
    _use_apikey(opts, opts.otoken)
    fetch, record = _statuses(opts, opts.member_id)
    assert fetch == 200 and record == 200, \
        f"override key {opts.okey_id} lost member {opts.member_id}'s account: fetch={fetch} record={record}"
