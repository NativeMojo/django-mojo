"""
Django management command to create a user, optionally granting admin access.

Bootstraps the first admin (or any additional user) for a fresh deployment.
Django's built-in `createsuperuser` does not work against this project's
custom User model — see docs/django_developer/account/bootstrap.md for why —
so this command is the supported replacement.

Usage:
    python manage.py create_user --email admin@example.com --superuser
    python manage.py create_user --phone +15551234567 --first-name Ada --last-name Lovelace
    python manage.py create_user --email ops@example.com --staff --permission manage_users --permission users
    python manage.py create_user --email admin@example.com --superuser --login-link

`--login-link` is the non-interactive bootstrap path: it needs no password
source at all, which is what makes it usable over SSH from a provisioning run
where there is no terminal to type one at.
"""

import os
import sys
import getpass

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from mojo import errors as merrors


class Command(BaseCommand):
    help = "Create a user (email-based or phone-only), optionally granting admin access."

    def add_arguments(self, parser):
        parser.add_argument('--username', default=None,
            help='Username to assign. Auto-generated from email/name when omitted.')
        parser.add_argument('--email', default=None, help='Email address.')
        parser.add_argument('--phone', default=None, help='Phone number (for phone-only accounts).')
        parser.add_argument('--first-name', default='', help='First name.')
        parser.add_argument('--last-name', default='', help='Last name.')
        parser.add_argument('--password', default=None,
            help='Password (visible in shell history / process list — prefer --password-env).')
        parser.add_argument('--password-env', default=None,
            help='Name of an environment variable to read the password from.')
        parser.add_argument('--login-link', action='store_true',
            help='Do not choose a password. Set an unguessable one, discard it, and print a '
                 'single-use password-reset link the new user opens to pick their own. '
                 'Requires --email; cannot be combined with --password/--password-env.')
        parser.add_argument('--staff', action='store_true', help='Grant is_staff.')
        parser.add_argument('--superuser', action='store_true',
            help='Grant is_superuser (implies --staff; the only true full-access grant).')
        parser.add_argument('--permission', action='append', dest='permissions', default=[],
            help='Permission key to grant (repeatable). See docs/django_developer/account/bootstrap.md '
                 'for the portal-section reference table.')
        parser.add_argument('--org', type=int, default=None, help='Group id to set as the org.')

    def handle(self, *args, **options):
        from mojo.apps.account.models import User, Group

        email = (options['email'] or '').strip() or None
        phone = (options['phone'] or '').strip() or None
        if not email and not phone:
            raise CommandError("Provide --email and/or --phone.")

        if options['login_link']:
            # Mutually exclusive rather than "last one wins": a run that was
            # given a password AND asked for a link has two different intents
            # in it, and silently honouring one of them is how an operator ends
            # up believing an account has a password it does not.
            if options['password'] or options['password_env']:
                raise CommandError(
                    "--login-link sets no password of its own, so it cannot be "
                    "combined with --password or --password-env. Pick one.")
            if not email:
                raise CommandError(
                    "--login-link prints a password-reset link, which is "
                    "addressed to an account by email. Add --email.")

        if email and User.objects.filter(email=email).exists():
            raise CommandError(f"A user with email '{email}' already exists.")
        if phone:
            normalized_phone = User.normalize_phone(phone)
            if normalized_phone and User.objects.filter(phone_number=normalized_phone).exists():
                raise CommandError(f"A user with phone '{phone}' already exists.")
        username = options['username']
        if username and User.objects.filter(username=username).exists():
            raise CommandError(f"A user with username '{username}' already exists.")

        password = self._resolve_password(options)

        org = None
        if options['org'] is not None:
            try:
                org = Group.objects.get(pk=options['org'])
            except Group.DoesNotExist:
                raise CommandError(f"No Group with id={options['org']} exists.")

        try:
            with transaction.atomic():
                user = User(email=email)
                if options['first_name']:
                    user.first_name = options['first_name']
                if options['last_name']:
                    user.last_name = options['last_name']
                if phone:
                    user.set_phone_number(phone)

                if username:
                    user.username = username
                elif email:
                    user.username = user.generate_username_from_email()
                else:
                    user.username = user.generate_username_from_names(fallback=phone)

                user.check_password_strength(password)
                user.set_password(password)

                # on_rest_pre_save/on_rest_created don't fire on a direct .save(),
                # so mirror the profile setup + validation on_register does
                # (mojo/apps/account/rest/user.py:470-514) explicitly here.
                user.infer_names_from_email()
                if not user.display_name:
                    user.display_name = user.generate_display_name()
                user.validate_username()
                if email:
                    user.validate_email()
                user.validate_name_fields({}, created=True)

                # set_is_staff/set_is_superuser require an already-superuser
                # active_user, which doesn't exist yet when bootstrapping the
                # first admin — so these are plain field assignments, not the
                # REST-mediated setters. Do not "fix" this into a permission
                # check; it would make bootstrap impossible again.
                user.is_staff = options['staff'] or options['superuser']
                user.is_superuser = options['superuser']

                if org is not None:
                    user.org = org

                user.save()

                if options['permissions']:
                    user.add_permission(options['permissions'], commit=True)
        except merrors.MojoException as e:
            raise CommandError(str(e))

        self.stdout.write(self.style.SUCCESS(
            f"Created user '{user.username}' (id={user.pk})"
            f"{' [staff]' if user.is_staff else ''}"
            f"{' [superuser]' if user.is_superuser else ''}"
        ))
        if options['permissions']:
            self.stdout.write(f"Permissions granted: {', '.join(options['permissions'])}")
        if options['login_link']:
            self._print_login_link(user)

    def _print_login_link(self, user):
        """A single-use password-reset link for an account nobody has a password to.

        This is how the FIRST admin of a freshly provisioned environment gets in:
        the password set above was random and was never shown to anyone, so the
        link is the only way through — and because it is a reset token, the
        operator ends up choosing their own password rather than being handed
        one that lived in a terminal scrollback.
        """
        from mojo.apps.account.utils.tokens import generate_password_reset_token
        from mojo.apps.account.utils.webapp_url import build_token_url

        token = generate_password_reset_token(user)
        url = build_token_url("password_reset", token, user=user)

        # A relative or empty BASE_URL yields something like "/auth?flow=..." —
        # not a link anyone can open, and worse, one that LOOKS like a link.
        # Print the raw token instead and say what is missing.
        if not str(url).lower().startswith(("http://", "https://")):
            self.stderr.write(self.style.WARNING(
                "BASE_URL (or WEBAPP_BASE_URL) is unset or relative, so no "
                "absolute login link can be built. Set it and re-issue, or "
                "paste this token into the reset form by hand:"))
            self.stdout.write(token)
            return

        # Deliberately unstyled: this line is meant to be copied, and an
        # ANSI-wrapped URL breaks that everywhere it is pasted.
        self.stdout.write(f"Login link: {url}")
        self.stdout.write(
            "This link is single use and expires in one hour. It sets the "
            "account's password; it does not reveal one.")

    def _resolve_password(self, options):
        if options['login_link']:
            # The same generator and strength check the portal's
            # "issue a temporary password" admin action uses
            # (mojo/apps/account/services/admin_passwords.py). The value is
            # generated, set, and discarded unread — nothing prints it, and the
            # reset link below is the only way into the account.
            from mojo.helpers import crypto

            return crypto.random_string(18, True, True, True)

        if options['password']:
            self.stderr.write(self.style.WARNING(
                "--password is visible in shell history and process list; "
                "prefer --password-env or the interactive prompt."))
            return options['password']

        if options['password_env']:
            value = os.environ.get(options['password_env'])
            if not value:
                raise CommandError(f"Environment variable '{options['password_env']}' is unset or empty.")
            return value

        if not sys.stdin.isatty():
            raise CommandError(
                "No password source available in a non-interactive shell. "
                "Pass --password or --password-env.")

        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Password (again): ")
        if password != confirm:
            raise CommandError("Passwords did not match.")
        return password
