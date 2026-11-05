"""#9556: Log rows from API-key requests carry the attributed person's id, never
the id of the user whose id equals the key's id."""
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
    _collide_keys(opts, "log")


def _hit(opts):
    try:
        opts.client.get(f"/api/metrics/fetch?account=public&slug=attr9556_{opts.tag}")
    finally:
        opts.client.logout()


@th.django_unit_test("9556: an unlinked reference key's Log rows carry uid 0 and the key's username")
def test_unlinked_key_log_uid(opts):
    from mojo.apps.logit.models import Log
    _use_apikey(opts, opts.token)
    _hit(opts)
    rows = list(Log.objects.filter(username=f"apikey:{opts.key_id}").values_list("uid", "path"))
    assert rows, f"no Log rows written for key {opts.key_id} (is LOGIT_DB_ALL on?)"
    assert all(uid == 0 for uid, _ in rows), \
        f"Log.uid should be 0 for an unlinked key; got {rows[:5]} (user V with the key's id is {opts.victim_id})"


@th.django_unit_test("9556: a linked reference key's Log rows carry its member's id")
def test_linked_key_log_uid(opts):
    from mojo.apps.logit.models import Log
    _use_apikey(opts, opts.token2)
    _hit(opts)
    rows = list(Log.objects.filter(username=f"apikey:{opts.key2_id}").values_list("uid", "path"))
    assert rows, f"no Log rows written for key {opts.key2_id}"
    assert all(uid == opts.member_id for uid, _ in rows), \
        f"Log.uid should be member {opts.member_id}; got {rows[:5]} (V2 with the key's id is {opts.victim2_id})"
