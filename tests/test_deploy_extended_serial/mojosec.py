"""Moved from tests/test_deploy/mojosec.py (maestro #2558).

Converge/rotation/enrollment orchestration coverage: these tests mock
`mojo.deploy.mojosec` internals (`_systemctl`, `_write_if_changed`,
`deploy.os`, `deploy.pwd`, `deploy.sys`, ...) and `mojo.deploy.audit` —
process-global patches of production module attributes, unsafe under the
parallel default tier. The pure rendering, policy-boundary, unit-text, and
filesystem contracts stay in the default-tier tests/test_deploy/mojosec.py.
"""

import io
import contextlib
import json
import os
import tempfile
from unittest import mock

from testit import helpers as th


@contextlib.contextmanager
def _provenance_deploy_mocks(deploy):
    with mock.patch.object(deploy.pwd, "getpwnam",
                           return_value=mock.Mock(pw_uid=1000, pw_gid=1000)), \
            mock.patch("mojo.deploy.audit.converge", return_value={
                "generation": "a" * 64, "rules_sha256": "b" * 64}), \
            mock.patch("mojo.deploy.audit.restore_immediate"), \
            mock.patch("mojo.deploy.audit.restore_prior"), \
            mock.patch.object(deploy.subprocess, "run",
                              return_value=mock.Mock(returncode=0, stderr="")):
        yield


@th.django_unit_test()
def test_active_nginx_audit_requires_cap_inside_both_exact_routes(opts):
    from mojo.deploy import mojosec as deploy

    misleading = """
log_format mojosec_v1 escape=json '{}';
access_log /var/log/nginx/mojosec.json.log mojosec_v1;
client_max_body_size 512k;
location = /api/incident/mojosec/batch { proxy_pass http://asgi_upstream; }
location = /api/incident/mojosec/batch/ { proxy_pass http://asgi_upstream; }
"""
    with mock.patch.object(deploy, "_run", return_value=misleading):
        with th.assert_raises(deploy.DeployError):
            deploy._audit_active_nginx(deploy.DEFAULT_LOG_PATH, [])


@th.django_unit_test()
def test_named_profile_is_expanded_once_into_effective_config(opts):
    from mojo.deploy import mojosec as deploy
    import mojo.mojosec.config as config_module
    import mojo.mojosec.__main__ as cli
    from mojo.mojosec.config import load_effective_config
    from mojo.mojosec.profiles import resolve_profile

    desired = {
        "version": 1,
        "profile": "al2023-web-v2",
        "policy_revision": "fleet-profile-r1",
    }
    enrollment = {
        "version": 1,
        "sensor_id": "prod-web-i-0123456789abcdef0",
        "endpoint": "https://incident.example/api/incident/mojosec/batch",
        "fim_allowed_roots": ["/etc"],
        "trusted_proxy_cidrs": [],
    }

    def read(path, *args, **kwargs):
        value = desired if path == deploy.DESIRED_CONFIG_PATH else enrollment
        return value, json.dumps(value, sort_keys=True).encode("utf-8")

    config, payload, _ = deploy._prepare_effective_config(loader=read)

    profile = resolve_profile("al2023-web-v2")
    th.assert_eq(config["collectors"]["fim"]["tiers"], profile["tiers"],
                 "effective preparation must preserve every immutable profile FIM tier")
    th.assert_eq(config["collectors"]["rpm"], dict(profile["rpm"], enabled=True),
                 "effective preparation must preserve the complete immutable RPM graph")
    th.assert_eq(json.loads(payload), config,
                 "persisted canonical bytes must contain the validated effective config")

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "config.json")
        with open(path, "wb") as handle:
            handle.write(payload)
        os.chmod(path, 0o600)
        real_fstat = config_module.os.fstat

        def root_fstat(descriptor):
            values = list(real_fstat(descriptor))
            values[4] = 0
            return os.stat_result(values)

        with mock.patch.object(config_module.os, "fstat", side_effect=root_fstat):
            loaded = load_effective_config(path)
            with mock.patch.object(deploy, "_lstat_regular"):
                audited = deploy._audit_config(path)
            th.assert_eq(loaded, config,
                         "descriptor loading must preserve the exact prepared artifact")
            th.assert_eq(audited, config,
                         "deployment audit must validate without re-expanding the profile")

            with mock.patch.object(cli, "probe_rpm_capability"):
                for argv in (["check"], ["--config", path, "check"]):
                    output = io.StringIO()
                    with mock.patch.object(cli, "CANONICAL_CONFIG_PATH", path):
                        th.assert_eq(
                            cli.main(argv, stdout=output), 0,
                            "implicit and explicit canonical CLI checks must accept the artifact")
                    th.assert_eq(
                        json.loads(output.getvalue())["sensor_id"], config["sensor_id"],
                        "CLI check must report the prepared sensor identity")

            alias = os.path.join(root, ".", "config.json")
            with mock.patch.object(cli, "CANONICAL_CONFIG_PATH", path):
                th.assert_eq(cli.main(
                    ["--config", alias, "check"], stderr=io.StringIO()), 2,
                             "a canonical-path alias must remain untrusted caller policy")


@th.django_unit_test()
def test_security_log_is_precreated_master_opened_and_root_only(opts):
    from mojo.deploy import mojosec as deploy

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "mojosec.json.log")
        with mock.patch.object(deploy, "DEFAULT_LOG_PATH", path), \
                mock.patch.object(deploy, "_require_root_install_dir"), \
                mock.patch.object(deploy.os, "fchown") as chown:
            th.assert_true(deploy._ensure_security_log(path),
                           "first convergence must securely precreate the evidence inode")
        th.assert_eq(os.stat(path).st_mode & 0o777, 0o600,
                     "nginx master-opened raw evidence must be inaccessible to group/other")
        chown.assert_called_once_with(mock.ANY, 0, 0)


@th.django_unit_test()
def test_python_310_observe_degrades_before_any_privileged_mutation(opts):
    from mojo.deploy import mojosec as deploy

    with mock.patch.object(deploy.sys, "version_info", (3, 10, 14)), \
            mock.patch.object(deploy, "_ensure_dir") as ensure_dir, \
            mock.patch.object(deploy, "_prepare_effective_config") as prepare:
        result = deploy.converge("observe", "required")
    th.assert_true(result["ok"] is False and "3.11" in result["warning"],
                   "unsupported observe must degrade with the version requirement "
                   "named, never abort the deploy (item 2007)")
    th.assert_true(not ensure_dir.called and not prepare.called,
                   "unsupported observe must degrade before config or filesystem mutation")


@th.django_unit_test()
def test_converge_lifecycle_is_an_exact_allowlist(opts):
    from mojo.deploy import mojosec as deploy

    calls = []
    states = {"enabled": False, "active": False}

    def systemctl(*args):
        calls.append(args)
        if args[:2] == ("enable", "--now"):
            states.update(enabled=True, active=True)
        elif args[0] == "restart":
            states["active"] = True

    with mock.patch.object(deploy, "_ensure_dir"), \
            mock.patch.object(deploy, "_prepare_home_binds"), \
            mock.patch.object(deploy, "_require_root_install_dir"), \
            mock.patch.object(deploy, "_retire_stale_units"), \
            mock.patch.object(deploy, "_retired_unit_snapshot", return_value={}), \
            mock.patch.object(deploy, "_owned_snapshot", return_value=None), \
            mock.patch.object(deploy, "_nginx_snapshot", return_value={}), \
            mock.patch.object(deploy, "_prepare_effective_config", return_value=(
                {"sensor_id": "i-host", "config_provenance": {}}, b"{}\n",
                {"trusted_proxy_cidrs": [], "nginx_plane": "standard",
                 "nginx_log_path": deploy.DEFAULT_LOG_PATH})), \
            mock.patch.object(deploy, "_write_if_changed", return_value=True), \
            mock.patch.object(deploy, "_converge_nginx", return_value=False), \
            mock.patch.object(deploy, "_audit_active_nginx"), \
            mock.patch.object(deploy, "_audit_config", return_value={}), \
            mock.patch.object(deploy, "_lstat_regular"), \
            mock.patch.object(deploy, "_systemctl_is",
                              side_effect=lambda verb, unit: states[verb[3:]]), \
            mock.patch.object(deploy, "_systemctl", side_effect=systemctl), \
            _provenance_deploy_mocks(deploy), \
            mock.patch.object(deploy.os, "geteuid", return_value=0):
        result = deploy.converge("observe", "required")

    th.assert_eq(result["mode"], "observe", "observe convergence must report its mode")
    th.assert_true(all(
        not call or call[-1] in (
            "daemon-reload", "nginx", deploy.SERVICE, "mojosec-audit-health.timer")
        for call in calls),
        f"lifecycle may address only nginx and the exact MojoSec unit: {calls}")
    th.assert_in(("enable", "--now", deploy.SERVICE), calls,
                 "observe must enable and start the exact service")
    th.assert_true(not any("*.service" in part for call in calls for part in call),
                   "deployment must never enable a service glob")


@th.django_unit_test()
def test_off_and_best_effort_preserve_evidence(opts):
    from mojo.deploy import mojosec as deploy

    calls = []
    states = {"enabled": True, "active": True}

    def systemctl(*args):
        calls.append(args)
        if args == ("stop", deploy.SERVICE):
            states["active"] = False
        elif args == ("disable", deploy.SERVICE):
            states["enabled"] = False

    with mock.patch.object(deploy, "_ensure_dir"), \
            mock.patch.object(deploy, "_require_root_install_dir"), \
            mock.patch.object(deploy, "_retire_stale_units"), \
            mock.patch.object(deploy, "_retired_unit_snapshot", return_value={}), \
            mock.patch.object(deploy, "_owned_snapshot", return_value=None), \
            mock.patch.object(deploy, "_nginx_snapshot", return_value={}), \
            mock.patch.object(deploy, "_write_if_changed", side_effect=[False, True]), \
            mock.patch.object(deploy, "_converge_nginx", return_value=True), \
            mock.patch.object(deploy, "_systemctl_is",
                              side_effect=lambda verb, unit: states[verb[3:]]), \
            mock.patch.object(deploy, "_systemctl", side_effect=systemctl), \
            mock.patch("mojo.deploy.audit.flush_pending_firewall") as flush, \
            mock.patch.object(deploy.os, "geteuid", return_value=0):
        result = deploy.converge("off", "required")
    th.assert_in(("stop", deploy.SERVICE), calls,
                 "off must stop and disable a prior installation")
    th.assert_in(("disable", deploy.SERVICE), calls,
                 "off must keep the quiesced sensor disabled")
    flush.assert_called_once_with()
    th.assert_eq(result["spool_preserved"], True,
                 "off/rollback must preserve local evidence")

    with mock.patch.object(deploy, "_prepare_effective_config",
                           side_effect=OSError("denied")), \
            mock.patch.object(deploy.os, "geteuid", return_value=0):
        warning = deploy.converge("observe", "best_effort")
    th.assert_eq(warning["ok"], False,
                 "best_effort must return an explicit warning rather than claim success")


@th.django_unit_test()
def test_off_restores_audit_and_removes_feature_assets(opts):
    from mojo.deploy import audit
    from mojo.deploy import mojosec as deploy
    from mojo.deploy.firewall_broker import BROKER_PATH, SUDOERS_PATH

    removed = []
    calls = []
    states = {"enabled": True, "active": True}

    def systemctl(*args):
        calls.append(args)
        if args == ("stop", deploy.SERVICE):
            states["active"] = False
        elif args == ("disable", deploy.SERVICE):
            states["enabled"] = False

    with mock.patch.object(deploy.os, "geteuid", return_value=0), \
            mock.patch.object(deploy, "_ensure_dir"), \
            mock.patch.object(deploy, "_require_root_install_dir"), \
            mock.patch.object(deploy, "_owned_snapshot", return_value={
                "path": "prior", "payload": b"prior", "mode": 0o600}), \
            mock.patch.object(deploy, "_retired_unit_snapshot", return_value={}), \
            mock.patch.object(deploy, "_retire_stale_units", return_value=False), \
            mock.patch.object(deploy, "_nginx_snapshot", return_value={}), \
            mock.patch.object(deploy, "_write_if_changed", return_value=False), \
            mock.patch.object(deploy, "_converge_nginx", return_value=False), \
            mock.patch.object(deploy, "_restore_nginx"), \
            mock.patch.object(deploy, "_restore_unit_set"), \
            mock.patch.object(deploy, "_restore_snapshot"), \
            mock.patch.object(deploy, "_systemctl_is",
                              side_effect=lambda verb, unit: states[verb[3:]]), \
            mock.patch.object(deploy, "_systemctl", side_effect=systemctl), \
            mock.patch.object(deploy, "_remove_owned",
                              side_effect=lambda path: removed.append(path) or True), \
            mock.patch.object(audit, "flush_pending_firewall") as flush, \
            mock.patch.object(audit, "restore_prior") as restore:
        deploy.converge("off", "required")

    flush.assert_called_once_with()
    restore.assert_called_once_with()
    for path in (BROKER_PATH, SUDOERS_PATH, deploy.AUDIT_HEALTH_SERVICE_PATH,
                 deploy.AUDIT_HEALTH_TIMER_PATH, deploy.AUDIT_STABLE_HELPER_PATH,
                 audit.HEALTH_PATH):
        th.assert_in(path, removed,
                     f"off must remove package-owned provenance asset {path}")
    th.assert_in(("disable", "--now", "mojosec-audit-health.timer"), calls,
                 "off must disable the privileged Audit health publisher")


@th.django_unit_test()
def test_off_refuses_managed_audit_without_rollback_inventory(opts):
    from mojo.deploy import audit
    from mojo.deploy import mojosec as deploy

    def snapshot(path):
        if path == audit.MANAGED_PATH:
            return (b"managed", 0o600)
        return None

    states = {"enabled": True, "active": True}

    def systemctl(*args):
        if args == ("stop", deploy.SERVICE):
            states["active"] = False

    with mock.patch.object(deploy.os, "geteuid", return_value=0), \
            mock.patch.object(deploy, "_ensure_dir"), \
            mock.patch.object(deploy, "_require_root_install_dir"), \
            mock.patch.object(deploy, "_owned_snapshot", side_effect=snapshot), \
            mock.patch.object(deploy, "_retired_unit_snapshot", return_value={}), \
            mock.patch.object(deploy, "_retire_stale_units", return_value=False), \
            mock.patch.object(deploy, "_nginx_snapshot", return_value={}), \
            mock.patch.object(deploy, "_write_if_changed", return_value=False), \
            mock.patch.object(deploy, "_converge_nginx", return_value=False), \
            mock.patch.object(deploy, "_restore_nginx"), \
            mock.patch.object(deploy, "_restore_unit_set"), \
            mock.patch.object(deploy, "_restore_snapshot"), \
            mock.patch.object(deploy, "_systemctl_is",
                              side_effect=lambda verb, unit: states[verb[3:]]), \
            mock.patch.object(deploy, "_systemctl", side_effect=systemctl), \
            mock.patch.object(audit, "flush_pending_firewall"):
        with th.assert_raises(deploy.DeployError):
            deploy.converge("off", "required")


@th.django_unit_test()
def test_off_flush_failure_restores_service_without_removing_assets(opts):
    from mojo.deploy import audit
    from mojo.deploy import mojosec as deploy

    states = {"enabled": True, "active": True}
    calls = []

    def systemctl(*args):
        calls.append(args)
        if args == ("stop", deploy.SERVICE):
            states["active"] = False
        elif args == ("start", deploy.SERVICE):
            states["active"] = True
        elif args == ("enable", deploy.SERVICE):
            states["enabled"] = True

    with mock.patch.object(deploy.os, "geteuid", return_value=0), \
            mock.patch.object(deploy, "_ensure_dir"), \
            mock.patch.object(deploy, "_require_root_install_dir"), \
            mock.patch.object(deploy, "_owned_snapshot", return_value=None), \
            mock.patch.object(deploy, "_retired_unit_snapshot", return_value={}), \
            mock.patch.object(deploy, "_nginx_snapshot", return_value={}), \
            mock.patch.object(deploy, "_restore_nginx"), \
            mock.patch.object(deploy, "_restore_unit_set",
                              side_effect=lambda *unused: systemctl("start", deploy.SERVICE)), \
            mock.patch.object(deploy, "_restore_snapshot"), \
            mock.patch.object(deploy, "_systemctl_is",
                              side_effect=lambda verb, unit: states[verb[3:]]), \
            mock.patch.object(deploy, "_systemctl", side_effect=systemctl), \
            mock.patch.object(deploy, "_remove_owned") as remove, \
            mock.patch.object(audit, "flush_pending_firewall",
                              side_effect=audit.AuditError("locked")):
        with th.assert_raises(deploy.DeployError):
            deploy.converge("off", "required")
    th.assert_true(states["active"],
                   "failed evidence flush must restore the prior active service")
    th.assert_true(not remove.called,
                   "off refusal must preserve provenance assets for a safe retry")


@th.django_unit_test()
def test_unenrolled_best_effort_observe_does_not_mutate_nginx_or_units(opts):
    from mojo.deploy import mojosec as deploy

    with mock.patch.object(deploy.os, "geteuid", return_value=0), \
            mock.patch.object(deploy, "_prepare_effective_config",
                              side_effect=deploy.DeployError("not enrolled")), \
            mock.patch.object(deploy, "_write_if_changed") as write, \
            mock.patch.object(deploy, "_converge_nginx") as nginx, \
            mock.patch.object(deploy, "_systemctl") as systemctl:
        result = deploy.converge("observe", "best_effort")

    th.assert_eq(result["ok"], False,
                 "an unenrolled best-effort node must return an explicit warning")
    th.assert_true(not write.called and not nginx.called and not systemctl.called,
                   "enrollment preflight must happen before every unit/nginx mutation")


@th.django_unit_test()
def test_failed_nginx_validation_restores_both_prior_fragments(opts):
    from mojo.deploy import mojosec as deploy

    log_path = "/etc/nginx/conf.d/00_mojosec.conf"
    snippet_path = "/etc/nginx/snippets/mojosec_receiver.conf"
    rotate_path = "/etc/logrotate.d/mojosec"
    django_path = "/etc/nginx/django.inc"
    prior = {log_path: (b"old log\n", 0o640), snippet_path: None,
             rotate_path: None, django_path: (b"# django\n", 0o644)}
    snapshot = {"files": prior, "log": None, "log_managed": True}
    writes = []
    removals = []
    lifecycle = []
    validations = {"count": 0}

    def validate(argv):
        validations["count"] += 1
        if validations["count"] == 1:
            raise deploy.DeployError("nginx rejected candidate")
        return ""

    with mock.patch.object(deploy, "_ensure_security_log"), \
            mock.patch.object(deploy, "_restore_security_log"), \
            mock.patch.object(deploy, "_write_if_changed", return_value=True), \
            mock.patch.object(deploy, "_atomic_write",
                              side_effect=lambda path, payload, mode: writes.append(
                                  (path, payload, mode))), \
            mock.patch.object(deploy.os, "unlink",
                              side_effect=lambda path: removals.append(path)), \
            mock.patch.object(deploy, "_run", side_effect=validate), \
            mock.patch.object(deploy, "_systemctl",
                              side_effect=lambda *args: lifecycle.append(args)):
        with th.assert_raises(deploy.DeployError):
            deploy._converge_nginx(
                True, "/var/log/nginx/mojosec.json.log", [],
                log_path, snippet_path, rotate_path, django_path, snapshot)

    th.assert_in((log_path, b"old log\n", 0o640), writes,
                 "validation failure must restore the prior fragment byte-for-byte and mode")
    th.assert_in(snippet_path, removals,
                 "a candidate created over an absent snippet must be removed on rollback")
    th.assert_eq(validations["count"], 2,
                 "rollback must revalidate the restored nginx graph")
    th.assert_in(("reload", "nginx"), lifecycle,
                 "rollback must reload the restored graph, not merely validate it")


@th.django_unit_test()
def test_late_converge_failure_restores_nginx_config_and_units(opts):
    from mojo.deploy import mojosec as deploy

    restored = []
    prepared = (
        {"sensor_id": "i-host", "config_provenance": {}}, b"{}\n",
        {"trusted_proxy_cidrs": [], "nginx_plane": "standard",
         "nginx_log_path": deploy.DEFAULT_LOG_PATH},
    )
    with mock.patch.object(deploy.os, "geteuid", return_value=0), \
            mock.patch.object(deploy, "_prepare_effective_config", return_value=prepared), \
            mock.patch.object(deploy, "_prepare_home_binds"), \
            mock.patch.object(deploy, "_lstat_regular"), \
            mock.patch.object(deploy, "_ensure_dir"), \
            mock.patch.object(deploy, "_require_root_install_dir"), \
            mock.patch.object(deploy, "_owned_snapshot", return_value=None), \
            mock.patch.object(deploy, "_retired_unit_snapshot", return_value={}), \
            mock.patch.object(deploy, "_retire_stale_units"), \
            mock.patch.object(deploy, "_nginx_snapshot", return_value={"prior": True}), \
            mock.patch.object(deploy, "_write_if_changed", return_value=True), \
            mock.patch.object(deploy, "_audit_config", return_value={}), \
            mock.patch.object(deploy, "_converge_nginx", return_value=True), \
            mock.patch.object(deploy, "_audit_active_nginx",
                              side_effect=deploy.DeployError("late active audit failed")), \
            mock.patch.object(deploy, "_systemctl_is", return_value=False), \
            mock.patch.object(deploy, "_restore_snapshot",
                              side_effect=lambda path, prior: restored.append(("file", path))), \
            mock.patch.object(deploy, "_restore_nginx",
                              side_effect=lambda prior, path: restored.append(("nginx", path))), \
            mock.patch.object(deploy, "_restore_unit_set",
                              side_effect=lambda *args: restored.append(("systemd", args[0]))), \
            _provenance_deploy_mocks(deploy), \
            mock.patch("mojo.deploy.audit.restore_immediate") as restore_audit:
        result = deploy.converge("observe", "required")

    th.assert_true(result["ok"] is False,
                   "observe convergence failure must degrade and report, never abort "
                   "the deploy — MojoSec is an observer (item 2007)")
    th.assert_in("late active audit failed", result["warning"],
                 "the degraded result must carry the actual failure for the operator")
    th.assert_in(("file", deploy.CONFIG_PATH), restored,
                 "late failure must restore the prior canonical root config")
    th.assert_in(("nginx", deploy.DEFAULT_LOG_PATH), restored,
                 "late failure must restore and reload the prior nginx graph")
    th.assert_in(("systemd", deploy.SERVICE_PATH), restored,
                 "late failure must restore prior unit files and lifecycle")
    restore_audit.assert_called_once_with()


@th.django_unit_test()
def test_audit_internal_rollback_is_not_rolled_back_again(opts):
    from mojo.deploy import audit
    from mojo.deploy import mojosec as deploy

    prepared = (
        {"sensor_id": "i-host", "config_provenance": {}}, b"{}\n",
        {"trusted_proxy_cidrs": [], "nginx_plane": "standard",
         "nginx_log_path": deploy.DEFAULT_LOG_PATH},
    )
    snapshot = {"path": "prior", "payload": b"prior", "mode": 0o600}
    with mock.patch.object(deploy.os, "geteuid", return_value=0), \
            mock.patch.object(deploy, "_prepare_effective_config", return_value=prepared), \
            mock.patch.object(deploy, "_prepare_home_binds"), \
            mock.patch.object(deploy, "_lstat_regular"), \
            mock.patch.object(deploy, "_ensure_dir"), \
            mock.patch.object(deploy, "_require_root_install_dir"), \
            mock.patch.object(deploy, "_owned_snapshot", return_value=snapshot), \
            mock.patch.object(deploy, "_retired_unit_snapshot", return_value={}), \
            mock.patch.object(deploy, "_retire_stale_units", return_value=False), \
            mock.patch.object(deploy, "_nginx_snapshot", return_value={}), \
            mock.patch.object(deploy, "_write_if_changed", return_value=False), \
            mock.patch.object(deploy, "_audit_config"), \
            mock.patch.object(deploy, "_restore_snapshot"), \
            mock.patch.object(deploy, "_restore_nginx"), \
            mock.patch.object(deploy, "_restore_unit_set"), \
            mock.patch.object(deploy, "_systemctl_is", return_value=False), \
            mock.patch.object(deploy, "_systemctl"), \
            mock.patch.object(deploy.pwd, "getpwnam",
                              return_value=mock.Mock(pw_uid=1000, pw_gid=1000)), \
            mock.patch.object(audit, "converge",
                              side_effect=audit.AuditError("internally restored")), \
            mock.patch.object(audit, "restore_immediate") as restore:
        result = deploy.converge("observe", "required")
    th.assert_true(result["ok"] is False,
                   "an Audit convergence failure must degrade and report, never abort "
                   "the deploy — MojoSec is an observer (item 2007)")
    th.assert_in("internally restored", result["warning"],
                 "the degraded result must carry the Audit failure for the operator")
    th.assert_true(not restore.called,
                   "outer rollback must not apply an old generation after Audit restored itself")


@th.django_unit_test()
def test_credential_rotation_accepts_secret_only_on_stdin(opts):
    from mojo.deploy import mojosec as deploy

    captured = {}
    stream = io.TextIOWrapper(io.BytesIO(b"secret-api-key\n"), encoding="utf-8")
    with mock.patch.object(deploy.os, "geteuid", return_value=0), \
            mock.patch.object(deploy, "_ensure_dir"), \
            mock.patch.object(deploy, "_owned_snapshot", return_value=None), \
            mock.patch.object(deploy, "_systemctl_is", return_value=False), \
            mock.patch.object(deploy, "_atomic_write",
                              side_effect=lambda path, payload, mode: captured.update(
                                  path=path, payload=payload, mode=mode)):
        deploy.rotate_credential(stream)
    th.assert_eq(captured["path"], deploy.CREDENTIAL_PATH,
                 "rotation must target only the root credential path")
    th.assert_eq(captured["mode"], 0o600, "rotated credentials must be mode 0600")
    th.assert_eq(captured["payload"], b"secret-api-key\n",
                 "stdin token should be atomically normalized with one newline")


@th.django_unit_test()
def test_enrollment_installs_protected_host_identity_from_stdin(opts):
    from mojo.deploy import mojosec as deploy

    enrollment = {
        "version": 1, "sensor_id": "i-012345.us-west-2",
        "endpoint": "https://incident.example/api/incident/mojosec/batch",
        "nginx_plane": "standard", "trusted_proxy_cidrs": ["10.0.0.0/8"],
        "fim_allowed_roots": ["/etc"],
    }
    captured = {}
    stream = io.TextIOWrapper(
        io.BytesIO(json.dumps(enrollment).encode()), encoding="utf-8")
    with mock.patch.object(deploy.os, "geteuid", return_value=0), \
            mock.patch.object(deploy, "_ensure_dir"), \
            mock.patch.object(deploy, "_write_if_changed",
                              side_effect=lambda path, text, mode: captured.update(
                                  path=path, text=text, mode=mode)):
        result = deploy.install_enrollment(stream)

    th.assert_eq(captured["path"], deploy.ENROLLMENT_PATH,
                 "host identity must land only in the root enrollment file")
    th.assert_eq(captured["mode"], 0o600, "enrollment must be root-only 0600")
    th.assert_eq(result["sensor_id"], enrollment["sensor_id"],
                 "installed identity is an infrastructure host, not a tenant")
    th.assert_true("credential" not in captured["text"].lower(),
                   "enrollment JSON must never carry the bearer secret")


@th.django_unit_test()
def test_edge_enrollment_accepts_app_owned_security_log_directory(opts):
    from mojo.deploy import mojosec as deploy

    enrollment = {
        "version": 1,
        "sensor_id": "prod-web-i-0123456789abcdef0",
        "endpoint": "https://incident.example/api/incident/mojosec/batch",
        "nginx_plane": "edge",
        "edge_log_dir": deploy.EDGE_LOG_DIR,
        "trusted_proxy_cidrs": [],
    }
    stream = io.TextIOWrapper(
        io.BytesIO(json.dumps(enrollment).encode()), encoding="utf-8")
    with mock.patch.object(deploy.os, "geteuid", return_value=0), \
            mock.patch.object(deploy, "_ensure_dir"), \
            mock.patch.object(deploy, "_write_if_changed"):
        result = deploy.install_enrollment(stream)

    th.assert_eq(result["edge_log_dir"], deploy.EDGE_LOG_DIR,
                 "Edge enrollment must accept its documented app-owned log root")
    th.assert_eq(result["nginx_log_path"],
                 deploy.DEFAULT_LOG_PATH,
                 "Edge raw evidence must use the root-owned nginx master-opened path")


@th.django_unit_test()
def test_enrolled_lifecycle_persists_observe_across_ordinary_deploys(opts):
    from mojo.deploy import mojosec as deploy

    with mock.patch.object(deploy.os, "lstat", side_effect=FileNotFoundError):
        th.assert_eq(deploy.resolve_lifecycle("enrolled", "enrolled"),
                     ("off", "best_effort"),
                     "an unenrolled legacy node must remain off across upgrades")
    with mock.patch.object(deploy.os, "lstat"), \
            mock.patch.object(deploy, "_load_enrollment", return_value={
                "mode": "observe", "criticality": "required"}):
        th.assert_eq(deploy.resolve_lifecycle("enrolled", "enrolled"),
                     ("observe", "required"),
                     "post_deploy must resolve the protected persistent lifecycle")


@th.django_unit_test()
def test_failed_live_credential_rotation_restores_old_secret(opts):
    from mojo.deploy import mojosec as deploy

    prior = (b"old-api-key\n", 0o600)
    writes = []
    restarts = {"count": 0}

    def systemctl(*args):
        restarts["count"] += 1
        if restarts["count"] == 1:
            raise deploy.DeployError("new service failed")

    with mock.patch.object(deploy.os, "geteuid", return_value=0), \
            mock.patch.object(deploy, "_ensure_dir"), \
            mock.patch.object(deploy, "_owned_snapshot", return_value=prior), \
            mock.patch.object(deploy, "_systemctl_is", return_value=True), \
            mock.patch.object(deploy, "_systemctl", side_effect=systemctl), \
            mock.patch.object(deploy, "_atomic_write",
                              side_effect=lambda path, payload, mode: writes.append(
                                  (path, payload, mode))):
        with th.assert_raises(deploy.DeployError):
            deploy.rotate_credential(
                io.TextIOWrapper(io.BytesIO(b"new-api-key\n"), encoding="utf-8"))

    th.assert_eq(writes[-1], (deploy.CREDENTIAL_PATH, prior[0], prior[1]),
                 "failed restart must atomically restore the exact prior secret")
    th.assert_eq(restarts["count"], 2,
                 "rotation rollback must restart once more with the old credential")
