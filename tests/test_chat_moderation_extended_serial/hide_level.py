"""Live hide level and link allowlist (#5774); serial because Setting rows are shared."""
from contextlib import contextmanager

from testit import helpers as th

HIDE = "CHAT_MODERATION_HIDE_LEVEL"
DOMAINS = "CHAT_MODERATION_ALLOWED_DOMAINS"
PREFIX = "test-chat-hide-level-"
EMAILS = [PREFIX + role + "@example.com" for role in ("author", "reader")]
FIELDS = ("moderation_decision", "moderation_reasons", "moderation_score")
SEVENTY_FIVE = "bastard bitch"      # 75, deny_hit + repeated_profanity
FIFTY = "fuck"                      # 50, strong_profanity
MAESTRO_LINK = "notes at https://maestromojo.com/app"
GITHUB_LINK = "diff at https://github.com/nativemojo/django-mojo/pull/1"


@contextmanager
def _restore_settings(*groups):
    """Restore the global rows of both keys and drop every test group row."""
    from mojo.apps.account.models import Setting

    saved = {}
    for key in (HIDE, DOMAINS):
        row = Setting.objects.filter(key=key, group=None).first()
        saved[key] = row.get_value() if row else None
    try:
        yield Setting
    finally:
        for key in (HIDE, DOMAINS):
            for group in groups:
                Setting.remove(key, group=group)
            if saved[key] is None:
                Setting.remove(key)
            else:
                Setting.set(key, saved[key])


@th.django_unit_setup()
def setup_hide_level(opts):
    from mojo.apps.account.models import Group, User
    from mojo.apps.chat.models import ChatRoom

    ChatRoom.objects.filter(name__startswith=PREFIX).delete()
    User.objects.filter(email__in=EMAILS).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    opts.author, opts.reader = [
        User.objects.create_user(username=email, email=email, password="TestPass1!")
        for email in EMAILS
    ]
    opts.parent = Group.objects.create(name=PREFIX + "parent")
    opts.child = Group.objects.create(name=PREFIX + "child", parent=opts.parent)
    opts.other = Group.objects.create(name=PREFIX + "other")


def _room(opts, name, group):
    from mojo.apps.chat.models import ChatRoom, ChatMembership

    ChatRoom.objects.filter(name=PREFIX + name).delete()
    room = ChatRoom.objects.create(
        name=PREFIX + name, kind="group", user=opts.author, group=group,
        rules={"rate_limit": 0})
    ChatMembership.objects.create(room=room, user=opts.author, role="owner")
    ChatMembership.objects.create(room=room, user=opts.reader)
    return room


def _decision(body, group=None):
    from mojo.apps.chat.rules import check_moderation_scored
    return check_moderation_scored(body, group=group)[0]


@th.django_unit_test("hide level: default 70, global row, group row, parent inheritance")
def test_hide_level_resolution(opts):
    with _restore_settings(opts.parent, opts.child, opts.other) as Setting:
        Setting.remove(HIDE)
        th.assert_eq(_decision(SEVENTY_FIVE), "masked", "no row: 75 reaches the default 70")
        th.assert_eq(_decision(FIFTY), "warn", "no row: 50 is warn")

        Setting.set(HIDE, 80)
        th.assert_eq(_decision(SEVENTY_FIVE), "warn", "global 80: 75 is shown as warn")
        th.assert_eq(_decision(SEVENTY_FIVE, opts.other), "warn", "a group with no row uses the global row")

        Setting.set(HIDE, 40, group=opts.parent)
        th.assert_eq(_decision(FIFTY, opts.parent), "masked", "group 40: 50 is masked in that group")
        th.assert_eq(_decision(FIFTY, opts.child), "masked", "a child group inherits its parent's row")
        th.assert_eq(_decision(FIFTY, opts.other), "warn", "another group keeps the global row")
        th.assert_eq(_decision(FIFTY), "warn", "no group keeps the global row")

        Setting.remove(HIDE, group=opts.parent)
        Setting.remove(HIDE)
        th.assert_eq(_decision(FIFTY, opts.child), "warn", "removing the rows restores the default")
        th.assert_eq(_decision(SEVENTY_FIVE, opts.child), "masked", "default 70 applies again")


@th.django_unit_test("slurs are masked whatever the level; other swearing follows it")
def test_slurs_always_hidden(opts):
    from mojo.apps.chat.rules import check_moderation_scored

    with _restore_settings() as Setting:
        Setting.set(HIDE, 101)
        for body in ("You are a faggot", "n1gger"):
            decision, reasons, score = check_moderation_scored(body)
            th.assert_eq(decision, "masked", f"{body!r} is a slur and must be masked at level 101")
            th.assert_true("high_severity" in reasons, f"{body!r} must carry high_severity: {reasons}")
        for body in ("fuck", "holy shit it works", "this is bullshit"):
            decision, reasons, score = check_moderation_scored(body)
            th.assert_eq(decision, "warn", f"{body!r} must follow the level (warn at 101)")
            th.assert_true("strong_profanity" in reasons, f"{body!r} reasons: {reasons}")
            th.assert_true("high_severity" not in reasons, f"{body!r} is not a slur: {reasons}")
        th.assert_eq(check_moderation_scored("fuck bastard")[0], "warn",
                     "101 never hides by score: 95 is warn")
        for body in ("suspicious", "Scunthorpe office"):
            th.assert_eq(check_moderation_scored(body), ("allow", [], 0), f"{body!r} is clean")


@th.django_unit_test("allowed domains: global row, group override, fallback")
def test_allowed_domains_resolution(opts):
    from mojo.apps.chat.rules import check_moderation_scored

    with _restore_settings(opts.parent, opts.child, opts.other) as Setting:
        Setting.remove(DOMAINS)
        th.assert_eq(check_moderation_scored(MAESTRO_LINK)[2], 25, "no row: every link scores")

        Setting.set(DOMAINS, ["maestromojo.com"])
        th.assert_eq(check_moderation_scored(MAESTRO_LINK)[2], 0, "global row allows maestromojo.com")
        th.assert_eq(check_moderation_scored(MAESTRO_LINK, group=opts.other)[2], 0,
                     "a group with no row falls back to the global row")

        Setting.set(DOMAINS, ["github.com"], group=opts.parent)
        th.assert_eq(check_moderation_scored(GITHUB_LINK, group=opts.child)[2], 0,
                     "the parent's row applies to its child group")
        th.assert_eq(check_moderation_scored(MAESTRO_LINK, group=opts.parent)[2], 25,
                     "a group row replaces the global list")
        th.assert_eq(check_moderation_scored(GITHUB_LINK)[2], 25, "the group row stays in its group")


@th.django_unit_test("validators refuse bad hide levels and domain lists")
def test_validators(opts):
    from mojo import errors as merrors

    with _restore_settings(opts.parent) as Setting:
        for value in (0, 102, True, "x"):
            try:
                Setting.set(HIDE, value)
            except merrors.ValueException:
                pass
            else:
                th.assert_true(False, f"hide level {value!r} must be refused")
        Setting.set(HIDE, 101)
        Setting.set(HIDE, 50, group=opts.parent)
        for value in (["com"], ["https://x.com"], ["*.x.com"], ["X.com"], "x.com", {"a": 1},
                      ["x.com"] * 201):
            try:
                Setting.set(DOMAINS, value)
            except merrors.ValueException:
                pass
            else:
                th.assert_true(False, f"domain list {value!r} must be refused")
        Setting.set(DOMAINS, ["maestromojo.com", "github.com"])
        Setting.set(DOMAINS, ["nativemojo.com"], group=opts.parent)


@th.django_unit_test("send and edit store and broadcast the room group's decision")
def test_send_and_edit_use_room_group(opts):
    from mojo.apps.chat.handler import _handle_edit
    from mojo.apps.chat.services.messages import send_message

    room = _room(opts, "wire", opts.child)
    plain = _room(opts, "plain", None)
    events = []
    capture = lambda topic, frame: events.append(frame)
    with _restore_settings(opts.parent) as Setting:
        Setting.remove(HIDE)
        Setting.set(HIDE, 40, group=opts.parent)
        msg, error = send_message(room, opts.author, FIFTY, publisher=capture)
        th.assert_eq(error, None, "send succeeds")
        th.assert_eq(msg.moderation_decision, "masked", "send uses the room group's level (40)")
        th.assert_eq(events[-1]["moderation_decision"], "masked", "the broadcast carries it")

        other, error = send_message(plain, opts.author, FIFTY, publisher=capture)
        th.assert_eq(other.moderation_decision, "warn", "a room with no group uses the global default")

        ack = _handle_edit(opts.author, {"message_id": other.pk, "body": FIFTY + " again"},
                           publisher=capture)
        th.assert_eq(ack["moderation_decision"], "warn", "edit in a room with no group")
        ack = _handle_edit(opts.author, {"message_id": msg.pk, "body": "clean now"},
                           publisher=capture)
        th.assert_eq(ack["moderation_decision"], "allow", "a clean edit is allowed")
        ack = _handle_edit(opts.author, {"message_id": msg.pk, "body": FIFTY}, publisher=capture)
        th.assert_eq(ack["moderation_decision"], "masked", "edit uses the room group's level")
        th.assert_eq(events[-1]["moderation_decision"], "masked", "the edit broadcast carries it")
        msg.refresh_from_db()
        th.assert_eq(msg.moderation_decision, "masked", "the edit stores it")
