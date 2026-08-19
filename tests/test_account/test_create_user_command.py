"""Tests for the `create_user` management command."""
from io import StringIO
from testit import helpers as th

EMAIL_SUPERUSER = "cucmd_super@test.com"
PHONE_ONLY = "+15555550100"
EMAIL_STAFF = "cucmd_scoped@test.com"
EMAIL_DUP = "cucmd_dup@test.com"
EMAIL_WEAK = "cucmd_weak@test.com"
EMAIL_LINK = "cucmd_link@test.com"
EMAIL_LINK_REL = "cucmd_link_rel@test.com"
EMAIL_LINK_CLASH = "cucmd_link_clash@test.com"
EMAIL_LINK_PW = "cucmd_link_pw@test.com"
EMAIL_LINK_DUP = "cucmd_link_dup@test.com"

# The webapp base URL is resolved from the user's org rather than from a
# patched settings object on purpose: a process-global settings mock is a known
# source of cross-test flakes in this suite, and `get_webapp_base_url` already
# consults `user.org` ahead of any setting.
LINK_GROUP = "cucmd-link-portal"
LINK_BASE = "https://portal.example.com"
LINK_GROUP_RELATIVE = "cucmd-link-relative"
LINK_BASE_RELATIVE = "/portal"


@th.django_unit_setup()
def setup_create_user_command(opts):
    from mojo.apps.account.models import Group, User

    # Clean up any leftover test data so the suite is repeatable.
    User.objects.filter(email__in=[
        EMAIL_SUPERUSER, EMAIL_STAFF, EMAIL_DUP, EMAIL_WEAK,
        EMAIL_LINK, EMAIL_LINK_REL, EMAIL_LINK_CLASH, EMAIL_LINK_PW,
        EMAIL_LINK_DUP,
    ]).delete()
    User.objects.filter(phone_number=User.normalize_phone(PHONE_ONLY)).delete()

    Group.objects.filter(name__in=[LINK_GROUP, LINK_GROUP_RELATIVE]).delete()
    opts.link_group = Group.objects.create(
        name=LINK_GROUP, is_active=True,
        metadata={"webapp_base_url": LINK_BASE})
    opts.relative_group = Group.objects.create(
        name=LINK_GROUP_RELATIVE, is_active=True,
        metadata={"webapp_base_url": LINK_BASE_RELATIVE})


@th.django_unit_test()
def test_create_email_superuser(opts):
    from django.core.management import call_command
    from mojo.apps.account.models import User

    out = StringIO()
    call_command('create_user', '--email', EMAIL_SUPERUSER, '--password', 'Str0ng!Passw0rd',
                  '--superuser', stdout=out)

    user = User.objects.get(email=EMAIL_SUPERUSER)
    assert user.is_staff, "superuser creation should also set is_staff"
    assert user.is_superuser, "expected --superuser to set is_superuser"
    assert user.check_password('Str0ng!Passw0rd'), "password should verify after creation"


@th.django_unit_test()
def test_create_phone_only_user(opts):
    from django.core.management import call_command
    from mojo.apps.account.models import User

    out = StringIO()
    call_command('create_user', '--phone', PHONE_ONLY, '--first-name', 'Ada', '--last-name', 'Lovelace',
                  '--password', 'Str0ng!Passw0rd', stdout=out)

    user = User.objects.get(phone_number=User.normalize_phone(PHONE_ONLY))
    assert user.email is None, f"phone-only user should have no email, got {user.email!r}"
    assert user.username, "a username should have been auto-generated"


@th.django_unit_test()
def test_create_staff_with_scoped_permission(opts):
    from django.core.management import call_command
    from mojo.apps.account.models import User

    out = StringIO()
    call_command('create_user', '--email', EMAIL_STAFF, '--password', 'Str0ng!Passw0rd',
                  '--staff', '--permission', 'manage_users', stdout=out)

    user = User.objects.get(email=EMAIL_STAFF)
    assert user.is_staff, "expected --staff to set is_staff"
    assert not user.is_superuser, "should not be superuser without --superuser"
    assert user.has_permission("manage_users"), "expected manage_users permission to be granted"
    assert not user.has_permission("view_logs"), \
        "granting one permission must not leak access to unrelated permissions"


@th.django_unit_test()
def test_create_user_duplicate_email_rejected(opts):
    from django.core.management import call_command, CommandError
    from mojo.apps.account.models import User

    User.objects.filter(email=EMAIL_DUP).delete()
    User.objects.create_user(username=EMAIL_DUP, email=EMAIL_DUP, password="Str0ng!Passw0rd")
    before_count = User.objects.filter(email=EMAIL_DUP).count()

    out = StringIO()
    raised = False
    try:
        call_command('create_user', '--email', EMAIL_DUP, '--password', 'AnotherStr0ng!Pw', stdout=out)
    except CommandError:
        raised = True
    assert raised, "creating a user with a duplicate email should raise CommandError"
    assert User.objects.filter(email=EMAIL_DUP).count() == before_count, \
        "duplicate rejection must not create an extra row"


@th.django_unit_test()
def test_create_user_weak_password_rejected(opts):
    from django.core.management import call_command, CommandError
    from mojo.apps.account.models import User

    User.objects.filter(email=EMAIL_WEAK).delete()

    out = StringIO()
    raised = False
    try:
        call_command('create_user', '--email', EMAIL_WEAK, '--password', 'weak', stdout=out)
    except CommandError:
        raised = True
    assert raised, "a weak password should raise CommandError"
    assert not User.objects.filter(email=EMAIL_WEAK).exists(), \
        "no user row should be created when password strength validation fails"


# ---------------------------------------------------------------------------
# --login-link — the first admin of a freshly provisioned environment
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_login_link_works_without_any_password_source(opts):
    """The whole point of the flag: it runs where there is no tty and no
    password to give, which is what a provisioning run over SSH is."""
    from django.core.management import call_command
    from mojo.apps.account.models import User
    from mojo.apps.account.utils.tokens import verify_password_reset_token

    out = StringIO()
    call_command('create_user', '--email', EMAIL_LINK, '--superuser',
                 '--login-link', '--org', opts.link_group.pk, stdout=out)
    printed = out.getvalue()

    user = User.objects.get(email=EMAIL_LINK)
    assert user.is_superuser, "--superuser must still be honored alongside --login-link"

    assert "Login link: " in printed, \
        f"a login link must be printed, got: {printed!r}"
    url = printed.split("Login link: ", 1)[1].splitlines()[0].strip()
    assert url.startswith(LINK_BASE), \
        f"the link must be absolute and built from the resolved webapp base URL, got {url!r}"
    assert "flow=password_reset" in url, \
        f"the link must open the password-reset flow, got {url!r}"

    token = url.split("token=", 1)[1]
    assert token.startswith("pr:"), \
        f"the link must carry a password-reset token, got {token!r}"
    verified = verify_password_reset_token(token)
    assert verified is not None and verified.pk == user.pk, \
        "the printed token must verify against the account that was just created"

    assert "single use" in printed, \
        "the operator must be told the link is single use and short-lived"


@th.django_unit_test()
def test_login_link_sets_a_password_nobody_is_given(opts):
    """The account is never left password-less, and the password is never shown.

    A blank or well-known password on a brand-new superuser is worse than no
    account at all, so one is set — and then discarded unread, which is why the
    reset link is the only way in.
    """
    from django.core.management import call_command
    from mojo.apps.account.models import User

    User.objects.filter(email=EMAIL_LINK_PW).delete()
    call_command('create_user', '--email', EMAIL_LINK_PW, '--login-link',
                 '--org', opts.link_group.pk, stdout=StringIO())

    created = User.objects.get(email=EMAIL_LINK_PW)
    assert created.password, \
        "the account must carry a password hash, not an unusable empty one"
    for guess in ("", "password", EMAIL_LINK_PW, "changeme"):
        assert not created.check_password(guess), \
            f"the generated password must not be guessable ({guess!r} matched)"


@th.django_unit_test()
def test_login_link_rejects_an_explicit_password(opts):
    """Two intents in one command. Refused rather than silently picking one."""
    from django.core.management import call_command, CommandError
    from mojo.apps.account.models import User

    User.objects.filter(email=EMAIL_LINK_CLASH).delete()
    out = StringIO()
    raised = ""
    try:
        call_command('create_user', '--email', EMAIL_LINK_CLASH,
                     '--password', 'Str0ng!Passw0rd', '--login-link', stdout=out)
    except CommandError as err:
        raised = str(err)

    assert raised, "--login-link with --password must raise CommandError"
    assert "cannot be combined" in raised, \
        f"the refusal must say why, got: {raised!r}"
    assert not User.objects.filter(email=EMAIL_LINK_CLASH).exists(), \
        "no account may be created by a refused invocation"


@th.django_unit_test()
def test_login_link_requires_an_email(opts):
    """A reset link is addressed to an account by email; a phone-only account
    has nowhere for one to go."""
    from django.core.management import call_command, CommandError

    out = StringIO()
    raised = ""
    try:
        call_command('create_user', '--phone', '+15555550199', '--login-link',
                     stdout=out)
    except CommandError as err:
        raised = str(err)

    assert raised, "--login-link without --email must raise CommandError"
    assert "--email" in raised, \
        f"the refusal must name the missing flag, got: {raised!r}"


@th.django_unit_test()
def test_login_link_prints_the_raw_token_when_no_absolute_base_url(opts):
    """A relative BASE_URL yields something that LOOKS like a link and is not.

    Printing it would send the operator to a dead page; the token is what
    actually matters, so that is printed with an explanation instead.
    """
    from django.core.management import call_command
    from mojo.apps.account.models import User
    from mojo.apps.account.utils.tokens import verify_password_reset_token

    User.objects.filter(email=EMAIL_LINK_REL).delete()
    out, err = StringIO(), StringIO()
    call_command('create_user', '--email', EMAIL_LINK_REL, '--login-link',
                 '--org', opts.relative_group.pk, stdout=out, stderr=err)

    printed, warned = out.getvalue(), err.getvalue()
    assert "Login link:" not in printed, \
        f"a relative base URL must not be dressed up as a link, got: {printed!r}"
    assert "BASE_URL" in warned, \
        f"the operator must be told what is missing, got: {warned!r}"

    token = printed.strip().splitlines()[-1].strip()
    assert token.startswith("pr:"), \
        f"the raw token must be printed so the operator can still use it, got {token!r}"
    user = User.objects.get(email=EMAIL_LINK_REL)
    verified = verify_password_reset_token(token)
    assert verified is not None and verified.pk == user.pk, \
        "the raw token must verify against the new account"


@th.django_unit_test()
def test_login_link_duplicate_email_refusal_is_detectable(opts):
    """`provision admin` turns this refusal into re-issue guidance rather than
    a traceback, and it recognizes it by this wording."""
    from django.core.management import call_command, CommandError
    from mojo.apps.account.models import User

    User.objects.filter(email=EMAIL_LINK_DUP).delete()
    User.objects.create_user(username=EMAIL_LINK_DUP, email=EMAIL_LINK_DUP,
                             password="Str0ng!Passw0rd")

    out = StringIO()
    raised = ""
    try:
        call_command('create_user', '--email', EMAIL_LINK_DUP, '--superuser',
                     '--login-link', stdout=out)
    except CommandError as err:
        raised = str(err)

    assert raised, "a duplicate email must still be refused with --login-link"
    assert "already exists" in raised, \
        (f"the message must contain 'already exists' — the provisioning CLI "
         f"matches on it to print re-issue guidance instead of a traceback. "
         f"Got: {raised!r}")
