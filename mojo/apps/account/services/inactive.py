"""Service for auto-disabling inactive users and groups.

State for this service lives under `metadata.protected.disable.*` (see
`disable_lifecycle.md`). Legacy keys `disable_warned`, `disable_warn_date`, and
`no_disable` are still honoured on read for one release; writes always use the
new namespace.
"""
from django.db.models import Q
from mojo.helpers import dates, logit
from mojo.helpers.settings import settings
from mojo.apps.account.services import disable as disable_service

logger = logit.get_logger("inactive_sweep", "inactive_sweep.log")


def _get_account_inactive_days():
    return settings.get("ACCOUNT_INACTIVE_DAYS", 90)


def _get_account_warning_days():
    return settings.get("ACCOUNT_INACTIVE_WARNING_DAYS", 7)


def _get_group_inactive_days():
    return settings.get("GROUP_INACTIVE_DAYS", 90)


def _clear_stale_warnings(Model, inactive_days):
    """Clear warning markers on entities whose last_activity is more recent than warn date.

    Filters candidates in Python rather than via JSONField path lookups so the dual
    legacy/new shape check is straightforward and version-portable.
    """
    candidates = Model.objects.filter(is_active=True, metadata__has_key="protected")
    cleared = 0
    for entity in candidates:
        if not disable_service.has_warning(entity):
            continue
        warn_date_str = disable_service.get_warning_sent_at(entity)
        if not warn_date_str:
            continue
        warn_date = dates.parse(warn_date_str)
        if warn_date and entity.last_activity and entity.last_activity > warn_date:
            disable_service.clear_warning(entity)
            cleared += 1
    return cleared


def warn_inactive_users():
    """Send warning emails to users approaching the inactivity threshold."""
    from mojo.apps.account.models import User
    from mojo.apps.incident import report_event

    inactive_days = _get_account_inactive_days()
    warning_days = _get_account_warning_days()
    warn_cutoff = dates.subtract(days=inactive_days - warning_days)

    users = User.objects.filter(
        is_active=True,
        last_activity__lt=warn_cutoff,
        last_activity__isnull=False,
    ).exclude(
        is_superuser=True,
    ).exclude(
        is_staff=True,
    )

    warned = 0
    for user in users:
        if disable_service.is_exempt(user):
            continue
        if disable_service.has_warning(user):
            continue
        days_until = inactive_days - (dates.utcnow() - user.last_activity).days
        if days_until < 0:
            days_until = 0
        try:
            user.send_template_email(
                "account_inactive_warning",
                context={
                    "days_until_disable": days_until,
                    "inactive_days": inactive_days,
                },
            )
        except Exception as err:
            logger.error(f"Failed to send inactive warning to user {user.id}: {err}")

        disable_service.mark_warning(user, days_until_disable=days_until)

        report_event(
            details=f"Inactive warning sent to user {user.username} (id={user.id}), {days_until} days until disable",
            title=f"Inactive warning: {user.username}",
            category="account:inactive_warning",
            level=2,
            uid=user.id,
            model_name="account.User",
            model_id=user.id,
        )
        warned += 1

    return warned


def disable_inactive_users():
    """Disable users past the inactivity threshold."""
    from mojo.apps.account.models import User
    from mojo import errors as merrors

    inactive_days = _get_account_inactive_days()
    disable_cutoff = dates.subtract(days=inactive_days)

    users = User.objects.filter(
        is_active=True,
        last_activity__lt=disable_cutoff,
        last_activity__isnull=False,
    ).exclude(
        is_superuser=True,
    ).exclude(
        is_staff=True,
    )
    # Also catch users with null last_activity but old last_login
    legacy = User.objects.filter(
        is_active=True,
        last_activity__isnull=True,
        last_login__lt=disable_cutoff,
        last_login__isnull=False,
    ).exclude(
        is_superuser=True,
    ).exclude(
        is_staff=True,
    )

    disabled = 0
    for qs in [users, legacy]:
        for user in qs:
            if disable_service.is_exempt(user):
                continue
            try:
                disable_service.disable_entity(user, reason="inactive", by_user=None)
            except merrors.ValueException:
                # Already disabled by a concurrent worker — skip.
                continue
            disabled += 1

    return disabled


def warn_inactive_groups():
    """Send warning emails to group admins for groups approaching inactivity threshold."""
    from mojo.apps.account.models import User, Group
    from mojo.apps.incident import report_event

    inactive_days = _get_group_inactive_days()
    warning_days = _get_account_warning_days()
    warn_cutoff = dates.subtract(days=inactive_days - warning_days)

    groups = Group.objects.filter(
        is_active=True,
        last_activity__lt=warn_cutoff,
        last_activity__isnull=False,
    )

    # Find system users with manage_groups or groups permission
    admin_users = User.objects.filter(
        is_active=True,
    ).filter(
        Q(permissions__has_key="manage_groups") | Q(permissions__has_key="groups")
    )

    warned = 0
    for group in groups:
        if disable_service.is_exempt(group):
            continue
        if disable_service.has_warning(group):
            continue
        days_until = inactive_days - (dates.utcnow() - group.last_activity).days
        if days_until < 0:
            days_until = 0

        if not admin_users.exists():
            logger.warning(f"No admin users found to warn about inactive group {group.name} (id={group.id})")
        else:
            for admin in admin_users:
                try:
                    admin.send_template_email(
                        "group_inactive_warning",
                        context={
                            "group_name": group.name,
                            "group_id": group.id,
                            "days_until_disable": days_until,
                            "inactive_days": inactive_days,
                        },
                    )
                except Exception as err:
                    logger.error(f"Failed to send group inactive warning to user {admin.id} for group {group.id}: {err}")

        disable_service.mark_warning(group, days_until_disable=days_until)

        report_event(
            details=f"Inactive warning for group {group.name} (id={group.id}), {days_until} days until disable",
            title=f"Inactive group warning: {group.name}",
            category="group:inactive_warning",
            level=2,
            model_name="account.Group",
            model_id=group.id,
        )
        warned += 1

    return warned


def disable_inactive_groups():
    """Disable groups past the inactivity threshold."""
    from mojo.apps.account.models import Group
    from mojo import errors as merrors

    inactive_days = _get_group_inactive_days()
    disable_cutoff = dates.subtract(days=inactive_days)

    groups = Group.objects.filter(
        is_active=True,
        last_activity__lt=disable_cutoff,
        last_activity__isnull=False,
    )

    disabled = 0
    for group in groups:
        if disable_service.is_exempt(group):
            continue
        try:
            disable_service.disable_entity(group, reason="inactive", by_user=None)
        except merrors.ValueException:
            continue
        disabled += 1

    return disabled
