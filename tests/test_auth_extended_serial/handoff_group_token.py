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


# --- Endpoint (reload) helpers, moved from tests/test_auth/handoff_group_token.py
# (maestro #2791). These blocks hit the real server, so they need
# th.server_settings() reloads — legal here in the serial sibling.
def _data(resp):
    """The `data` dict of a response body, or {} when there isn't one."""
    body = resp.response
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def _mint(opts, destination, **kwargs):
    """Mint a handoff code for `destination`; returns the response."""
    return opts.client.post("/api/auth/handoff", {"redirect_uri": destination}, **kwargs)


BLOCK1_ALLOWED = [
    f"https://{GATED_HOST}/",
    f"http://{GATED_HOST}/",
    f"https://{GATED_HOST}:8443/",
    f"https://{GATED_HOST}./",
    f"https://deep.sub.{GATED_HOST}/",
    f"https://{UNGATED_HOST}/",
]




# ==========================================================================
# Endpoint gating (reload) blocks — moved from the parallel package
# (maestro #2791). block1 stashes opts.flip_code (TTL 300); the
# stamp-wins test consumes it AFTER block1's enforce window exits, so
# these must stay in this order.
# ==========================================================================
@th.unit_test("enforce: a gated destination NEVER receives a platform JWT")
def test_block1_enforce(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.account.models.member import GroupMember
    from mojo.apps.account.models.login_event import UserLoginEvent
    from mojo.apps.incident.models import Event as IncidentEvent
    from tests.test_register import _capture

    source = "handoff:grouptoken"
    gated = f"https://{GATED_HOST}/"

    with th.server_settings(
            AUTH_HANDOFF_ALLOWED_URLS=BLOCK1_ALLOWED,
            AUTH_HANDOFF_GROUP_TOKEN_MODE="enforce",
            AUTH_HANDOFF_GROUP_TOKEN_HOSTS={GATED_HOST: opts.group_a_uuid},
            AUTH_HANDOFF_CODE_TTL=300,
            # The OAuth arm below needs the gated host to CLEAR
            # _validate_redirect_uri, or its 400 would come from the allowlist
            # and the test would pass with the whole feature deleted. The
            # test project's own entry is kept: tests/test_oauth runs in a
            # parallel package and relies on it.
            ALLOWED_REDIRECT_URLS=["https://example.com/", f"https://{GATED_HOST}/"]):
        _clear_limits()

        # --- mint every code this block needs in ONE session ----------------
        assert_true(opts.client.login(MEMBER, PWORD), "member login should succeed")

        def mint(dest, why):
            resp = _mint(opts, dest)
            assert_eq(resp.status_code, 200,
                      f"minting for {dest!r} ({why}) should succeed under "
                      f"enforcement, got {resp.status_code}: {resp.response}")
            code = _data(resp).get("code")
            assert_true(bool(code), f"no code minted for {dest!r}: {resp.response}")
            return code

        code_gated = mint(gated, "the gated host")
        code_ungated = mint(UNGATED_DEST, "an ungated host")
        code_sidefx = mint(gated, "the side-effect probe")
        code_deact = mint(gated, "the group-deactivation probe")
        codes_g = [(mint(dest, why), dest, why) for dest, why in BLOCK1_GATED_DESTS]
        # Stashed UNSPENT for section F — AUTH_HANDOFF_CODE_TTL is 300s here so
        # it survives this block's exit and the mode flipping back to off.
        opts.flip_code = mint(gated, "the mode-flip probe")
        opts.client.logout()

        # --- the group package ----------------------------------------------
        # #3329: a gated exchange IS a sign-in, so it must also leave one
        # security-history row attributed to the destination's group. Cleared
        # first — the member login above wrote an unattributed one of its own.
        IncidentEvent.objects.filter(
            uid=opts.member_id, category="login").delete()
        resp = opts.client.post("/api/auth/exchange", {"code": code_gated})
        assert_eq(resp.status_code, 200,
                  f"a gated exchange should succeed for a member, got "
                  f"{resp.status_code}: {resp.response}")
        data = _data(resp)
        token = data.get("access_token")
        assert_true(isinstance(token, str) and token.startswith("gt1."),
                    f"a gated destination must receive a group-scoped token, "
                    f"got {token!r}")
        assert_false("refresh_token" in data,
                     f"a group package must carry NO refresh_token key AT ALL — "
                     f"its absence is what makes the client clear a stale one. "
                     f"Got keys {sorted(data.keys())}")
        assert_eq(data.get("token_type"), "grouptoken",
                  f"the package must name its scheme, got {data.get('token_type')!r}")
        assert_true((data.get("expires_in") or 0) > 0,
                    f"a gt1. token carries no exp claim, so expires_in is the "
                    f"only lifetime the client gets: {data.get('expires_in')!r}")
        group = data.get("group") or {}
        assert_eq(group.get("id"), opts.group_a_id, f"wrong group id: {group!r}")
        assert_eq(group.get("uuid"), opts.group_a_uuid, f"wrong group uuid: {group!r}")
        assert_eq(group.get("name"), "hgt_tenant_a", f"wrong group name: {group!r}")
        assert_eq((data.get("user") or {}).get("id"), opts.member_id,
                  f"the package must identify the signed-in visitor: {data.get('user')!r}")

        rows = list(IncidentEvent.objects.filter(
            uid=opts.member_id, category="login"))
        assert_eq(len(rows), 1,
                  f"a gated exchange must write exactly ONE security-history "
                  f"sign-in row, got {len(rows)}")
        assert_eq(rows[0].group_id, opts.group_a_id,
                  f"the row must be attributed to the destination's group — the "
                  f"trusted handoff context, whose membership mint() already "
                  f"proved. Got {rows[0].group_id!r}")
        assert_eq(rows[0].metadata.get("origin_group_id"), opts.group_a_id,
                  f"the brand marker must name the group: {rows[0].metadata}")

        # --- the token actually works, and buys nothing more ----------------
        me = opts.client.get("/api/user/me",
                             headers={"Authorization": f"grouptoken {token}"})
        assert_eq(me.status_code, 200,
                  f"the delivered group token must authenticate, got "
                  f"{me.status_code}: {me.response}")
        assert_eq(me.response.data.id, opts.member_id,
                  "the group token must resolve to the visitor it was minted for")

        replay = opts.client.post("/api/auth/exchange", {"code": code_gated})
        assert_eq(replay.status_code, 401,
                  f"a gated code is single-use like any other, got "
                  f"{replay.status_code}: {replay.response}")

        upgrade = opts.client.post("/api/refresh_token", {"refresh_token": token})
        assert_eq(upgrade.status_code, 401,
                  f"a gt1. string posted as a refresh_token must not buy a JWT "
                  f"pair, got {upgrade.status_code}: {upgrade.response}")

        # --- the untouched path: an ungated destination is unchanged ---------
        resp = opts.client.post("/api/auth/exchange", {"code": code_ungated})
        assert_eq(resp.status_code, 200,
                  f"an ungated destination must still exchange, got "
                  f"{resp.status_code}: {resp.response}")
        data = _data(resp)
        assert_true(bool(data.get("refresh_token")),
                    f"an ungated destination must still receive a full JWT PAIR "
                    f"— gating must not change any other destination. Got keys "
                    f"{sorted(data.keys())}")
        assert_false(str(data.get("access_token") or "").startswith("gt1."),
                     "an ungated destination must receive a platform JWT")
        assert_false("token_type" in data,
                     f"JWT responses are frozen by login_response_contract.py "
                     f"and must not grow a token_type key: {sorted(data.keys())}")

        # --- G. every bypass shape delivers a group token, not a JWT ---------
        _clear_limits()
        for code, dest, why in codes_g:
            resp = opts.client.post("/api/auth/exchange", {"code": code})
            assert_eq(resp.status_code, 200,
                      f"exchange for {dest!r} ({why}) should succeed, got "
                      f"{resp.status_code}: {resp.response}")
            data = _data(resp)
            assert_false("refresh_token" in data,
                         f"A JWT-BEARING RESPONSE FOR A GATED HOST IS A BUG: "
                         f"{dest!r} ({why}) is the gated host wearing a "
                         f"different scheme/port/case/depth, and it received a "
                         f"refresh token. Keys: {sorted(data.keys())}")
            assert_true(str(data.get("access_token") or "").startswith("gt1."),
                        f"{dest!r} ({why}) must receive a gt1. token, got "
                        f"{data.get('access_token')!r}")

        # --- D10 side effects on a successful gated exchange -----------------
        _clear_limits()
        member = User.objects.get(pk=opts.member_id)
        before_login = member.last_login
        before_webapp = member.get_protected_metadata("orig_webapp_url")
        UserLoginEvent.objects.filter(user_id=opts.member_id, source=source).delete()
        capture_id = _capture.new_capture_id()
        _capture.clear_capture(capture_id)
        resp = opts.client.post(
            "/api/auth/exchange",
            {"code": code_sidefx, "webapp_base_url": "https://hgt-tenant.example.org"},
            headers={
                "X-Mojo-Test-User-Login-Handler": "tests.test_register._capture.capture_login",
                "X-Mojo-Test-Capture-Id": capture_id,
                "Origin": "https://hgt-tenant.example.org",
            })
        assert_eq(resp.status_code, 200,
                  f"the side-effect probe should exchange, got {resp.status_code}: "
                  f"{resp.response}")
        member = User.objects.get(pk=opts.member_id)
        assert_true(before_login is None or member.last_login > before_login,
                    f"a gated exchange IS a login and must advance last_login "
                    f"({before_login} -> {member.last_login})")
        event = UserLoginEvent.objects.filter(
            user_id=opts.member_id, source=source).last()
        assert_true(event is not None,
                    f"a gated exchange must record a UserLoginEvent with "
                    f"source={source!r} — an auth path that leaves no event is "
                    f"an invisible one")
        assert_true(event.device_id is not None,
                    "the login event must carry the device, exactly as jwt_login does")
        calls = _capture.read_capture(capture_id).get("login", [])
        assert_eq(len(calls), 1,
                  f"USER_LOGIN_HANDLER must fire exactly once for a gated "
                  f"exchange, got {calls!r}")
        assert_eq(calls[0].get("source"), source,
                  f"the handler must see the gated source, got {calls[0]!r}")
        after_webapp = member.get_protected_metadata("orig_webapp_url")
        assert_eq(after_webapp, before_webapp,
                  f"a gated exchange must NOT write webapp_url metadata: both "
                  f"webapp_base_url and Origin come from the GATED site's own "
                  f"backend, so honoring them would let a tenant overwrite "
                  f"platform-account metadata for every visitor "
                  f"({before_webapp!r} -> {after_webapp!r})")
        _capture.clear_capture(capture_id)

        # --- the group goes dark between mint and exchange -------------------
        signin_rows_before = IncidentEvent.objects.filter(
            uid=opts.member_id, category="login").count()
        Group.objects.filter(pk=opts.group_a_id).update(is_active=False)
        try:
            resp = opts.client.post("/api/auth/exchange", {"code": code_deact})
            assert_eq(resp.status_code, 403,
                      f"a group deactivated inside the code's TTL must refuse, "
                      f"got {resp.status_code}: {resp.response}")
            assert_false("access_token" in _data(resp),
                         f"a refused gated exchange must never fall back to a "
                         f"JWT: {resp.response}")
            signin_rows_after = IncidentEvent.objects.filter(
                uid=opts.member_id, category="login").count()
            assert_eq(signin_rows_after, signin_rows_before,
                      f"#3329: a failed mint raises above the recorder, so a "
                      f"refused gated exchange must leave NO security-history "
                      f"sign-in row ({signin_rows_before} -> {signin_rows_after})")
        finally:
            Group.objects.filter(pk=opts.group_a_id).update(is_active=True)

        # --- membership removed between mint and exchange --------------------
        _clear_limits()
        assert_true(opts.client.login(TEMP, PWORD), "temp-user login should succeed")
        code_temp = mint(gated, "the membership-revocation probe")
        opts.client.logout()
        GroupMember.objects.filter(
            user_id=opts.temp_id, group_id=opts.group_a_id).delete()
        resp = opts.client.post("/api/auth/exchange", {"code": code_temp})
        assert_eq(resp.status_code, 403,
                  f"membership removed inside the code's TTL must refuse — the "
                  f"real mint at exchange re-runs every guard. Got "
                  f"{resp.status_code}: {resp.response}")
        assert_false("access_token" in _data(resp),
                     f"a refused gated exchange must never carry a token: {resp.response}")

        # --- who may not be handed a gated destination at all ----------------
        _clear_limits()
        for username, why in ((OUTSIDER, "a non-member"),
                              (SUPERUSER, "a superuser, who can never hold a "
                                          "group token")):
            assert_true(opts.client.login(username, PWORD),
                        f"{username} login should succeed")
            resp = _mint(opts, gated)
            assert_eq(resp.status_code, 403,
                      f"{why} must be refused at MINT — a code that can never be "
                      f"spent is worse than no code. Got {resp.status_code}: "
                      f"{resp.response}")
            assert_true(_data(resp).get("code") is None,
                        f"{why} must not receive a code: {resp.response}")
            assert_false("access_token" in _data(resp),
                         f"{why} must not receive a token: {resp.response}")
            opts.client.logout()

        # --- H. the OAuth leg REFUSES a gated destination --------------------
        # It cannot deliver a scoped token instead: /complete can create the
        # account, join a group from client-supplied state and fire the
        # registration webhook before any membership pre-flight could fail.
        _clear_limits()
        opts.client.logout()

        resp = opts.client.get(
            f"/api/auth/oauth/google/begin?redirect_uri=https://{GATED_HOST}/land")
        assert_eq(resp.status_code, 400,
                  f"/begin must refuse a gated redirect_uri — this URL CLEARS "
                  f"ALLOWED_REDIRECT_URLS, so a 200 here means gating never ran. "
                  f"Got {resp.status_code}: {resp.response}")
        assert_true("allowlist" in str(resp.response),
                    f"the refusal must reuse the string /begin already emits for "
                    f"an unlisted URI, so gated-vs-unlisted is not an oracle: "
                    f"{resp.response}")
        assert_true(_data(resp).get("auth_url") is None,
                    f"a refused /begin must return no auth_url: {resp.response}")

        resp = opts.client.get(
            "/api/auth/oauth/google/begin?redirect_uri=https://example.com/login")
        assert_eq(resp.status_code, 200,
                  f"an UNGATED redirect_uri must still begin normally — this is "
                  f"what proves the 400 above came from gating and not from the "
                  f"allowlist. Got {resp.status_code}: {resp.response}")
        assert_true(bool(_data(resp).get("auth_url")) and bool(_data(resp).get("state")),
                    f"an ungated /begin must be unchanged: {resp.response}")

        # The no-redirect_uri branch derives the landing URL from the
        # UNVALIDATED Origin header and never calls _validate_redirect_uri at
        # all, so this arm cannot be vacuous.
        resp = opts.client.get("/api/auth/oauth/google/begin",
                               headers={"Origin": f"https://{GATED_HOST}"})
        assert_eq(resp.status_code, 400,
                  f"/begin must refuse a gated Origin-derived default — that "
                  f"branch picks the destination with a request header and "
                  f"skips the allowlist entirely. Got {resp.status_code}: "
                  f"{resp.response}")

        resp = opts.client.get("/api/auth/oauth/google/begin")
        assert_eq(resp.status_code, 200,
                  f"with no Origin the default lands on the AUTH ORIGIN itself, "
                  f"which is the same-host carve-out: a zone-wide gating entry "
                  f"must never refuse a tenant's own white-label auth page when "
                  f"it calls its own OAuth buttons. Got {resp.status_code}: "
                  f"{resp.response}")

        # /complete cannot be driven end to end (no real provider), so seed the
        # state directly and assert the refusal lands BEFORE any provider call.
        # "redirect_uri is not on the allowlist" is unreachable in /complete by
        # any other path — _validate_redirect_uri is never called there.
        import json as _json
        import uuid as _uuid
        from mojo.helpers.redis import get_connection

        def seed_state(frontend_uri):
            state = _uuid.uuid4().hex
            get_connection().setex(f"oauth:state:{state}", 600, _json.dumps({
                "redirect_uri": "https://example.com/api/auth/oauth/google/callback",
                "frontend_uri": frontend_uri}))
            return state

        resp = opts.client.post(
            "/api/auth/oauth/google/complete",
            {"code": "hgt-not-a-real-code",
             "state": seed_state(f"https://{GATED_HOST}/land")})
        assert_eq(resp.status_code, 400,
                  f"/complete must refuse a state whose frontend_uri is gated — "
                  f"this is the authoritative check and it survives a mode flip "
                  f"inside the state's TTL. Got {resp.status_code}: {resp.response}")
        assert_true("redirect_uri is not on the allowlist" in str(resp.response),
                    f"the refusal must be the gating one, not a provider error: "
                    f"{resp.response}")

        resp = opts.client.post(
            "/api/auth/oauth/google/complete",
            {"code": "hgt-not-a-real-code",
             "state": seed_state("https://example.com/login")},
            headers={"Origin": f"https://{GATED_HOST}"})
        assert_eq(resp.status_code, 400,
                  f"/complete must also refuse when the CALLER's own origin is "
                  f"gated — the response body physically goes there. Got "
                  f"{resp.status_code}: {resp.response}")
        assert_true("redirect_uri is not on the allowlist" in str(resp.response),
                    f"the refusal must be the gating one: {resp.response}")


@th.unit_test("monitor: predicts both outcomes in the incident feed, binds neither")
def test_block2_monitor(opts):
    from mojo.apps.incident.models import Event
    from mojo.apps.account.services import handoff_group as hg
    from mojo.helpers.redis import get_connection

    category = "auth:handoff_group_token_preview"
    gated = f"https://{GATED_HOST}/"

    Event.objects.filter(category=category).delete()
    # Suppression is per host per outcome per hour — clear it so this test is
    # repeatable against a long-lived Redis (precedent: handoff.py).
    for outcome in ("deliver", "refuse"):
        try:
            get_connection().delete(hg._notice_key("preview", GATED_HOST, outcome))
        except Exception:
            pass

    with th.server_settings(
            AUTH_HANDOFF_GROUP_TOKEN_MODE="monitor",
            AUTH_HANDOFF_GROUP_TOKEN_HOSTS={GATED_HOST: opts.group_a_uuid}):
        _clear_limits()
        for username, why in ((MEMBER, "a member"), (OUTSIDER, "a non-member")):
            assert_true(opts.client.login(username, PWORD),
                        f"{username} login should succeed")
            resp = _mint(opts, gated)
            assert_eq(resp.status_code, 200,
                      f"monitor must never refuse {why}, got {resp.status_code}: "
                      f"{resp.response}")
            code = _data(resp).get("code")
            assert_true(bool(code), f"monitor must still mint for {why}: {resp.response}")
            opts.client.logout()

            resp = opts.client.post("/api/auth/exchange", {"code": code})
            assert_eq(resp.status_code, 200,
                      f"monitor must still exchange for {why}, got "
                      f"{resp.status_code}: {resp.response}")
            data = _data(resp)
            assert_true(bool(data.get("refresh_token")),
                        f"monitor must NOT bind — {why} still receives the full "
                        f"JWT pair they receive today. Keys: {sorted(data.keys())}")

        # A repeat inside the suppression window must not file a third event.
        _clear_limits()
        assert_true(opts.client.login(OUTSIDER, PWORD), "outsider re-login should succeed")
        assert_eq(_mint(opts, gated).status_code, 200, "the repeat mint should succeed")
        opts.client.logout()

    events = list(Event.objects.filter(category=category))
    assert_eq(len(events), 2,
              f"monitor must file exactly ONE preview per outcome per host — one "
              f"'would deliver' and one 'would refuse' — so the feed writes the "
              f"gating map the way it already writes the allowlist. Got "
              f"{[e.title for e in events]}")
    titles = sorted(e.title or "" for e in events)
    assert_true(any("(deliver)" in t for t in titles) and any("(refuse)" in t for t in titles),
                f"the two previews must be distinguishable by outcome: {titles}")
    for event in events:
        assert_true(GATED_HOST in (event.details or ""),
                    f"a preview must name the destination: {event.details!r}")
        assert_true("hgt_tenant_a" in (event.details or ""),
                    f"a preview must name the group it would confine to: "
                    f"{event.details!r}")
    Event.objects.filter(category=category).delete()


@th.unit_test("enforce without the destination allowlist refuses EVERY handoff")
def test_block3_missing_prerequisite(opts):
    from mojo.apps.incident.models import Event
    from mojo.apps.account.services import handoff_group as hg
    from mojo.helpers.redis import get_connection

    category = "auth:handoff_group_token_misconfigured"
    Event.objects.filter(category=category).delete()
    try:
        get_connection().delete(hg._notice_key("misconfigured", "prerequisite", None))
    except Exception:
        pass

    # No AUTH_HANDOFF_ALLOWED_URLS / AUTH_HANDOFF_RESOLVER: the test server's
    # shipped state is the destination allowlist's MONITOR mode.
    with th.server_settings(
            AUTH_HANDOFF_GROUP_TOKEN_MODE="enforce",
            AUTH_HANDOFF_GROUP_TOKEN_HOSTS={GATED_HOST: opts.group_a_uuid}):
        _clear_limits()
        assert_true(opts.client.login(MEMBER, PWORD), "member login should succeed")
        cases = [
            (f"https://{GATED_HOST}/", "the gated host itself"),
            ("https://anything.example.org/x",
             "a host that is NOT in the gating map — the exact combination that "
             "would otherwise hand out a full JWT pair while the operator "
             "believes gating is on"),
            (None, "no redirect_uri at all"),
        ]
        for dest, why in cases:
            body = {} if dest is None else {"redirect_uri": dest}
            resp = opts.client.post("/api/auth/handoff", body)
            assert_eq(resp.status_code, 400,
                      f"gating enforce without allowlist enforcement must refuse "
                      f"EVERY handoff, including {why}. Got {resp.status_code}: "
                      f"{resp.response}")
            assert_true(_data(resp).get("code") is None,
                        f"a refused handoff must mint no code ({why}): {resp.response}")
        opts.client.logout()

    events = list(Event.objects.filter(category=category))
    assert_eq(len(events), 1,
              f"the misconfiguration must be reported once per hour, loudly — a "
              f"two-line settings mistake becoming a visible handoff outage is "
              f"the point. Got {[e.title for e in events]}")
    assert_eq(events[0].level, 2,
              f"a deployment that cannot deliver the property it advertises is "
              f"a level-2 incident, got level {events[0].level}")
    Event.objects.filter(category=category).delete()


@th.unit_test("the stamp on the code wins a mode flip, and a corrupt gid is never a JWT")
def test_stamp_wins_and_corrupt_gid_refuses(opts):
    import json
    import uuid as _uuid
    from mojo.apps.account.models import Group
    from mojo.helpers.redis import get_connection

    _clear_limits()
    assert_true(bool(getattr(opts, "flip_code", None)),
                "block 1 must have stashed an unspent gated code")

    resp = opts.client.post("/api/auth/exchange", {"code": opts.flip_code})
    assert_eq(resp.status_code, 200,
              f"the stashed code should still exchange, got {resp.status_code}: "
              f"{resp.response}")
    data = _data(resp)
    assert_true(str(data.get("access_token") or "").startswith("gt1."),
                f"gating is now OFF, and this code must STILL deliver a group "
                f"token: the decision was taken at mint and is never re-read, "
                f"so a resolver that breaks inside the code's TTL cannot turn a "
                f"gated code back into a platform JWT. Got "
                f"{data.get('access_token')!r}")
    assert_false("refresh_token" in data,
                 f"the honored stamp must still suppress the refresh token: "
                 f"{sorted(data.keys())}")

    # Corrupt stamps, written straight into the Redis payload.
    highest = Group.objects.order_by("-pk").first()
    deleted_pk = (highest.pk if highest else 0) + 100000

    def seed(gid):
        code = _uuid.uuid4().hex
        payload = {"uid": opts.member_id, "ip": "",
                   "dest": f"https://{GATED_HOST}/", "gid": gid}
        get_connection().setex(f"auth:handoff:{code}", 300, json.dumps(payload))
        return code

    cases = [
        ("abc", 401, "a non-numeric gid"),
        (0, 401, "a zero gid"),
        (-1, 401, "a negative gid"),
        (True, 401, "a boolean gid (bool is an int subclass in Python)"),
        (deleted_pk, 403, "a gid naming a group that does not exist"),
    ]
    for gid, expected, why in cases:
        resp = opts.client.post("/api/auth/exchange", {"code": seed(gid)})
        assert_eq(resp.status_code, expected,
                  f"{why} should be {expected}, got {resp.status_code}: "
                  f"{resp.response}")
        assert_false("access_token" in _data(resp),
                     f"{why} must NEVER produce a token — a corrupt stamp is a "
                     f"corrupt code, not a licence to fall back to a JWT: "
                     f"{resp.response}")
