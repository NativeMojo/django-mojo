"""Maestro #1955, criterion 3 — an invite emailed with no WEBAPP_BASE_URL and no
request still carries an absolute link.

The #6225 chain ends at BASE_URL (mojo/apps/account/utils/webapp_url.py). The
test project sets neither setting, so these override django.conf.settings
in-process, which is why they live in this serial package.
"""
from contextlib import contextmanager

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

API_BASE = "https://api.ilb-invite.example"
INVITEE = "ilb_invitee@example.com"
GROUP = "ilb-invite-group"
UNSET = {"WEBAPP_BASE_URL": "", "BASE_URL": API_BASE, "SHORTLINK_BASE_URL": ""}


@contextmanager
def _override_settings(**values):
    """In-process Django settings override (th.server_settings only affects the
    separate server process; override_settings is banned by testing rules)."""
    import django.conf
    sentinel = object()
    originals = {name: getattr(django.conf.settings, name, sentinel) for name in values}
    for name, value in values.items():
        setattr(django.conf.settings, name, value)
    try:
        yield
    finally:
        for name, original in originals.items():
            if original is sentinel:
                delattr(django.conf.settings, name)
            else:
                setattr(django.conf.settings, name, original)


def _assert_settings_apply():
    # A stored Setting row wins over django.conf.settings; the test only means
    # something if the chain really sees WEBAPP_BASE_URL unset.
    from mojo.helpers.settings import settings
    assert_true(not settings.get("WEBAPP_BASE_URL"),
                "WEBAPP_BASE_URL must read as unset for this test (is a Setting row stored?)")
    assert_eq(settings.get("BASE_URL"), API_BASE,
              "BASE_URL must read as the test value (is a Setting row stored?)")


@th.django_unit_setup()
def setup_invite_link_base(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.shortlink.models import ShortLink

    ShortLink.objects.filter(user__email=INVITEE).delete()
    User.objects.filter(email=INVITEE).delete()
    Group.objects.filter(name=GROUP).delete()
    opts.group = Group.objects.create(name=GROUP, is_active=True, kind="group")


@th.django_unit_test("invite link: no request and no WEBAPP_BASE_URL falls back to BASE_URL")
def test_invite_token_url_falls_back_to_base_url(opts):
    from mojo.apps.account.utils.webapp_url import build_token_url

    with _override_settings(**UNSET):
        _assert_settings_apply()
        url = build_token_url("invite", "iv:ilbtoken")
    assert_true(url.startswith(f"{API_BASE}/auth?flow=invite&token=iv:"),
                f"the invite link must be absolute on BASE_URL, got {url}")


@th.django_unit_test("invite link: Group.invite emails a new user an absolute link")
def test_group_invite_emails_an_absolute_link(opts):
    from unittest.mock import patch
    from mojo.apps.account.models import User
    from mojo.apps.shortlink.models import ShortLink

    sent = []

    def capture(self, template_name=None, group=None, context=None, **kwargs):
        sent.append({"template": template_name, "group": group, "context": context})

    with _override_settings(**UNSET), \
            patch.object(User, "send_template_email", capture):
        _assert_settings_apply()
        opts.group.invite(INVITEE)

    assert_eq(len(sent), 1, f"Group.invite must send one email to a new user, sent {len(sent)}")
    assert_eq(sent[0]["template"], "invite", "a never-signed-in invitee gets the invite email")
    assert_eq(sent[0]["group"], opts.group, "the invite email is sent with the inviting group")
    token_url = sent[0]["context"]["token_url"]
    assert_true(token_url.startswith("https://"),
                f"the emailed link must be absolute, got {token_url}")
    link = ShortLink.objects.filter(user__email=INVITEE, source="invite").order_by("-id").first()
    destination = link.url if link is not None else token_url
    assert_true(destination.startswith(f"{API_BASE}/auth?flow=invite&token=iv:"),
                f"the link must lead to the hosted invite page on BASE_URL, got {destination}")
