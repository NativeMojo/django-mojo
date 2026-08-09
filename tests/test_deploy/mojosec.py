"""Focused contracts for MojoSec root deployment and nginx rendering."""

import io
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
    from mojo.deploy.mojosec_nginx import NginxConfigError, render_http_log

    for cidr in ("10.0.0.1/8", "0.0.0.0/0; include /tmp/x"):
        with th.assert_raises(NginxConfigError):
            render_http_log(proxy_cidrs=[cidr])
    with th.assert_raises(NginxConfigError):
        render_http_log("/var/log/nginx/x;error_log")


@th.django_unit_test()
def test_unit_is_privileged_isolated_and_never_bans(opts):
    from mojo.deploy import mojosec as deploy

    unit = deploy.UNIT_TEXT
    for expected in (
            "User=root", "WorkingDirectory=/", "python3 -I -m mojo.mojosec",
            "StateDirectoryMode=0700", "RuntimeDirectoryMode=0755",
            "NoNewPrivileges=true", "ProtectKernelModules=true"):
        th.assert_in(expected, unit, f"service is missing deployment contract: {expected}")
    for forbidden in ("fail2ban", "iptables", "nft", "firewall", "/opt/api/var/deploy"):
        th.assert_true(forbidden not in unit.lower(),
                       f"root service must not execute {forbidden!r}")
    rotation = deploy.LOGROTATE_TEXT
    for expected in ("daily", "rotate 14", "create 0640 root root",
                     "systemctl kill -s USR1 nginx.service"):
        th.assert_in(expected, rotation,
                     f"nginx security-log rotation is missing {expected!r}")


@th.django_unit_test()
def test_converge_lifecycle_is_an_exact_allowlist(opts):
    from mojo.deploy import mojosec as deploy

    calls = []
    with mock.patch.object(deploy, "_ensure_dir"), \
            mock.patch.object(deploy, "_require_root_install_dir"), \
            mock.patch.object(deploy, "_retire_stale_units"), \
            mock.patch.object(deploy, "_write_if_changed", side_effect=[True, True]), \
            mock.patch.object(deploy, "_converge_nginx", return_value=False), \
            mock.patch.object(deploy, "_audit_config", return_value={}), \
            mock.patch.object(deploy, "_lstat_regular"), \
            mock.patch.object(deploy, "_systemctl_is", return_value=False), \
            mock.patch.object(deploy, "_systemctl", side_effect=lambda *a: calls.append(a)), \
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
    with mock.patch.object(deploy, "_ensure_dir"), \
            mock.patch.object(deploy, "_require_root_install_dir"), \
            mock.patch.object(deploy, "_retire_stale_units"), \
            mock.patch.object(deploy, "_write_if_changed", side_effect=[False, True]), \
            mock.patch.object(deploy, "_converge_nginx", return_value=True), \
            mock.patch.object(deploy, "_systemctl_is", return_value=False), \
            mock.patch.object(deploy, "_systemctl", side_effect=lambda *a: calls.append(a)), \
            mock.patch.object(deploy.os, "geteuid", return_value=0):
        result = deploy.converge("off", "required")
    th.assert_in(("disable", "--now", deploy.SERVICE), calls,
                 "off must stop and disable a prior installation")
    th.assert_eq(result["spool_preserved"], True,
                 "off/rollback must preserve local evidence")

    with mock.patch.object(deploy, "_ensure_dir", side_effect=OSError("denied")), \
            mock.patch.object(deploy.os, "geteuid", return_value=0):
        warning = deploy.converge("observe", "best_effort")
    th.assert_eq(warning["ok"], False,
                 "best_effort must return an explicit warning rather than claim success")


@th.django_unit_test()
def test_unenrolled_best_effort_observe_does_not_mutate_nginx_or_units(opts):
    from mojo.deploy import mojosec as deploy

    with mock.patch.object(deploy.os, "geteuid", return_value=0), \
            mock.patch.object(deploy, "_audit_config",
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
    prior = {log_path: (b"old log\n", 0o640), snippet_path: None,
             rotate_path: None}
    writes = []
    removals = []
    validations = {"count": 0}

    def validate(argv):
        validations["count"] += 1
        if validations["count"] == 1:
            raise deploy.DeployError("nginx rejected candidate")
        return ""

    with mock.patch.object(deploy, "_owned_snapshot",
                           side_effect=lambda path: prior[path]), \
            mock.patch.object(deploy, "_write_if_changed", return_value=True), \
            mock.patch.object(deploy, "_atomic_write",
                              side_effect=lambda path, payload, mode: writes.append(
                                  (path, payload, mode))), \
            mock.patch.object(deploy.os, "unlink",
                              side_effect=lambda path: removals.append(path)), \
            mock.patch.object(deploy, "_run", side_effect=validate), \
            mock.patch.object(deploy, "_systemctl"):
        with th.assert_raises(deploy.DeployError):
            deploy._converge_nginx(
                True, "/var/log/nginx/mojosec.json.log", [],
                log_path, snippet_path, rotate_path)

    th.assert_in((log_path, b"old log\n", 0o640), writes,
                 "validation failure must restore the prior fragment byte-for-byte and mode")
    th.assert_in(snippet_path, removals,
                 "a candidate created over an absent snippet must be removed on rollback")
    th.assert_eq(validations["count"], 2,
                 "rollback must revalidate the restored nginx graph")


@th.django_unit_test()
def test_credential_rotation_accepts_secret_only_on_stdin(opts):
    from mojo.deploy import mojosec as deploy

    captured = {}
    stream = io.TextIOWrapper(io.BytesIO(b"secret-api-key\n"), encoding="utf-8")
    with mock.patch.object(deploy.os, "geteuid", return_value=0), \
            mock.patch.object(deploy, "_atomic_write",
                              side_effect=lambda path, payload, mode: captured.update(
                                  path=path, payload=payload, mode=mode)), \
            mock.patch.object(deploy, "_run", side_effect=deploy.DeployError("inactive")):
        deploy.rotate_credential(stream)
    th.assert_eq(captured["path"], deploy.CREDENTIAL_PATH,
                 "rotation must target only the root credential path")
    th.assert_eq(captured["mode"], 0o600, "rotated credentials must be mode 0600")
    th.assert_eq(captured["payload"], b"secret-api-key\n",
                 "stdin token should be atomically normalized with one newline")
