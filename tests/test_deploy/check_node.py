"""mojo.deploy.check_node — the node audit, pure-python.

The section functions all take an injected run(cmd) -> (rc, out, err), so the
audit logic is testable with a scripted fake runner and no node: grading of
the compare states, the shims matrix (shim/fork/absent x --require-shims),
collision reporting, probe-url plumbing, the node_retired.conf audit, and the
var/deploy-missing INFO path. The template-freshness diff reads the real
filesystem, so it gets a real rendered tmp tree instead.
"""

import os
import datetime
import json
import shutil
import subprocess
import sys
import tempfile

from testit import helpers as th

PROJ = "/opt/api"


class FakeRunner:
    """run() double: first matching (substring, response) rule wins; anything
    unmatched succeeds silently. Every issued command is recorded, so tests
    can assert what the audit probed as well as what it concluded."""

    def __init__(self, rules=None):
        self.rules = list(rules or [])
        self.commands = []

    def __call__(self, cmd, timeout=30):
        self.commands.append(cmd)
        for needle, response in self.rules:
            if needle in cmd:
                return response
        return (0, "", "")


def _findings(report, section):
    return [f for f in report.findings if f["section"] == section]


def _statuses(report, section):
    return {f["name"]: f["status"] for f in _findings(report, section)}


def _find(report, section, fragment):
    for f in _findings(report, section):
        if fragment in f["name"]:
            return f
    return None


def _mojosec_systemd_show(dropins=""):
    values = {
        "User": "root", "Group": "root", "UMask": "0077",
        "WorkingDirectory": "/",
        "Environment": ("PYTHONUNBUFFERED=1 PYTHONHOME= PYTHONPATH= "
                        "PYTHONUSERBASE= PYTHONSTARTUP= PYTHONINSPECT="),
        "FragmentPath": "/etc/systemd/system/mojosec.service",
        "DropInPaths": dropins, "NoNewPrivileges": "yes", "PrivateTmp": "yes",
        "ProtectHome": "yes", "ProtectSystem": "strict",
        "ProtectKernelTunables": "yes", "ProtectKernelModules": "yes",
        "ProtectKernelLogs": "yes", "ProtectControlGroups": "yes",
        "RestrictSUIDSGID": "yes", "LockPersonality": "yes",
        "RestrictRealtime": "yes", "RestrictNamespaces": "yes",
        "RestrictAddressFamilies": "AF_UNIX AF_INET AF_INET6",
        "CapabilityBoundingSet": "cap_dac_read_search",
        "AmbientCapabilities": "",
        "ReadWritePaths": "/var/lib/mojosec /run/mojosec",
    }
    return "\n".join(f"{key}={value}" for key, value in values.items())


# ---------------------------------------------------------------------------
# compare-state grading
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_compare_set_grades_all_four_states(opts):
    from mojo.deploy import check_node as cn

    root = tempfile.mkdtemp(prefix="testit_check_node.")
    try:
        src = os.path.join(root, "contract")
        dst = os.path.join(root, "etc")
        os.makedirs(src)
        os.makedirs(dst)
        for name, in_src, in_dst in (("same.txt", "A", "A"),
                                     ("drift.txt", "A", "B"),
                                     ("missing.txt", "A", None)):
            with open(os.path.join(src, name), "w") as handle:
                handle.write(in_src)
            if in_dst is not None:
                with open(os.path.join(dst, name), "w") as handle:
                    handle.write(in_dst)

        run = cn.build_runner(None)
        report = cn.Report()
        cn.compare_set(report, "cron", run, src, dst,
                       ["same.txt", "drift.txt", "missing.txt", "ghost.txt"],
                       "run post_deploy")

        statuses = _statuses(report, "cron")
        th.assert_eq(statuses.get("same.txt"), cn.PASS,
                     f"byte-identical must PASS: {statuses}")
        th.assert_eq(statuses.get("drift.txt drifted"), cn.FAIL,
                     f"a drifted install must FAIL: {statuses}")
        th.assert_eq(statuses.get("missing.txt not installed"), cn.FAIL,
                     f"a never-installed contract file must FAIL: {statuses}")
        th.assert_eq(statuses.get("ghost.txt: contract file missing"), cn.INFO,
                     f"a vanished contract file is INFO, not a node defect: "
                     f"{statuses}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# var/deploy-missing INFO path
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_absent_rendered_contract_is_info_never_fail(opts):
    from mojo.deploy import check_node as cn

    run = FakeRunner([
        (f"ls -1 {PROJ}/var/deploy/cron.d", (1, "", "")),
        (f"ls -1 {PROJ}/var/deploy/systemd", (1, "", "")),
        ("systemctl is-active", (0, "active", "")),
        ("systemctl", (0, "enabled", "")),
    ])
    report = cn.Report()
    cn.check_cron(report, run, PROJ)
    cn.check_systemd(report, run, PROJ)

    for section in ("cron", "systemd"):
        rows = _findings(report, section)
        th.assert_true(all(f["status"] != cn.FAIL for f in rows),
                       f"an unrendered node is behind, not broken — no FAIL "
                       f"allowed in {section}: {rows}")
        info = _find(report, section, "no rendered contract")
        th.assert_true(info is not None,
                       f"{section} must INFO the absent contract: {rows}")
        th.assert_in("post_deploy", info["detail"],
                     "the INFO must point at running post_deploy")

    th.assert_true(
        not any("grep -lF" in c for c in run.commands),
        "the stale-cron sweep must be SKIPPED with no contract — grading "
        "every installed cron stale against an empty set is noise")


@th.django_unit_test()
def test_mojosec_audit_reads_public_status_but_never_secret_content(opts):
    from mojo.deploy import check_node as cn

    metadata = "root root 600"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    status = json.dumps({
        "schema": "mojosec.status", "version": 1, "state": "running",
        "sensor_id": "i-test", "updated_at": now, "spooled_events": 3,
        "delivery_accepted": 2,
        "collectors": {"journal": {"ok": True}, "nginx": {"ok": True}},
        "delivery": {"sent": 1, "accepted": 1, "retry": 0},
        "config": {
            "source_revision": "r1", "source_sha256": "a" * 64,
            "canonical_revision": "v1:r1",
            "effective_sha256": "b" * 64, "nginx_plane": "standard",
            "nginx_log_path": "/var/log/nginx/mojosec.json.log",
            "trusted_proxy_cidrs": ["10.0.0.0/8"],
            "deployment_mode": "observe", "deployment_criticality": "required",
        },
    })
    run = FakeRunner([
        ("test ! -L /opt/api/var/mojosec.json", (0, "", "")),
        ("/etc/mojosec/config.json", (0, metadata, "")),
        ("/etc/mojosec/enrollment.json", (0, metadata, "")),
        ("/etc/mojosec/credential", (0, metadata, "")),
        ("/var/lib/mojosec", (0, "root root 700", "")),
        ("/etc/systemd/system/mojosec.service", (0, "root root 644", "")),
        ("/etc/mojosec/expected_changes.json", (1, "", "")),
        ("is-active mojosec", (0, "active", "")),
        ("is-enabled mojosec", (0, "enabled", "")),
        ("systemctl show mojosec.service", (0, _mojosec_systemd_show(), "")),
        ("python3 -c", (0, status, "")),
        ("/run/mojosec/status.json", (0, "root root 640", "")),
        ("nginx -T", (0, "log_format mojosec_v1 escape=json\n"
                       "access_log /var/log/nginx/mojosec.json.log mojosec_v1;\n"
                       "set_real_ip_from 10.0.0.0/8;\n"
                       "location = /api/incident/mojosec/batch {\n"
                       "client_max_body_size 512k;\n}\n"
                       "location = /api/incident/mojosec/batch/ {\n"
                       "client_max_body_size 512k;\n}\n", "")),
        ("/var/log/nginx/mojosec.json.log", (0, "root root 640", "")),
        ("grep -F 'maxsize 50M'", (0, "", "")),
    ])
    report = cn.Report()
    cn.check_mojosec(report, run, "observe", "")

    statuses = _statuses(report, "mojosec")
    th.assert_eq(statuses.get("observe lifecycle"), cn.PASS,
                 f"active+enabled observe service must pass: {statuses}")
    th.assert_eq(statuses.get("public status"), cn.PASS,
                 f"bounded public status must be inspectable: {statuses}")
    secret_commands = [command for command in run.commands
                       if ("cat /etc/mojosec/credential" in command or
                           "open('/etc/mojosec/credential" in command)]
    th.assert_eq(secret_commands, [],
                 f"check_node must never open or print the credential: {secret_commands}")


@th.django_unit_test()
def test_mojosec_auto_mode_keeps_legacy_nodes_informational(opts):
    from mojo.deploy import check_node as cn

    run = FakeRunner([
        ("is-active mojosec", (3, "inactive", "")),
        ("is-enabled mojosec", (1, "disabled", "")),
    ])
    report = cn.Report()
    cn.check_mojosec(report, run, "auto", "")

    failures = [row for row in _findings(report, "mojosec")
                if row["status"] == cn.FAIL]
    th.assert_eq(failures, [],
                 f"an upgraded legacy node with no enabled sensor must not fail: {failures}")
    th.assert_true(_find(report, "mojosec", "auto-derived mode: off") is not None,
                   "auto mode must explain that it derived the legacy node as off")


@th.django_unit_test()
def test_mojosec_unit_audit_rejects_byte_drift_and_dropins(opts):
    from mojo.deploy import check_node as cn

    run = FakeRunner([
        ("UNIT_TEXT", (1, "", "")),
        ("systemctl show mojosec.service",
         (0, _mojosec_systemd_show("/etc/systemd/system/mojosec.service.d/override.conf"), "")),
        ("systemd-analyze security", (0, "ok", "")),
    ])
    report = cn.Report()
    cn._audit_mojosec_unit(report, run, "sudo -n ")
    statuses = _statuses(report, "mojosec")
    th.assert_eq(statuses.get("service unit byte drift"), cn.FAIL,
                 f"package-owned unit drift must fail: {statuses}")
    th.assert_eq(statuses.get("effective systemd sandbox drift"), cn.FAIL,
                 f"any drop-in must fail the effective sandbox audit: {statuses}")


@th.django_unit_test()
def test_mojosec_managed_symlink_is_never_accepted_as_metadata(opts):
    from mojo.deploy import check_node as cn

    run = FakeRunner([("test ! -L /etc/mojosec/config.json", (1, "", ""))])
    th.assert_eq(
        cn._secure_metadata(run, "/etc/mojosec/config.json", "600", sudo="sudo -n "),
        None, "a managed-file symlink must fail before stat/contents are trusted")


@th.django_unit_test()
def test_mojosec_exact_nginx_asset_drift_fails(opts):
    from mojo.deploy import check_node as cn

    report = cn.Report()
    cn._audit_exact_mojosec_nginx_assets(
        report, FakeRunner([("LOGROTATE_TEXT", (1, "", ""))]),
        "sudo -n ", ["10.0.0.0/8"])
    th.assert_eq(_statuses(report, "mojosec").get("generated nginx asset drift"),
                 cn.FAIL, "extra or changed generated nginx bytes must fail")


@th.django_unit_test()
def test_mojosec_edge_log_must_be_exactly_beneath_enrolled_root(opts):
    from mojo.deploy import check_node as cn

    root = "/opt/api/var/edge/log"
    th.assert_true(cn._edge_log_contained(root + "/mojosec.json.log", root),
                   "the exact enrolled Edge collector path should pass")
    for escaped in ("/var/log/nginx/mojosec.json.log",
                    root + "/../mojosec.json.log",
                    root + "/nested/mojosec.json.log"):
        th.assert_true(not cn._edge_log_contained(escaped, root),
                       f"Edge collector path escaped its exact root contract: {escaped}")


@th.django_unit_test()
def test_rendered_contract_present_runs_the_stale_sweep(opts):
    from mojo.deploy import check_node as cn

    run = FakeRunner([
        (f"ls -1 {PROJ}/var/deploy/cron.d", (0, "1_certbot", "")),
        ("if [ ! -f ", (0, "SAME", "")),
        ("grep -lF", (0, "/etc/cron.d/1_certbot\n/etc/cron.d/1mojocron", "")),
    ])
    report = cn.Report()
    cn.check_cron(report, run, PROJ)

    stale = _find(report, "cron", "stale project cron: 1mojocron")
    th.assert_true(stale is not None and stale["status"] == cn.FAIL,
                   f"a project-referencing cron the contract no longer ships "
                   f"must FAIL: {_findings(report, 'cron')}")
    th.assert_true(_find(report, "cron", "stale project cron: 1_certbot") is None,
                   "a shipped name is never graded stale")


# ---------------------------------------------------------------------------
# shims grading matrix
# ---------------------------------------------------------------------------

def _shim_runner(state):
    """state: rel -> 'shim' | 'fork' | 'absent'."""
    rules = []
    for rel, kind in state.items():
        path = f"{PROJ}/{rel}"
        rules.append((f"test -f {path}",
                      (0, "", "") if kind != "absent" else (1, "", "")))
        rules.append((f"grep -qF -- mojo.deploy {path}",
                      (0, "", "") if kind == "shim" else (1, "", "")))
    return FakeRunner(rules)


@th.django_unit_test()
def test_shims_matrix(opts):
    from mojo.deploy import check_node as cn

    state = {
        "aws/update.sh": "shim",
        "aws/post_deploy.sh": "fork",
        "aws/certbot_sync.py": "absent",
        "aws/check_node.py": "shim",
    }

    for require, fork_status in ((False, cn.WARN), (True, cn.FAIL)):
        report = cn.Report()
        cn.check_shims(report, _shim_runner(state), PROJ, require)
        statuses = _statuses(report, "shims")
        th.assert_eq(statuses.get("aws/update.sh"), cn.PASS,
                     f"a delegating shim must PASS (require={require}): "
                     f"{statuses}")
        th.assert_eq(statuses.get("aws/post_deploy.sh is a fork"), fork_status,
                     f"a fork must grade {fork_status} under "
                     f"require_shims={require}: {statuses}")
        th.assert_eq(statuses.get("aws/certbot_sync.py absent"), cn.INFO,
                     f"an absent entry point is INFO either way: {statuses}")
        th.assert_eq(statuses.get("aws/check_node.py"), cn.PASS,
                     f"the check_node shim grades like the rest: {statuses}")


# ---------------------------------------------------------------------------
# collision reporting
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_collision_reporting(opts):
    from mojo.deploy import check_node as cn

    def runner():
        return FakeRunner([
            (f"cat {PROJ}/aws/node_overrides.conf",
             (0, "# deliberate fork\n2_mojo_cron\n", "")),
            (f"ls -1 {PROJ}/aws/cron.d",
             (0, "2_mojo_cron\n3_mojo_jobs\n9_extra", "")),
            (f"ls -1 {PROJ}/aws/nginx/systemd", (1, "", "")),
        ])

    for require, undeclared_status in ((False, cn.WARN), (True, cn.FAIL)):
        report = cn.Report()
        cn.check_collisions(report, runner(), PROJ, require)
        statuses = _statuses(report, "shims")
        th.assert_eq(statuses.get("declared override: aws/cron.d/2_mojo_cron"),
                     cn.PASS,
                     f"a declared override is a PASS: {statuses}")
        th.assert_eq(statuses.get("undeclared collision: aws/cron.d/3_mojo_jobs"),
                     undeclared_status,
                     f"an undeclared collision must grade "
                     f"{undeclared_status} under require_shims={require}: "
                     f"{statuses}")
        th.assert_true(not any("9_extra" in name for name in statuses),
                       f"a non-colliding extra is not a finding: {statuses}")


# ---------------------------------------------------------------------------
# probe-url plumbing + retired conf.d audit
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_nginx_probes_the_configured_url_and_flags_retired_vhosts(opts):
    from mojo.deploy import check_node as cn

    url = "http://127.0.0.1:9999/api/version"
    run = FakeRunner([
        ("curl -fsS", (0, '{"status": true, "data": {"version": "1"}}', "")),
        ("nginx -t", (0, "syntax ok", "")),
        ("test -f /etc/nginx/conf.d/old.conf", (0, "", "")),
    ])
    report = cn.Report()
    cn.check_nginx(report, run, PROJ, "", url, ["old.conf"])

    th.assert_true(any(url in c for c in run.commands if "curl" in c),
                   f"the audit must probe the CONFIGURED url, not a "
                   f"hardcoded one: {[c for c in run.commands if 'curl' in c]}")
    probe = _find(report, "nginx", "probe URL")
    th.assert_true(probe is not None and probe["status"] == cn.PASS,
                   f"an envelope answer passes: {_findings(report, 'nginx')}")
    retired = _find(report, "nginx", "retired conf.d file present: old.conf")
    th.assert_true(retired is not None and retired["status"] == cn.WARN,
                   "a node_retired.conf-declared vhost still installed must "
                   f"WARN: {_findings(report, 'nginx')}")


@th.django_unit_test()
def test_nginx_probe_failure_names_the_url(opts):
    from mojo.deploy import check_node as cn

    url = "http://127.0.0.1:9999/api/version"
    run = FakeRunner([
        ("curl -fsS", (7, "", "connection refused")),
        ("nginx -t", (0, "syntax ok", "")),
    ])
    report = cn.Report()
    cn.check_nginx(report, run, PROJ, "", url, [])

    probe = _find(report, "nginx", "probe URL not answering")
    th.assert_true(probe is not None and probe["status"] == cn.FAIL,
                   f"an unanswered probe must FAIL: {_findings(report, 'nginx')}")
    th.assert_in(url, probe["detail"],
                 "the failure must name the exact URL the gates use")


# ---------------------------------------------------------------------------
# node_retired.conf audit
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_read_retired_parses_prefixes_and_comments(opts):
    from mojo.deploy import check_node as cn

    run = FakeRunner([
        (f"cat {PROJ}/aws/node_retired.conf",
         (0, "# retired names\ncron.d/2certbot  # legacy\nconf.d/old.conf\n"
             "\nnot_a_prefix\n", "")),
    ])
    cron, confd = cn.read_retired(run, PROJ)
    th.assert_eq(cron, ["2certbot"],
                 f"cron.d/ lines must parse to bare names: {cron}")
    th.assert_eq(confd, ["old.conf"],
                 f"conf.d/ lines must parse to bare names: {confd}")

    cron, confd = cn.read_retired(FakeRunner([("cat ", (1, "", ""))]), PROJ)
    th.assert_eq((cron, confd), ([], []),
                 "a missing node_retired.conf is empty, not an error — the "
                 "file is optional")


@th.django_unit_test()
def test_legacy_grades_declared_retired_crons(opts):
    from mojo.deploy import check_node as cn

    run = FakeRunner([
        ("test -f ", (1, "", "")),          # no allow_migrate, no update_prod
        ("ls -1 /etc/cron.d", (0, "2certbot\n0hourly", "")),
    ])
    report = cn.Report()
    cn.check_legacy(report, run, PROJ, PROJ, ["2certbot", "gone_already"])

    retired = _find(report, "legacy", "retired cron /etc/cron.d/2certbot")
    th.assert_true(retired is not None and retired["status"] == cn.FAIL,
                   f"a declared-retired cron still installed must FAIL: "
                   f"{_findings(report, 'legacy')}")
    th.assert_true(_find(report, "legacy", "gone_already") is None,
                   "an already-removed retired name is not a finding")

    report = cn.Report()
    cn.check_legacy(report, FakeRunner([("test -f ", (1, "", ""))]),
                    PROJ, PROJ, [])
    empty = _find(report, "legacy", "no declared retired crons")
    th.assert_true(empty is not None and empty["status"] == cn.INFO,
                   f"no declarations is INFO context: "
                   f"{_findings(report, 'legacy')}")


# ---------------------------------------------------------------------------
# template freshness (real filesystem — the one non-runner check)
# ---------------------------------------------------------------------------

def _render_into(proj):
    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    import mojo
    env["PYTHONPATH"] = os.path.dirname(
        os.path.dirname(os.path.abspath(mojo.__file__)))
    done = subprocess.run(
        [sys.executable, "-m", "mojo.deploy", "render",
         "--dest", os.path.join(proj, "var", "deploy"),
         "--project-path", proj],
        env=env, capture_output=True, text=True, timeout=120)
    th.assert_eq(done.returncode, 0,
                 f"fixture render must succeed: {done.stderr}")


@th.django_unit_test()
def test_template_freshness_current_then_moved(opts):
    from mojo.deploy import check_node as cn

    root = tempfile.mkdtemp(prefix="testit_check_node.")
    try:
        proj = os.path.join(root, "proj")
        os.makedirs(proj)
        _render_into(proj)

        report = cn.Report()
        cn.check_template_freshness(report, proj, "ec2-user", "www", "4")
        current = _find(report, "shims", "framework templates current")
        th.assert_true(current is not None and current["status"] == cn.PASS,
                       f"a fresh render must read current: "
                       f"{_findings(report, 'shims')}")

        target = os.path.join(proj, "var", "deploy", "cron.d", "1_certbot")
        with open(target, "a") as handle:
            handle.write("# framework moved on\n")
        report = cn.Report()
        cn.check_template_freshness(report, proj, "ec2-user", "www", "4")
        moved = _find(report, "shims", "framework templates moved")
        th.assert_true(moved is not None and moved["status"] == cn.INFO,
                       f"a diverged template is INFO (run post_deploy), "
                       f"never FAIL: {_findings(report, 'shims')}")
        th.assert_in("cron.d/1_certbot", moved["detail"],
                     "the INFO must name which template moved")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@th.django_unit_test()
def test_template_freshness_skips_declared_overrides_and_absent_var_deploy(opts):
    from mojo.deploy import check_node as cn

    root = tempfile.mkdtemp(prefix="testit_check_node.")
    try:
        proj = os.path.join(root, "proj")
        os.makedirs(os.path.join(proj, "aws"))
        _render_into(proj)
        with open(os.path.join(proj, "aws", "node_overrides.conf"), "w") as handle:
            handle.write("1_certbot\n")
        target = os.path.join(proj, "var", "deploy", "cron.d", "1_certbot")
        with open(target, "w") as handle:
            handle.write("# the project's own copy, by declaration\n")

        report = cn.Report()
        cn.check_template_freshness(report, proj, "ec2-user", "www", "4")
        th.assert_true(_find(report, "shims", "framework templates moved") is None,
                       "a declared override's var/deploy copy is legitimately "
                       "the project's — it must not read as framework drift: "
                       f"{_findings(report, 'shims')}")

        report = cn.Report()
        cn.check_template_freshness(
            report, os.path.join(root, "never-rendered"),
            "ec2-user", "www", "4")
        th.assert_eq(_findings(report, "shims"), [],
                     "with var/deploy absent the freshness check stays silent "
                     "— the cron/systemd sections already report the missing "
                     "contract")
    finally:
        shutil.rmtree(root, ignore_errors=True)
