"""Audited administrator password operations for selected users."""

import uuid

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction

from mojo import errors as merrors
from mojo.apps.account.models import User
from mojo.apps.account.utils import tokens
from mojo.apps.account.utils.webapp_url import build_token_url
from mojo.apps.shortlink import maybe_shorten_url
from mojo.helpers import crypto


def _target(user_id):
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        raise merrors.ValueException("user must be an integer")
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        raise merrors.ValueException("User not found")
    return user


def send_reset_link(request, user_id):
    """Send the selected user an administrator-issued password-reset link."""
    user = _target(user_id)
    if not user.email:
        raise merrors.ValueException("The selected user has no email address")
    token = tokens.generate_password_reset_token(user)
    token_url = build_token_url(
        "password_reset", token, request=request, user=user,
        group=getattr(request, "group", None))
    token_url = maybe_shorten_url(
        token_url, source="admin_password_reset", user=user, expire_hours=1)
    user.send_template_email(
        "password_reset_link", {"token": token, "token_url": token_url})
    user.log(
        f"Password reset link issued by administrator {request.user.pk}",
        "password:admin_reset_link")
    return {"user": user.pk, "sent": True}


def issue_temporary_password(request, user_id):
    """Set and reveal one generated temporary password exactly once."""
    target = _target(user_id)
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=target.pk).first()
        if user is None:
            raise merrors.ValueException("User not found")
        temporary_password = crypto.random_string(18, True, True, True)
        user.check_password_strength(temporary_password)
        # Bypass User.set_password intentionally: that method is the permanent
        # password choke point and clears this flag.
        AbstractBaseUser.set_password(user, temporary_password)
        user.requires_password_change = True
        user.auth_key = uuid.uuid4().hex
        user.save(update_fields=[
            "password", "requires_password_change", "auth_key", "modified"])
        user.log(
            f"Temporary password issued by administrator {request.user.pk}",
            "password:temporary_issued")

    from mojo.apps.account.services.disable import disconnect_realtime
    disconnect_realtime(user, request=request)
    return {
        "user": user.pk,
        "temporary_password": temporary_password,
        "requires_password_change": True,
    }
