from mojo.helpers import dates


def prune_notifications(job):
    from mojo.apps.account.models.notification import Notification
    Notification.objects.filter(expires_at__lt=dates.utcnow()).delete()


def refresh_bouncer_sig_cache(job):
    """Scheduled job: rebuild Redis signature cache from active BotSignature records."""
    from mojo.apps.account.services.bouncer.learner import refresh_sig_cache
    refresh_sig_cache()


def inactive_sweep(job):
    """Nightly sweep: warn and disable inactive users and groups."""
    from mojo.helpers.settings import settings
    from mojo.helpers import logit

    results = {}

    if settings.get("ACCOUNT_AUTO_DISABLE_ENABLED", False):
        from mojo.apps.account.services.inactive import (
            _clear_stale_warnings, warn_inactive_users, disable_inactive_users,
        )
        from mojo.apps.account.models import User
        cleared = _clear_stale_warnings(User, settings.get("ACCOUNT_INACTIVE_DAYS", 90))
        warned = warn_inactive_users()
        disabled = disable_inactive_users()
        results["users"] = {"warnings_cleared": cleared, "warned": warned, "disabled": disabled}
        logit.info(f"Inactive user sweep: {cleared} warnings cleared, {warned} warned, {disabled} disabled")

    if settings.get("GROUP_AUTO_DISABLE_ENABLED", False):
        from mojo.apps.account.services.inactive import (
            _clear_stale_warnings, warn_inactive_groups, disable_inactive_groups,
        )
        from mojo.apps.account.models import Group
        cleared = _clear_stale_warnings(Group, settings.get("GROUP_INACTIVE_DAYS", 90))
        warned = warn_inactive_groups()
        disabled = disable_inactive_groups()
        results["groups"] = {"warnings_cleared": cleared, "warned": warned, "disabled": disabled}
        logit.info(f"Inactive group sweep: {cleared} warnings cleared, {warned} warned, {disabled} disabled")

    return results
