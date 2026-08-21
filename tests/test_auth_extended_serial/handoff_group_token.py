"""
Gated auth-handoff destinations — the IN-PROCESS gating cluster.

Moved from tests/test_auth/handoff_group_token.py (maestro item #1839): every
test here drives the gating module through the `_gating` contextmanager, which
mutates django.conf.settings process-wide — unsafe under the parallel default
tier. The endpoint blocks (which configure the separate server process through
th.server_settings) stayed behind.
"""
import contextlib

from testit import helpers as th
from testit.helpers import assert_true, assert_eq


def assert_false(value, msg):
    """Local mirror of assert_true — most of this module is "must be refused",
    and `assert_true(not x, ...)` reads backwards for every one of them.
    testit.helpers has no assert_false today (same helper as handoff.py)."""
    assert not value, msg


MEMBER = "hgt_member"
OUTSIDER = "hgt_outsider"
SUPERUSER = "hgt_super"
TEMP = "hgt_temp"
PWORD = "hgt##mojo99"

GATED_HOST = "gated.hgt.example.net"
UNGATED_HOST = "plain.hgt.example.net"
UNGATED_DEST = f"https://{UNGATED_HOST}/"

# The EXACT AUTH_HANDOFF_ALLOWED_URLS entries reload block 1 installs (kept in
# sync with tests/test_auth/handoff_group_token.py — the in-process deny-rule
# matrix below is a strict SUPERSET of the endpoint arm's).
BLOCK1_GATED_DESTS = [
    (f"https://{GATED_HOST}/", "the exact host"),
    (f"http://{GATED_HOST}/", "an http:// destination under a scheme-less entry"),
    (f"https://{GATED_HOST}:8443/", "a non-default port"),
    (f"https://{GATED_HOST}./", "a trailing dot"),
    (f"https://deep.sub.{GATED_HOST}/", "two extra labels of subdomain depth"),
    (f"HTTPS://{GATED_HOST.upper()}/", "an uppercase scheme and host"),
    (f"https://{GATED_HOST}/deep/path?q=1", "a path and query under the host"),
]

# Module-level state the resolver fixtures read. They are addressed by the name
# testit loads this module under (tests/ is on sys.path), so load_function()
# resolves the SAME module object.
_STATE = {}


def _resolver_group_a(url, request=None):
    from mojo.apps.account.models import Group
    return Group.objects.filter(pk=_STATE.get("group_a_id")).first()


def _resolver_none(url, request=None):
    return None


def _resolver_unknown_uuid(url, request=None):
    return "hgt-no-such-group-uuid"


def _resolver_junk(url, request=None):
    return ["not", "a", "group"]


def _resolver_raises(url, request=None):
    raise RuntimeError("gating resolver blew up")


@contextlib.contextmanager
def _gating(mode=None, hosts=None, resolver=None):
    """Point the IN-PROCESS gating module at a specific configuration.

    In-process only: opts.client talks to a separate server process which keeps
    the test project's settings, so this never leaks into endpoint tests.
    Modeled on handoff.py's `_allowlist`.
    """
    from django.conf import settings as django_settings
    from mojo.apps.account.services import handoff_group

    values = {
        "AUTH_HANDOFF_GROUP_TOKEN_MODE": mode or handoff_group.MODE_OFF,
        "AUTH_HANDOFF_GROUP_TOKEN_HOSTS": dict(hosts or {}),
        "AUTH_HANDOFF_GROUP_TOKEN_RESOLVER": resolver or "",
    }
    missing = object()
    previous = {key: getattr(django_settings, key, missing) for key in values}
    for key, value in values.items():
        setattr(django_settings, key, value)
    handoff_group._reset_cache_for_tests()
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is missing:
                delattr(django_settings, key)
            else:
                setattr(django_settings, key, old)
        handoff_group._reset_cache_for_tests()


def _clear_limits():
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    for key in ("auth_handoff", "auth_exchange", "login", "refresh_token"):
        clear_rate_limits(ip="127.0.0.1", key=key)


@th.django_unit_setup()
def setup_gating(opts):
    from mojo.apps.account.models import User, Group

    _clear_limits()

    # Long-lived DB: delete before creating. A `hgt_` prefix keeps this module
    # clear of tests/test_auth/group_token.py, whose setup deletes every `gt_`
    # user and group and which can run in a sibling thread.
    User.objects.filter(username__startswith="hgt_").delete()
    Group.objects.filter(name__startswith="hgt_").delete()

    group_a = Group.objects.create(name="hgt_tenant_a", kind="organization")
    child_a = Group.objects.create(name="hgt_child_a", kind="organization",
                                   parent=group_a)
    inactive = Group.objects.create(name="hgt_inactive", kind="organization",
                                    is_active=False)
    dark_parent = Group.objects.create(name="hgt_dark_parent", kind="organization",
                                       is_active=False)
    dark_child = Group.objects.create(name="hgt_dark_child", kind="organization",
                                      parent=dark_parent)

    def _user(username, **kwargs):
        user = User(username=username, email=f"{username}@example.com",
                    display_name=username, **kwargs)
        user.save()
        user.is_email_verified = True
        user.is_active = True
        user.save_password(PWORD)
        user.save()
        return user

    member = _user(MEMBER)
    outsider = _user(OUTSIDER)
    superu = _user(SUPERUSER, is_superuser=True)
    temp = _user(TEMP)

    group_a.add_member(member)
    group_a.add_member(superu)
    group_a.add_member(temp)

    opts.group_a_id = group_a.pk
    opts.group_a_uuid = group_a.get_uuid()
    opts.child_a_id = child_a.pk
    opts.child_a_uuid = child_a.get_uuid()
    opts.inactive_uuid = inactive.get_uuid()
    opts.dark_child_uuid = dark_child.get_uuid()
    opts.member_id = member.pk
    opts.outsider_id = outsider.pk
    opts.super_id = superu.pk
    opts.temp_id = temp.pk
    _STATE["group_a_id"] = group_a.pk


@th.django_unit_test("gating: mode normalizes, and garbage is treated as enforce")
def test_mode_normalization(opts):
    from mojo.apps.account.services import handoff_group as hg

    with _gating():
        assert_eq(hg.get_mode(), hg.MODE_OFF,
                  "an unset mode must default to 'off' — this feature ships inert")
        assert_false(hg.is_enforcing(), "'off' must never enforce")

    with _gating(mode="MoNiToR"):
        assert_eq(hg.get_mode(), hg.MODE_MONITOR,
                  "the mode must be case- and whitespace-insensitive")

    with _gating(mode="  enforce "):
        assert_eq(hg.get_mode(), hg.MODE_ENFORCE, "'  enforce ' must normalize")

    with _gating(mode="enfroce"):
        assert_eq(hg.get_mode(), hg.MODE_ENFORCE,
                  "a typo in a SECURITY switch must fail closed — an unknown "
                  "mode string is treated as enforce, never as off")


@th.django_unit_test("gating: configured-but-off is inert, and says so")
def test_configured_but_off_is_inert(opts):
    from mojo.apps.incident.models import Event
    from mojo.apps.account.services import handoff_group as hg
    from mojo.helpers.redis import get_connection

    category = "auth:handoff_group_token_inert"
    Event.objects.filter(category=category).delete()
    try:
        get_connection().delete(hg._notice_key("inert"))
    except Exception:
        pass

    # A deliberately fatal map: if 'off' consulted the sources at all, this
    # would refuse rather than sit inert.
    with _gating(mode="off", hosts={"192.0.2.7": opts.group_a_uuid}):
        assert_eq(hg.get_mode(), hg.MODE_OFF,
                  "a configured map must not turn gating on by itself")
        assert_true(hg._CACHE.get(hg._WARNED_INERT),
                    "a configured-but-off gating map is the obvious footgun and "
                    "must be warned about exactly once")
        assert_true(hg.prerequisite_ok(),
                    "'off' must never trip the destination-allowlist "
                    "prerequisite — an inert map cannot break a handoff")
        assert_false(hg.is_enforcing(),
                     "nothing binds while the mode is off, however broken the "
                     "map is. (resolve_group() is deliberately mode-agnostic — "
                     "the mode is checked by its caller, which short-circuits "
                     "before the sources are ever compiled. Section E pins that "
                     "end to end against the shipped defaults.)")

        # The inert footgun is now a suppressed incident, not just a file log.
        events = list(Event.objects.filter(category=category))
        assert_eq(len(events), 1,
                  f"configured-but-off must file exactly one inert-map incident, "
                  f"got {[e.title for e in events]}")
        assert_eq(events[0].level, 6,
                  f"a security control that is switched off while configured on "
                  f"is a level-6 incident, got {events[0].level}")
        assert_true("AUTH_HANDOFF_GROUP_TOKEN_MODE" in (events[0].details or ""),
                    f"the incident must name the mode switch that is off: "
                    f"{events[0].details!r}")

        # Survives the in-process one-shot drop: _reset_cache_for_tests() clears
        # the _WARNED_INERT flag (as a fresh worker/process would), but the Redis
        # suppression key persists, so a second get_mode() does NOT file again —
        # the "survives restarts/workers" property the incident buys over a
        # per-process warning.
        hg._reset_cache_for_tests()
        assert_eq(hg.get_mode(), hg.MODE_OFF, "still inert after the cache drop")
        events = list(Event.objects.filter(category=category))
        assert_eq(len(events), 1,
                  f"the inert incident survives an in-process cache drop — the "
                  f"Redis key keeps it to one per window across workers and "
                  f"restarts, got {len(events)}")
    Event.objects.filter(category=category).delete()


@th.django_unit_test("gating: enforce hard-requires destination-allowlist enforcement")
def test_prerequisite_requires_allowlist(opts):
    from mojo.apps.account.services import handoff_group as hg
    # The mutating allowlist helpers moved with the in-process allowlist tests
    # to the opt-in serial package (maestro item #1839).
    from tests.test_auth_extended_serial.handoff import _allowlist, _unconfigured

    with _gating(mode="enforce", hosts={GATED_HOST: opts.group_a_uuid}):
        with _unconfigured():
            assert_false(hg.prerequisite_ok(),
                         "gating enforce with the destination allowlist in "
                         "monitor mode cannot deliver its property: a deny map "
                         "cannot enumerate 'every host that is not mine', so an "
                         "unmapped host collects a full JWT pair")
        with _allowlist([f"https://{GATED_HOST}/"]):
            assert_true(hg.prerequisite_ok(),
                        "with the allowlist enforced the prerequisite is met")

    with _gating(mode="monitor", hosts={GATED_HOST: opts.group_a_uuid}):
        with _unconfigured():
            assert_true(hg.prerequisite_ok(),
                        "monitor must NEVER require the prerequisite — that is "
                        "what makes a monitor rehearsal safe")


@th.django_unit_test("gating: the deny rule catches every C2 bypass shape")
def test_deny_rule_gates_every_bypass_shape(opts):
    from mojo.apps.account.services import handoff_group as hg

    must_gate = [d for d, _ in BLOCK1_GATED_DESTS] + [
        f"https://{GATED_HOST}:80/app",
        f"//{GATED_HOST}/x",
        f"https://a.b.c.{GATED_HOST}/",
    ]

    for entry, why in ((GATED_HOST, "a bare host entry"),
                       (f"*.{GATED_HOST}", "a *.-prefixed entry")):
        with _gating(mode="enforce", hosts={entry: opts.group_a_uuid}):
            for dest in must_gate:
                ok, group = hg.resolve_group(dest)
                assert_true(ok, f"{dest!r} must resolve cleanly under {why}")
                assert_true(group is not None and group.pk == opts.group_a_id,
                            f"{dest!r} MUST be gated by {entry!r} ({why}) — an "
                            f"ungated answer here mints a plain JWT whose code "
                            f"lands right back on the gated origin. Got {group!r}")


@th.django_unit_test("gating: a host we cannot read unambiguously is REFUSED, not ignored")
def test_suspicious_hosts_fail_closed(opts):
    from mojo.apps.account.services import handoff_group as hg

    suspicious = [
        (f"https://{GATED_HOST}\\@evil.tld/",
         "a backslash — Python keeps it inside the host, a browser treats it as "
         "an authority terminator and navigates to evil.tld"),
        ("https://gätéd.example.com/",
         "a unicode IDN label — list IDN destinations in punycode"),
        ("https://[2001:db8::1]/", "a bracketed IPv6 literal"),
        ("https://192.0.2.7/",
         "a dotted-quad IP literal — no entry can ever BE an IP form, so "
         "treating this as 'matches nothing' would be fail-OPEN for a "
         "deployment whose gated box is an internal address"),
        ("https://3221226007/", "a decimal IP literal"),
        ("http://localhost:3000/", "a single-label host"),
    ]
    with _gating(mode="enforce", hosts={GATED_HOST: opts.group_a_uuid}):
        for dest, why in suspicious:
            ok, group = hg.resolve_group(dest)
            assert_false(ok,
                         f"{dest!r} ({why}) must be reported SUSPICIOUS so the "
                         f"caller refuses — the inversion of the allowlist's "
                         f"'no match', which fails open here. Got ok={ok}")
            assert_true(group is None,
                        f"a suspicious destination must never carry a group, got {group!r}")


@th.django_unit_test("gating: a host that is NOT the gated one is not gated")
def test_confusable_hosts_are_not_gated(opts):
    from mojo.apps.account.services import handoff_group as hg

    with _gating(mode="enforce", hosts={GATED_HOST: opts.group_a_uuid}):
        ok, host = hg.host_of(f"https://{GATED_HOST}@evil.tld/")
        assert_true(ok and host == "evil.tld",
                    f"userinfo: the token is going to evil.tld, not to "
                    f"{GATED_HOST} — gating it would be gating the wrong host, "
                    f"and the destination allowlist is what refuses this one. "
                    f"Got ok={ok} host={host!r}")

        for dest, why in (
                (f"https://{GATED_HOST}@evil.tld/", "userinfo confusable"),
                (f"https://{GATED_HOST}.evil.tld/", "a suffix-extended host"),
                ("https://notgated.example.com/", "an unrelated host")):
            ok, group = hg.resolve_group(dest)
            assert_true(ok, f"{dest!r} ({why}) is parsable and must resolve cleanly")
            assert_true(group is None,
                        f"{dest!r} ({why}) must NOT be gated — over-gating a "
                        f"foreign host would refuse or scope a legitimate "
                        f"destination. Got {group!r}")


@th.django_unit_test("gating: a defective entry refuses EVERY handoff, it is never dropped")
def test_defective_entries_refuse_everything(opts):
    from mojo.apps.account.services import handoff_group as hg

    good = f"https://{GATED_HOST}/"
    defective = [
        ("192.0.2.7", "a dotted-quad IP entry"),
        ("3221226007", "a decimal IP entry"),
        ("0xC0000207", "a hex IP entry"),
        ("0300.0.2.7", "an octal-ish IP entry"),
        ("[2001:db8::1]", "a bracketed IPv6 entry"),
        ("localhost", "a single-label entry"),
    ]
    for entry, why in defective:
        # Paired with a VALID entry: the defect must refuse the good
        # destination too. A dropped entry is a silent hole; a fatal one is a
        # loud, correctable outage.
        with _gating(mode="enforce",
                     hosts={GATED_HOST: opts.group_a_uuid, entry: opts.group_a_uuid}):
            ok, group = hg.resolve_group(good)
            assert_false(ok,
                         f"{entry!r} ({why}) is not a usable deny entry — we do "
                         f"not normalize numeric host encodings and must not "
                         f"pretend to, so the whole map is refused until it is "
                         f"fixed. Got ok={ok} group={group!r}")

    with _gating(mode="enforce", hosts={GATED_HOST: opts.group_a_uuid,
                                        f"https://{GATED_HOST}/app": opts.child_a_uuid}):
        ok, group = hg.resolve_group(good)
        assert_false(ok,
                     "two entries normalizing to one host with DIFFERENT groups "
                     "is a config conflict — picking either one silently would "
                     f"confine visitors to a group nobody chose. Got {group!r}")


@th.django_unit_test("gating: a full-URL entry is accepted and reduced to its host")
def test_full_url_entry_is_host_only(opts):
    from mojo.apps.incident.models import Event
    from mojo.apps.account.services import handoff_group as hg
    from mojo.helpers.redis import get_connection

    category = "auth:handoff_group_token_entry_widened"
    raw_entry = f"https://{GATED_HOST}:9443/app"
    Event.objects.filter(category=category).delete()
    try:
        get_connection().delete(hg._notice_key("url_entry", GATED_HOST))
    except Exception:
        pass

    with _gating(mode="enforce", hosts={raw_entry: opts.group_a_uuid}):
        for dest, why in ((f"https://{GATED_HOST}/", "the bare root"),
                          (f"http://{GATED_HOST}/elsewhere", "another scheme and path")):
            ok, group = hg.resolve_group(dest)
            assert_true(ok and group is not None and group.pk == opts.group_a_id,
                        f"{dest!r} ({why}) must still be gated by a full-URL "
                        f"entry — a DENY rule must never be narrowed by the "
                        f"scheme, port or path an operator happened to paste. "
                        f"Got ok={ok} group={group!r}")

        # The widening (scheme/port/path dropped) is now a suppressed incident.
        events = list(Event.objects.filter(category=category))
        assert_eq(len(events), 1,
                  f"a full-URL gating entry must file exactly one entry-widened "
                  f"notice, got {[e.title for e in events]}")
        event = events[0]
        assert_eq(event.level, 3,
                  f"the entry-widened notice is a level-3 signal, got {event.level}")
        details = event.details or ""
        assert_true(GATED_HOST in details,
                    f"the notice must name the derived bare host: {details!r}")
        assert_true(raw_entry in details,
                    f"the notice must quote the raw entry that was widened: {details!r}")
        assert_true("MORE" in details,
                    f"the notice must say the bare-host deny rule now covers MORE "
                    f"(the host AND all its subdomains), not less: {details!r}")

        # Survives an in-process cache drop: _reset_cache_for_tests() clears the
        # compiled table (as a fresh worker would), so _entry_host re-runs on the
        # next resolve — but the Redis suppression key persists, so no second
        # notice is filed.
        hg._reset_cache_for_tests()
        hg.resolve_group(f"https://{GATED_HOST}/")
        events = list(Event.objects.filter(category=category))
        assert_eq(len(events), 1,
                  f"the entry-widened notice survives an in-process cache drop — "
                  f"the Redis key keeps it to one per window across workers and "
                  f"restarts, got {len(events)}")
    Event.objects.filter(category=category).delete()


@th.django_unit_test("gating: an unknown or dark group refuses, it never falls back to a JWT")
def test_unknown_and_inactive_groups_refuse(opts):
    from mojo.apps.account.services import handoff_group as hg

    cases = [
        ("hgt-no-such-uuid", "an unknown group uuid"),
        (opts.inactive_uuid, "an inactive group"),
        (opts.dark_child_uuid, "an active group under an INACTIVE ancestor"),
    ]
    for value, why in cases:
        with _gating(mode="enforce", hosts={GATED_HOST: value}):
            ok, group = hg.resolve_group(f"https://{GATED_HOST}/")
            assert_false(ok,
                         f"{why} must REFUSE — silently issuing a platform JWT "
                         f"because the gating target is broken is the exact "
                         f"failure this map exists to prevent. Got {group!r}")


@th.django_unit_test("gating: a resolver decides, and a broken one refuses")
def test_resolver_decides(opts):
    from mojo.apps.account.services import handoff_group as hg

    dest = "https://anything.unmapped.example.org/x"

    with _gating(mode="enforce", resolver=f"{__name__}._resolver_group_a"):
        ok, group = hg.resolve_group(dest)
        assert_true(ok and group is not None and group.pk == opts.group_a_id,
                    f"a resolver must be able to gate a host no static entry "
                    f"covers, got ok={ok} group={group!r}")

    # A resolver DECIDES: its None beats a matching static entry.
    with _gating(mode="enforce", hosts={GATED_HOST: opts.group_a_uuid},
                 resolver=f"{__name__}._resolver_none"):
        ok, group = hg.resolve_group(f"https://{GATED_HOST}/")
        assert_true(ok and group is None,
                    f"when a resolver is configured the static map must not be "
                    f"consulted at all, got group={group!r}")

    broken = [
        (f"{__name__}._resolver_unknown_uuid", "a resolver naming an unknown group"),
        (f"{__name__}._resolver_junk", "a resolver returning a junk type"),
        (f"{__name__}._resolver_raises", "a resolver that raises"),
        ("no.such.module.resolver_fn", "a dotted path that will not import"),
    ]
    for path, why in broken:
        with _gating(mode="enforce", resolver=path):
            ok, group = hg.resolve_group(dest)
            assert_false(ok,
                         f"{why} must refuse — gating is security-critical code "
                         f"and a broken one must never open the gate. Got "
                         f"ok={ok} group={group!r}")


@th.django_unit_test("oauth: a gated destination is refused, and only under enforce")
def test_oauth_refusal_modes(opts):
    from mojo import errors as merrors
    from mojo.apps.account.rest import oauth

    gated = f"https://{GATED_HOST}/land"
    hosts = {GATED_HOST: opts.group_a_uuid}

    with _gating(mode="off", hosts=hosts):
        oauth._refuse_gated_destination(None, gated)  # must not raise

    with _gating(mode="monitor", hosts=hosts):
        # Monitor reports and PROCEEDS — that is what makes the rollout safe:
        # a deployment learns from the feed that a gated site runs its own
        # OAuth callback before enforcement can break it.
        oauth._refuse_gated_destination(None, gated)

    with _gating(mode="enforce", hosts=hosts):
        try:
            oauth._refuse_gated_destination(None, gated)
            raise AssertionError(
                "enforce must REFUSE a gated OAuth destination — /complete "
                "hands a full access+refresh pair to whichever origin posts "
                "to it, and it cannot deliver a scoped token instead without "
                "provisioning the account first")
        except merrors.ValueException as exc:
            assert_eq(str(exc.reason), "redirect_uri is not on the allowlist",
                      f"the refusal must reuse /begin's existing unlisted-URI "
                      f"string so gated-vs-unlisted is not an oracle, got "
                      f"{exc.reason!r}")

        # Ungated and absent destinations are untouched.
        oauth._refuse_gated_destination(None, "https://plain.example.org/x")
        oauth._refuse_gated_destination(None, None)

        # A destination we cannot read unambiguously is refused too.
        try:
            oauth._refuse_gated_destination(None, "https://[2001:db8::1]/x")
            raise AssertionError(
                "a suspicious OAuth destination must fail CLOSED under enforce")
        except merrors.ValueException:
            pass
