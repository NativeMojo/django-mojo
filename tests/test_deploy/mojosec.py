"""Focused contracts for MojoSec root deployment and nginx rendering."""

import io
import json
import os
import subprocess
import sys
import tempfile
from unittest import mock

from testit import helpers as th


@th.django_unit_test()
def test_nginx_security_log_is_queryless_bounded_json(opts):
    from mojo.deploy.mojosec_nginx import render_http_log

    text = render_http_log(
        "/var/log/nginx/mojosec.json.log",
        ["10.0.0.0/8", "2001:db8::/32"],
    )
    th.assert_in("log_format mojosec_v1 escape=json", text,
                 "control characters must be JSON escaped by nginx")
    th.assert_in('"uri":"$uri"', text,
                 "$uri must be logged deliberately without a query string")
    th.assert_true("$request_uri" not in text and "$args" not in text,
                   "the security stream must never include query data")
    for forbidden in ("http_referer", "http_cookie", "http_authorization", "request_body"):
        th.assert_true(forbidden not in text,
                       f"the security stream leaked forbidden field {forbidden}")
    th.assert_in('"remote_addr":"$remote_addr"', text,
                 "the resolved client must be explicit")
    th.assert_in('"peer_addr":"$realip_remote_addr"', text,
                 "the direct peer must remain available after realip resolution")
    th.assert_in("set_real_ip_from 10.0.0.0/8;", text,
                 "only an exact configured proxy network may affect client identity")


@th.django_unit_test()
def test_nginx_rejects_ambiguous_proxy_ranges_and_paths(opts):
    from mojo.deploy.mojosec_nginx import (
        NginxConfigError, render_http_log, render_receiver_location,
    )

    for cidr in ("10.0.0.1/8", "0.0.0.0/0; include /tmp/x"):
        with th.assert_raises(NginxConfigError):
            render_http_log(proxy_cidrs=[cidr])
    with th.assert_raises(NginxConfigError):
        render_http_log("/var/log/nginx/x;error_log")
    receiver = render_receiver_location()
    th.assert_in("include /etc/nginx/asgi.inc;", receiver,
                 "the exact receiver must use the location-safe proxy include")
    th.assert_in("proxy_pass http://asgi_upstream;", receiver,
                 "the standard receiver must target the deployed ASGI upstream")
    th.assert_in("location = /api/incident/mojosec/batch/ {", receiver,
                 "the trailing-slash alias must receive the same wire cap")
    th.assert_true("django.inc" not in receiver,
                   "django.inc declares locations and cannot be nested here")


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
def test_standard_receiver_is_automatically_wired_and_off_removes_only_marker(opts):
    from mojo.deploy import mojosec as deploy

    source = b"location / { proxy_pass http://asgi_upstream; }\n"
    observed = deploy._render_django_include(source, True)
    th.assert_in(deploy.DJANGO_RECEIVER_INCLUDE, observed.decode(),
                 "observe must wire the generated exact receiver into django.inc")
    th.assert_eq(observed.count(deploy.DJANGO_RECEIVER_INCLUDE.encode()), 1,
                 "repeated convergence must never duplicate the exact route")
    repeated = deploy._render_django_include(observed, True)
    th.assert_eq(repeated, observed, "receiver wiring must be byte-idempotent")
    th.assert_eq(deploy._render_django_include(observed, False), source,
                 "off removes only the package-owned marker and keeps the vhost graph")


@th.django_unit_test()
def test_desired_policy_cannot_override_root_enrollment_or_log_boundary(opts):
    from mojo.deploy import mojosec as deploy

    desired = {
        "version": 1, "policy_revision": "fleet-r7",
        "collectors": {"fim": {"enabled": True, "targets": [
            {"path": "/opt/api/app", "recursive": True, "exclude": []},
        ]}},
    }
    enrollment = {
        "version": 1, "sensor_id": "i-012345.us-west-2",
        "endpoint": "https://incident.example/api/incident/mojosec/batch",
        "fim_allowed_roots": ["/opt/api"], "trusted_proxy_cidrs": ["10.0.0.0/8"],
    }

    def read(path, *args, **kwargs):
        if path == deploy.DESIRED_CONFIG_PATH:
            return desired, json.dumps(desired).encode()
        return enrollment, json.dumps(enrollment).encode()

    with mock.patch.object(deploy, "_read_json_file", side_effect=read):
        config, _, protected = deploy._prepare_effective_config()
    th.assert_eq(config["sensor_id"], enrollment["sensor_id"],
                 "canonical identity must come only from root enrollment")
    th.assert_eq(config["collectors"]["nginx"]["paths"], [deploy.DEFAULT_LOG_PATH],
                 "app policy must be pinned to the dedicated security log")
    th.assert_eq(protected["trusted_proxy_cidrs"], ["10.0.0.0/8"],
                 "trusted proxy identity boundary belongs to enrollment")

    for key, value in (
            ("endpoint", "https://evil.example/api/incident/mojosec/batch"),
            ("sensor_id", "other-host"), ("credential_path", "/tmp/key")):
        poisoned = dict(desired, **{key: value})
        with mock.patch.object(
                deploy, "_read_json_file",
                side_effect=lambda path, *a, **k: (
                    (poisoned, json.dumps(poisoned).encode())
                    if path == deploy.DESIRED_CONFIG_PATH else
                    (enrollment, json.dumps(enrollment).encode()))):
            with th.assert_raises(deploy.DeployError):
                deploy._prepare_effective_config()


@th.django_unit_test()
def test_unit_is_privileged_isolated_and_never_bans(opts):
    from mojo.deploy import mojosec as deploy

    unit = deploy.UNIT_TEXT
    for expected in (
            "User=root", "WorkingDirectory=/", "python3 -E -P -m mojo.mojosec",
            "Environment=PYTHONHOME=", "Environment=PYTHONPATH=",
            "StateDirectoryMode=0700", "RuntimeDirectoryMode=0755",
            "NoNewPrivileges=true", "ProtectKernelModules=true",
            "ProtectSystem=strict",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "ConditionPathExists=/etc/mojosec/config.json"):
        th.assert_in(expected, unit, f"service is missing deployment contract: {expected}")
    for forbidden in ("fail2ban", "iptables", "nft", "firewall", "/opt/api/var"):
        th.assert_true(forbidden not in unit.lower(),
                       f"root service must not execute {forbidden!r}")
    th.assert_true(" -I " not in unit and " -s " not in unit,
                   "AL2023 root-pip packages disappear under -I/-s")
    rotation = deploy.LOGROTATE_TEXT
    for expected in ("daily", "maxsize 50M", "rotate 14", "create 0640 root root",
                     "systemctl kill -s USR1 nginx.service"):
        th.assert_in(expected, rotation,
                     f"nginx security-log rotation is missing {expected!r}")


@th.django_unit_test()
def test_safe_path_launcher_retains_system_site_without_env_or_cwd(opts):
    """Model the AL2023 root-pip layout that `-I`/`-s` incorrectly hide."""
    script = (
        "import json,site,sys;"
        "print(json.dumps({'path':sys.path,'site':site.getsitepackages(),"
        "'safe':sys.flags.safe_path,'env':sys.flags.ignore_environment}))"
    )
    with tempfile.TemporaryDirectory() as attacker:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = attacker
        done = subprocess.run(
            [sys.executable, "-E", "-P", "-c", script], cwd=attacker,
            env=environment, capture_output=True, text=True, timeout=10)
    th.assert_eq(done.returncode, 0, f"secure Python probe failed: {done.stderr}")
    observed = json.loads(done.stdout)
    th.assert_true(observed["safe"] and observed["env"],
                   "-E -P must activate environment-ignore and safe-path flags")
    th.assert_true(attacker not in observed["path"] and "" not in observed["path"],
                   "attacker PYTHONPATH/current directory survived secure launch")
    th.assert_true(any(path in observed["path"] for path in observed["site"]),
                   "safe-path launch must retain an installed system site")


@th.django_unit_test()
def test_python_310_observe_fails_before_any_privileged_mutation(opts):
    from mojo.deploy import mojosec as deploy

    with mock.patch.object(deploy.sys, "version_info", (3, 10, 14)), \
            mock.patch.object(deploy, "_ensure_dir") as ensure_dir, \
            mock.patch.object(deploy, "_prepare_effective_config") as prepare:
        with th.assert_raises(deploy.DeployError):
            deploy.converge("observe", "required")
    th.assert_true(not ensure_dir.called and not prepare.called,
                   "unsupported observe must fail before config or filesystem mutation")


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
            mock.patch.object(deploy, "_require_root_install_dir"), \
            mock.patch.object(deploy, "_retire_stale_units"), \
            mock.patch.object(deploy, "_retired_unit_snapshot", return_value={}), \
            mock.patch.object(deploy, "_owned_snapshot", return_value=None), \
            mock.patch.object(deploy, "_nginx_snapshot", return_value={}), \
            mock.patch.object(deploy, "_prepare_effective_config", return_value=(
                {"sensor_id": "i-host", "config_provenance": {}}, b"{}\n",
                {"trusted_proxy_cidrs": [], "nginx_plane": "standard",
                 "nginx_log_path": deploy.DEFAULT_LOG_PATH})), \
            mock.patch.object(deploy, "_write_if_changed", side_effect=[True, True, True]), \
            mock.patch.object(deploy, "_converge_nginx", return_value=False), \
            mock.patch.object(deploy, "_audit_active_nginx"), \
            mock.patch.object(deploy, "_audit_config", return_value={}), \
            mock.patch.object(deploy, "_lstat_regular"), \
            mock.patch.object(deploy, "_systemctl_is",
                              side_effect=lambda verb, unit: states[verb[3:]]), \
            mock.patch.object(deploy, "_systemctl", side_effect=systemctl), \
            mock.patch.object(deploy.os, "geteuid", return_value=0):
        result = deploy.converge("observe", "required")

    th.assert_eq(result["mode"], "observe", "observe convergence must report its mode")
    th.assert_true(all(
        not call or call[-1] in ("daemon-reload", "nginx", deploy.SERVICE)
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
        if args[:2] == ("disable", "--now"):
            states.update(enabled=False, active=False)

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
            mock.patch.object(deploy.os, "geteuid", return_value=0):
        result = deploy.converge("off", "required")
    th.assert_in(("disable", "--now", deploy.SERVICE), calls,
                 "off must stop and disable a prior installation")
    th.assert_eq(result["spool_preserved"], True,
                 "off/rollback must preserve local evidence")

    with mock.patch.object(deploy, "_prepare_effective_config",
                           side_effect=OSError("denied")), \
            mock.patch.object(deploy.os, "geteuid", return_value=0):
        warning = deploy.converge("observe", "best_effort")
    th.assert_eq(warning["ok"], False,
                 "best_effort must return an explicit warning rather than claim success")


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
                              side_effect=lambda *args: restored.append(("systemd", args[0]))):
        with th.assert_raises(deploy.DeployError):
            deploy.converge("observe", "required")

    th.assert_in(("file", deploy.CONFIG_PATH), restored,
                 "late failure must restore the prior canonical root config")
    th.assert_in(("nginx", deploy.DEFAULT_LOG_PATH), restored,
                 "late failure must restore and reload the prior nginx graph")
    th.assert_in(("systemd", deploy.SERVICE_PATH), restored,
                 "late failure must restore prior unit files and lifecycle")


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
        "fim_allowed_roots": ["/opt/api"],
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
