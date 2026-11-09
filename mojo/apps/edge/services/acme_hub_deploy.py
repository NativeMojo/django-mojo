"""Shared ACME's narrow admission policy on the existing deployment plane."""
from functools import wraps


def _installed():
    from django.apps import apps
    return apps.is_installed("mojo.apps.dnsman")


def guarded(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not _installed():
            return function(*args, **kwargs)
        from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
        with upgrade.admission():
            return function(*args, **kwargs)
    return wrapped


def check_framework(version):
    if not _installed():
        return version
    from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
    if upgrade.enabled():
        state = upgrade.read_state()
        if upgrade._release(version) < upgrade._release(state.minimum_release):
            upgrade.refuse("framework_below_acme_floor")
    return version


def check_dispatch(deployment_id, sha, framework, runner, migrate, api_cohort,
                   *, capacity_operation=None):
    from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
    from mojo.apps.edge.services import platform_deploy, deploy
    row = platform_deploy.get(deployment_id)
    if row is None or row.sha != sha or row.framework_version != framework:
        upgrade.refuse("deployment_identity_mismatch")
    check_framework(framework)
    if capacity_operation:
        from mojo.apps.aws.services import capacity
        operation = capacity._read_operation(capacity_operation)
        latest = platform_deploy.last_converged_deployment()
        detail = (operation or {}).get("detail") or {}
        if (row.status != "converged" or latest is None or latest.pk != row.pk or migrate
                or not operation or operation.get("action") != capacity.ACTION_ADD_NODE
                or operation.get("state") != capacity.STATE_RUNNING
                or operation.get("phase") != "converging"
                or detail.get("runner_id") != runner or not detail.get("instance_id")):
            upgrade.refuse("capacity_admission_unavailable")
        return row
    roster = set(row.frozen_roster or [])
    raw_api = (row.detail or {}).get("api_roster")
    api_roster = set(raw_api) if isinstance(raw_api, list) else roster
    if (row.status not in ("canary", "fleet") or runner not in roster
            or bool(api_cohort) != (runner in api_roster) or (migrate and not api_cohort)):
        upgrade.refuse("stale_deployment_dispatch")
    target = deploy.get_target() or {}
    if target and target.get("deployment") != str(row.pk):
        upgrade.refuse("stale_deployment_dispatch")
    return row


def before_spawn(job):
    from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
    from mojo.apps.edge.services import deploy
    payload = job.payload or {}
    runner = job.runner_id or deploy.local_runner_id()
    if payload.get("runner") != runner:
        upgrade.refuse("deployment_runner_mismatch")
    check_dispatch(payload.get("deployment"), payload.get("sha"), payload.get("framework"),
                   runner, bool(payload.get("migrate")), bool(payload.get("api_cohort", True)),
                   capacity_operation=payload.get("capacity_operation"))
    if _installed():
        upgrade.check_session()
