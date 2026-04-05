"""Service for auto-disabling inactive users and groups."""
from django.db.models import Q
from mojo.helpers import dates, logit
from mojo.helpers.settings import settings

logger = logit.get_logger("inactive_sweep", "inactive_sweep.log")


def _get_account_inactive_days():
    return settings.get("ACCOUNT_INACTIVE_DAYS", 90)


def _get_account_warning_days():
    return settings.get("ACCOUNT_INACTIVE_WARNING_DAYS", 7)


def _get_group_inactive_days():
    return settings.get("GROUP_INACTIVE_DAYS", 90)


def _clear_stale_warnings(Model, inactive_days):
    """Clear warning metadata on entities whose last_activity is more recent than warn date."""
    warned = Model.objects.filter(
        is_active=True,
        metadata__contains={"protected": {"disable_warned": True}},
    )
    cleared = 0
    for entity in warned:
        warn_date_str = entity.get_protected_metadata("disable_warn_date")
        if not warn_date_str:
            continue
        # If last_activity is after the warn date, user reactivated
        # Parse warn_date to datetime for reliable comparison (not string ordering)
        warn_date = dates.parse(warn_date_str)
        if warn_date and entity.last_activity and entity.last_activity > warn_date:
            entity.set_protected_metadata("disable_warned", None)
            entity.set_protected_metadata("disable_warn_date", None)
            cleared += 1
    return cleared


def warn_inactive_users():
    """Send warning emails to users approaching inactivity threshold."""
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
    ).exclude(
        metadata__contains={"protected": {"no_disable": True}},
    ).exclude(
        metadata__contains={"protected": {"disable_warned": True}},
    )

    warned = 0
    for user in users:
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

        user.set_protected_metadata("disable_warned", True)
        user.set_protected_metadata("disable_warn_date", str(dates.utcnow()))

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
    from mojo.apps.incident import report_event

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
    ).exclude(
        metadata__contains={"protected": {"no_disable": True}},
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
    ).exclude(
        metadata__contains={"protected": {"no_disable": True}},
    )

    disabled = 0
    for qs in [users, legacy]:
        for user in qs:
            # Atomic update to avoid race condition
            updated = User.objects.filter(
                pk=user.pk,
                is_active=True,
            ).update(is_active=False)
            if not updated:
                continue

            # Clear warning metadata
            user.refresh_from_db()
            if user.get_protected_metadata("disable_warned"):
                user.set_protected_metadata("disable_warned", None)
                user.set_protected_metadata("disable_warn_date", None)

            days_inactive = (dates.utcnow() - (user.last_activity or user.last_login)).days

            User.class_logit(
                None,
                f"Auto-disabled inactive user {user.username} (id={user.id}), {days_inactive} days inactive",
                kind="auto_disabled",
                model_id=user.id,
                level="warn",
            )

            report_event(
                details=f"Auto-disabled user {user.username} (id={user.id}, email={user.email}), {days_inactive} days inactive",
                title=f"Auto-disabled: {user.username}",
                category="account:auto_disabled",
                level=4,
                uid=user.id,
                model_name="account.User",
                model_id=user.id,
            )
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
    ).exclude(
        metadata__contains={"protected": {"no_disable": True}},
    ).exclude(
        metadata__contains={"protected": {"disable_warned": True}},
    )

    # Find system users with manage_groups or groups permission
    admin_users = User.objects.filter(
        is_active=True,
    ).filter(
        Q(permissions__has_key="manage_groups") | Q(permissions__has_key="groups")
    )

    warned = 0
    for group in groups:
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

        group.set_protected_metadata("disable_warned", True)
        group.set_protected_metadata("disable_warn_date", str(dates.utcnow()))

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
    from mojo.apps.incident import report_event

    inactive_days = _get_group_inactive_days()
    disable_cutoff = dates.subtract(days=inactive_days)

    groups = Group.objects.filter(
        is_active=True,
        last_activity__lt=disable_cutoff,
        last_activity__isnull=False,
    ).exclude(
        metadata__contains={"protected": {"no_disable": True}},
    )

    disabled = 0
    for group in groups:
        updated = Group.objects.filter(
            pk=group.pk,
            is_active=True,
        ).update(is_active=False)
        if not updated:
            continue

        group.refresh_from_db()
        if group.get_protected_metadata("disable_warned"):
            group.set_protected_metadata("disable_warned", None)
            group.set_protected_metadata("disable_warn_date", None)

        days_inactive = (dates.utcnow() - group.last_activity).days
        member_count = group.members.filter(is_active=True).count()

        Group.class_logit(
            None,
            f"Auto-disabled inactive group {group.name} (id={group.id}), {days_inactive} days inactive, {member_count} members",
            kind="auto_disabled",
            model_id=group.id,
            level="warn",
        )

        report_event(
            details=f"Auto-disabled group {group.name} (id={group.id}), {days_inactive} days inactive, {member_count} members",
            title=f"Auto-disabled group: {group.name}",
            category="group:auto_disabled",
            level=4,
            model_name="account.Group",
            model_id=group.id,
        )
        disabled += 1

    return disabled
