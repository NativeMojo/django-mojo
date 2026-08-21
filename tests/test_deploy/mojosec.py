"""Focused contracts for MojoSec root deployment and nginx rendering.

The pure rendering, policy-boundary, unit-text, and filesystem contracts live
here. The converge/rotation/enrollment orchestration tests, which mock
`mojo.deploy.mojosec` internals process-globally, moved to
tests/test_deploy_extended_serial/mojosec.py (maestro #2558).
"""

import json
import os
import subprocess
import sys
import tempfile

from testit import helpers as th


@th.django_unit_test()
def test_nginx_security_log_is_rich_bounded_json(opts):
    from mojo.deploy.mojosec_nginx import render_http_log
    from mojo.apps.edge.services import render as edge_render

    text = render_http_log(
        "/var/log/nginx/mojosec.json.log",
        ["10.0.0.0/8", "2001:db8::/32"],
    )
    th.assert_in("log_format mojosec_v1 escape=json", text,
                 "control characters must be JSON escaped by nginx")
    for required in (
            '"request_uri":"$request_uri"', '"referrer":"$http_referer"',
            '"user_agent":"$http_user_agent"', '"host":"$host"',
            '"upstream_status":"$upstream_status"',
            '"upstream_response_time":"$upstream_response_time"'):
        th.assert_in(required, text,
                     f"the protected security stream omitted approved evidence {required}")
    for required in (
            '"request_id":"$request_id"', '"scheme":"$scheme"',
            '"protocol":"$server_protocol"', '"tls_protocol":"$ssl_protocol"',
            '"tls_cipher":"$ssl_cipher"', '"remote_port":"$remote_port"',
            '"peer_port":"$realip_remote_port"', '"server_port":"$server_port"',
            '"request_length":"$request_length"', '"response_bytes":"$bytes_sent"',
            '"response_body_bytes":"$body_bytes_sent"',
            '"upstream_connect_time":"$upstream_connect_time"',
            '"upstream_header_time":"$upstream_header_time"',
            '"upstream_response_length":"$upstream_response_length"',
            '"upstream_bytes_received":"$upstream_bytes_received"',
            '"upstream_bytes_sent":"$upstream_bytes_sent"',
            '"response_class":"$mojosec_response_class"',
            '"resource_id":"$mojosec_resource_id"',
            '"edge_policy_version":"$mojosec_policy_version"'):
        th.assert_in(required, text,
                     f"the protected security stream omitted approved rich field {required}")
    for forbidden in ("http_cookie", "http_authorization", "request_body"):
        th.assert_true(forbidden not in text,
                       f"the security stream leaked forbidden field {forbidden}")
    th.assert_in('"remote_addr":"$remote_addr"', text,
                 "the resolved client must be explicit")
    th.assert_in('"peer_addr":"$realip_remote_addr"', text,
                 "the direct peer must remain available after realip resolution")
    th.assert_in("set_real_ip_from 10.0.0.0/8;", text,
                 "only an exact configured proxy network may affect client identity")
    th.assert_true(edge_render.render_mojosec_http_log is render_http_log,
                   "standard and Edge nginx must use one shared evidence renderer")


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
            {"path": "/etc/nginx", "recursive": True, "exclude": []},
        ]}},
    }
    enrollment = {
        "version": 1, "sensor_id": "i-012345.us-west-2",
        "endpoint": "https://incident.example/api/incident/mojosec/batch",
        "fim_allowed_roots": ["/etc"], "trusted_proxy_cidrs": ["10.0.0.0/8"],
    }

    def read(path, *args, **kwargs):
        if path == deploy.DESIRED_CONFIG_PATH:
            return desired, json.dumps(desired).encode()
        return enrollment, json.dumps(enrollment).encode()

    config, _, protected = deploy._prepare_effective_config(loader=read)
    th.assert_eq(config["sensor_id"], enrollment["sensor_id"],
                 "canonical identity must come only from root enrollment")
    th.assert_eq(config["collectors"]["nginx"]["paths"], [deploy.DEFAULT_LOG_PATH],
                 "app policy must be pinned to the dedicated security log")
    th.assert_eq(protected["trusted_proxy_cidrs"], ["10.0.0.0/8"],
                 "trusted proxy identity boundary belongs to enrollment")
    th.assert_true("local_only_diagnostic" not in config,
                   "the root diagnostic sidecar must never enter effective configuration")
    th.assert_true("local_only_diagnostic" not in deploy.DESIRED_KEYS,
                   "fleet desired policy must not grow a diagnostic override knob")

    for key, value in (
            ("endpoint", "https://evil.example/api/incident/mojosec/batch"),
            ("sensor_id", "other-host"), ("credential_path", "/tmp/key")):
        poisoned = dict(desired, **{key: value})

        def poisoned_read(path, *args, poisoned=poisoned, **kwargs):
            if path == deploy.DESIRED_CONFIG_PATH:
                return poisoned, json.dumps(poisoned).encode()
            return enrollment, json.dumps(enrollment).encode()

        with th.assert_raises(deploy.DeployError):
            deploy._prepare_effective_config(loader=poisoned_read)


@th.django_unit_test()
def test_unit_is_privileged_isolated_and_never_bans(opts):
    from mojo.deploy import mojosec as deploy

    unit = deploy.UNIT_TEXT
    for expected in (
            "User=root", "WorkingDirectory=/", "python3 -E -P -m mojo.mojosec",
            "Environment=PYTHONHOME=", "Environment=PYTHONPATH=",
            "StateDirectoryMode=0700", "RuntimeDirectoryMode=0755",
            "NoNewPrivileges=true", "ProtectKernelModules=true",
            "ProtectSystem=strict", "ProtectHome=tmpfs", "BindReadOnlyPaths=",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "ConditionPathExists=/etc/mojosec/config.json"):
        th.assert_in(expected, unit, f"service is missing deployment contract: {expected}")
    for forbidden in ("fail2ban", "iptables", "nft", "firewall", "/opt/api/var"):
        th.assert_true(forbidden not in unit.lower(),
                       f"root service must not execute {forbidden!r}")
    th.assert_true(all(path in unit for path in deploy.HOME_BIND_PATHS),
                   "every exact root/ec2-user persistence path must be a non-optional bind")
    th.assert_true(" -I " not in unit and " -s " not in unit,
                   "AL2023 root-pip packages disappear under -I/-s")
    rotation = deploy.LOGROTATE_TEXT
    for expected in ("daily", "maxsize 50M", "rotate 14", "copytruncate",
                     "su root root", "create 0600 root root"):
        th.assert_in(expected, rotation,
                     f"nginx security-log rotation is missing {expected!r}")
    for forbidden in ("postrotate", "USR1"):
        th.assert_true(forbidden not in rotation,
                       f"rotation must preserve the root-owned active inode: {forbidden}")


@th.django_unit_test()
def test_home_binds_create_aws_parent_and_reject_unsafe_parents(opts):
    from mojo.deploy import mojosec as deploy

    with tempfile.TemporaryDirectory() as root:
        home = os.path.join(root, "home")
        os.mkdir(home, 0o700)
        owner = (os.getuid(), os.getgid())
        paths = (
            os.path.join(home, ".aws", "config"),
            os.path.join(home, ".aws", "credentials"),
            os.path.join(home, ".ssh"),
        )
        deploy._prepare_home_binds(paths=paths, users={home: owner})
        th.assert_true(os.path.isdir(os.path.join(home, ".aws")),
                       "a clean home must get its required .aws parent")
        th.assert_true(all(os.path.isfile(path) for path in paths[:2]),
                       "exact AWS leaf binds must exist before service activation")

    with tempfile.TemporaryDirectory() as root:
        home = os.path.join(root, "home")
        outside = os.path.join(root, "outside")
        os.mkdir(home, 0o700)
        os.mkdir(outside, 0o700)
        os.symlink(outside, os.path.join(home, ".aws"))
        with th.assert_raises(deploy.DeployError):
            deploy._prepare_home_binds(
                paths=(os.path.join(home, ".aws", "credentials"),),
                users={home: (os.getuid(), os.getgid())})
        th.assert_true(not os.path.exists(os.path.join(outside, "credentials")),
                       "a parent symlink must be rejected before leaf creation")

    with tempfile.TemporaryDirectory() as root:
        home = os.path.join(root, "home")
        os.mkdir(home, 0o700)
        os.mkdir(os.path.join(home, ".aws"), 0o777)
        os.chmod(os.path.join(home, ".aws"), 0o777)
        with th.assert_raises(deploy.DeployError):
            deploy._prepare_home_binds(
                paths=(os.path.join(home, ".aws", "config"),),
                users={home: (os.getuid(), os.getgid())})


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
def test_downgrade_handoff_quiesces_writer_through_flush_and_restore(opts):
    from mojo.deploy import mojosec as deploy

    path = os.path.join(os.path.dirname(deploy.__file__), "scripts", "post_deploy.sh")
    with open(path, encoding="utf-8") as handle:
        script = handle.read()
    function = script.index("quiesce_mojosec_for_downgrade()")
    stop = script.index("systemctl stop mojosec.service", function)
    inactive = script.index("! systemctl is-active --quiet mojosec.service", stop)
    handoff = script.index("quiesce_mojosec_for_downgrade", function + 1)
    flush = script.index('"$MOJOSEC_AUDIT_HELPER" flush-pending', handoff)
    restore = script.index('"$MOJOSEC_AUDIT_HELPER" restore', flush)
    th.assert_true(stop < inactive < handoff < flush < restore,
                   "downgrade must stop and verify the writer before flush and handoff")
    between = script[flush:restore]
    th.assert_true("systemctl start mojosec.service" not in between,
                   "no writer may restart between the pending flush and old-module handoff")
    th.assert_in("restore_mojosec_service_after_failure", script[handoff:restore],
                 "a failed safe handoff must restore the prior service lifecycle")
